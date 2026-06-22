import os, shutil, asyncio, mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

# ── Config ───────────────────────────────────────────────────────────────────
API_KEY            = os.environ["CIVICR_API_KEY"]
OLLAMA_URL         = "http://192.168.12.200:11434"
QDRANT_HOST        = "192.168.12.215"
QDRANT_PORT        = 6333
COLLECTION         = "nas-docs"
LENOVO_IP          = "192.168.12.215"
LENOVO_USER        = "james"
NAS_LOCAL          = "/Volumes/clusterstorage"
PUBLIC_BASE_URL    = "https://api.civicresilience.net"

# ── Auth ─────────────────────────────────────────────────────────────────────
def require_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")

# ── App ──────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="CivicResilience API", lifespan=lifespan)

# ── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    nas = Path(NAS_LOCAL)
    disk = shutil.disk_usage(NAS_LOCAL) if nas.exists() else None

    try:
        async with httpx.AsyncClient() as c:
            ollama_ok = (await c.get(f"{OLLAMA_URL}/api/tags", timeout=3)).status_code == 200
    except Exception:
        ollama_ok = False

    try:
        qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=3)
        collections = [col.name for col in qdrant.get_collections().collections]
        qdrant_ok = True
    except Exception:
        collections = []
        qdrant_ok = False

    return {
        "ollama": ollama_ok,
        "qdrant": qdrant_ok,
        "collections": collections,
        "nas": {
            "mounted":  nas.exists(),
            "total_gb": round(disk.total / 1e9, 1) if disk else None,
            "free_gb":  round(disk.free  / 1e9, 1) if disk else None,
        },
    }

# ── Ingest state ─────────────────────────────────────────────────────────────
INGEST_LOG = Path("/tmp/ingest.log")

_ingest: dict = {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None}

async def _run_ingest_ssh():
    _ingest.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, exit_code=None)
    INGEST_LOG.write_text("")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-i", "/Users/jamesflanagan/.ssh/id_ed25519_new",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            f"{LENOVO_USER}@{LENOVO_IP}",
            "source ~/ray-env/bin/activate && python ~/ingest/pipeline.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for line in proc.stdout:
            with INGEST_LOG.open("ab") as f:
                f.write(line)
        await proc.wait()
        _ingest.update(
            status="done" if proc.returncode == 0 else "failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            exit_code=proc.returncode,
        )
    except Exception as e:
        _ingest.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(), exit_code=-1)
        INGEST_LOG.write_text(f"Launch error: {e}\n")

@app.post("/ingest", dependencies=[Depends(require_key)])
async def trigger_ingest(background_tasks: BackgroundTasks):
    if _ingest["status"] == "running":
        return {"status": "already_running", "started_at": _ingest["started_at"]}
    background_tasks.add_task(_run_ingest_ssh)
    return {"status": "queued"}

@app.get("/ingest/status", dependencies=[Depends(require_key)])
async def ingest_status(lines: int = 50):
    log_tail = []
    if INGEST_LOG.exists():
        all_lines = INGEST_LOG.read_text(errors="ignore").splitlines()
        log_tail = all_lines[-lines:]
    return {**_ingest, "log": log_tail}

# ── Embed ─────────────────────────────────────────────────────────────────────
class EmbedRequest(BaseModel):
    text: str
    model: str = "nomic-embed-text"

@app.post("/embed", dependencies=[Depends(require_key)])
async def embed(req: EmbedRequest):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": req.model, "prompt": req.text},
            timeout=30,
        )
    r.raise_for_status()
    return r.json()

# ── Search ────────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    subdir: str | None = None
    file_type: str | None = None

@app.post("/search", dependencies=[Depends(require_key)])
async def search(req: SearchRequest):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": req.query},
            timeout=30,
        )
    r.raise_for_status()
    vector = r.json()["embedding"]

    conditions = []
    if req.subdir:
        conditions.append(FieldCondition(key="nas_subdir", match=MatchValue(value=req.subdir)))
    if req.file_type:
        conditions.append(FieldCondition(key="file_type", match=MatchValue(value=req.file_type.lstrip("."))))
    qdrant_filter = Filter(must=conditions) if conditions else None

    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    hits = qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=req.top_k,
        query_filter=qdrant_filter,
    ).points

    return [
        {
            "score":        round(h.score, 4),
            "source":       h.payload.get("source"),
            "nas_subdir":   h.payload.get("nas_subdir"),
            "file_type":    h.payload.get("file_type"),
            "text":         h.payload.get("text", "")[:300],
            "document_url": _document_url(h.payload.get("rel_path")),
            "r2_url":       h.payload.get("r2_url"),
            "chunk":        h.payload.get("chunk"),
        }
        for h in hits
    ]

# ── Document serving ─────────────────────────────────────────────────────────
def _document_url(rel_path: str | None) -> str | None:
    if not rel_path:
        return None
    return f"{PUBLIC_BASE_URL}/document/{quote(rel_path)}"

@app.get("/document/{path:path}", dependencies=[Depends(require_key)])
async def serve_document(path: str):
    full_path = (Path(NAS_LOCAL) / path).resolve()
    if not str(full_path).startswith(str(Path(NAS_LOCAL).resolve())):
        raise HTTPException(400, "Invalid path")
    if not full_path.exists():
        raise HTTPException(404, "File not found")
    mime, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(str(full_path), media_type=mime or "application/octet-stream",
                        filename=full_path.name)

# ── Models ───────────────────────────────────────────────────────────────────
@app.get("/models", dependencies=[Depends(require_key)])
async def list_models():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()
    return [{"name": m["name"], "size_gb": round(m["size"] / 1e9, 1)} for m in r.json().get("models", [])]

# ── Generate (Ollama proxy) ───────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    prompt: str
    model: str = "gemma3:4b"
    stream: bool = False

@app.post("/generate", dependencies=[Depends(require_key)])
async def generate(req: GenerateRequest):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/generate",
            json=req.model_dump(),
            timeout=120,
        )
    r.raise_for_status()
    return r.json()
