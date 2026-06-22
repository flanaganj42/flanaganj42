import os, hashlib, requests, email as email_lib
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# ── Config ───────────────────────────────────────────────────────────────────
OLLAMA_URL    = "http://192.168.12.200:11434"
QDRANT_URL    = "http://192.168.12.215:6333"
EMBED_MODEL   = "nomic-embed-text"
COLLECTION    = "nas-docs"
NAS_MOUNT     = "/mnt/nas"
CHUNK_WORDS   = 500
CHUNK_OVERLAP = 50
SUPPORTED     = {".pdf", ".txt", ".md", ".rst", ".emlx"}
EXCLUDE_DIRS  = {"Docker"}

R2_ACCOUNT_ID  = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY  = os.environ.get("R2_ACCESS_KEY")
R2_SECRET_KEY  = os.environ.get("R2_SECRET_KEY")
R2_BUCKET      = "civicresilience-media"
R2_PUBLIC_BASE = "https://media.civicresilience.net"
LARGE_FILE_BYTES = 10 * 1024 * 1024
VIDEO_EXTS     = {".mp4", ".mov", ".mkv", ".webm"}

# ── R2 client (optional) ─────────────────────────────────────────────────────
r2 = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY:
    import boto3
    r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

# ── Helpers ───────────────────────────────────────────────────────────────────
def scan_files(root: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(str(root), onerror=lambda e: print(f"[skip] {e}")):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.startswith("."):
                p = Path(dirpath) / fn
                if p.suffix.lower() in SUPPORTED:
                    files.append(p)
    return files

def read_emlx(path: Path) -> str:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="ignore")
        # emlx files optionally start with a numeric size line before the RFC 2822 content
        first_line, rest = text.split("\n", 1) if "\n" in text else (text, "")
        mail_text = rest if first_line.strip().isdigit() else text
        msg = email_lib.message_from_string(mail_text)
        parts = []
        frm  = msg.get("From", "")
        to   = msg.get("To", "")
        date = msg.get("Date", "")
        subj = msg.get("Subject", "")
        if frm or subj:
            parts.append(f"From: {frm}\nTo: {to}\nDate: {date}\nSubject: {subj}")
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode("utf-8", errors="ignore"))
        return "\n\n".join(parts)
    except Exception as e:
        print(f"  EMLX error {path}: {e}")
        return ""

def read_file(path: Path) -> str:
    if path.suffix.lower() == ".emlx":
        return read_emlx(path)
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        except Exception as e:
            print(f"  PDF error {path}: {e}")
            return ""
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            return raw.decode("utf-16", errors="ignore")
        elif raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig", errors="ignore")
        return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  Read error {path}: {e}")
        return ""

def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if chunk.strip():
            chunks.append(chunk)
        i += CHUNK_WORDS - CHUNK_OVERLAP
    return chunks

def embed(text: str) -> list[float]:
    for attempt in range(3):
        try:
            r = requests.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()["embedding"]
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  Embed retry {attempt + 1}: {e}")

def upload_to_r2(path: Path) -> str | None:
    if not r2:
        return None
    if path.stat().st_size < LARGE_FILE_BYTES and path.suffix.lower() not in VIDEO_EXTS:
        return None
    try:
        key = str(path.relative_to(NAS_MOUNT))
        r2.upload_file(str(path), R2_BUCKET, key)
        return f"{R2_PUBLIC_BASE}/{key}"
    except Exception as e:
        print(f"  R2 error {path}: {e}")
        return None

def ensure_collection(client: QdrantClient, dim: int):
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"Created collection {COLLECTION} (dim={dim})")

def point_id(path: Path, chunk_idx: int) -> int:
    return int(hashlib.md5(f"{path}:{chunk_idx}".encode()).hexdigest()[:8], 16)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    client = QdrantClient(url=QDRANT_URL)
    nas = Path(NAS_MOUNT)

    if not nas.exists():
        print(f"NAS not mounted at {NAS_MOUNT}")
        return

    files = scan_files(nas)
    by_ext = {}
    for f in files:
        by_ext[f.suffix.lower()] = by_ext.get(f.suffix.lower(), 0) + 1
    print(f"Found {len(files)} files: {by_ext}")

    collection_ready = False

    for file_path in files:
        print(f"Processing {file_path}")
        text = read_file(file_path)
        if not text.strip():
            continue

        rel_parts = file_path.relative_to(nas).parts
        nas_subdir = rel_parts[0] if rel_parts else ""
        file_type = file_path.suffix.lstrip(".").lower()

        r2_url = upload_to_r2(file_path)
        chunks = chunk_text(text)
        points = []

        for i, chunk in enumerate(chunks):
            try:
                vector = embed(chunk)
            except Exception as e:
                print(f"  Embed error chunk {i}: {e}")
                continue

            if not collection_ready:
                ensure_collection(client, len(vector))
                collection_ready = True

            payload = {
                "source":     str(file_path),
                "rel_path":   str(file_path.relative_to(nas)),
                "text":       chunk,
                "chunk":      i,
                "nas_subdir": nas_subdir,
                "file_type":  file_type,
            }
            if r2_url:
                payload["r2_url"] = r2_url

            points.append(PointStruct(
                id=point_id(file_path, i),
                vector=vector,
                payload=payload,
            ))

        if points:
            client.upsert(collection_name=COLLECTION, points=points)
            print(f"  Upserted {len(points)} chunks")

    print("Ingest complete")
    if collection_ready:
        info = client.get_collection(COLLECTION)
        print(f"Total vectors: {info.points_count}")

if __name__ == "__main__":
    main()
