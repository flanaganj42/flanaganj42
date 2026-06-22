# Home Cluster Runbook

## Topology

| Node | IP | OS | Role |
|------|----|----|------|
| Mac Mini M1 | 192.168.12.200 | macOS | Ray worker, Ollama :11434, Seagate 5TB NAS |
| Lenovo | 192.168.12.215 | Ubuntu | Ray head, Open WebUI :3000, Portainer :9000, Qdrant, Docker |
| HP | 192.168.12.212 | Windows | Ray worker |

---

## Ray Cluster

### Head node (Lenovo)
- Venv: `~/ray-env`
- Dashboard: `http://192.168.12.215:8265`
- Systemd service: `ray-head`
- Start: `sudo systemctl start ray-head`
- Stop: `sudo systemctl stop ray-head`
- Logs: `journalctl -u ray-head -n 50`
- Started with: `--dashboard-host=0.0.0.0 --dashboard-port=8265 --metrics-export-port=8076`
- Prometheus metrics: `http://192.168.12.215:8076/metrics`

### Worker — HP Windows (192.168.12.212)
- Task Scheduler task: `RayWorker`
- Script: `C:\ray\start-worker.bat`
- Connects to: `192.168.12.215:6379`
- Start manually: `Start-ScheduledTask -TaskName "RayWorker"` (PowerShell as Admin)

### Worker — Mac Mini (192.168.12.200)
- Launchd plist: `/Library/LaunchDaemons/com.ray.worker.plist`
- Connects to: `192.168.12.215:6379`
- Logs: `/tmp/ray-worker.log`, `/tmp/ray-worker-error.log`
- Start manually: `sudo launchctl start com.ray.worker`
- Stop manually: `sudo launchctl stop com.ray.worker`

### Verify cluster health (from Lenovo)
```bash
source ~/ray-env/bin/activate
ray status
```

---

## Firewall — Lenovo (UFW)

Ports that must be open:

| Port | Protocol | Purpose |
|------|----------|---------|
| 8265 | TCP | Ray dashboard (LAN access) |
| 6379 | TCP | Ray GCS (worker connections) |
| 10001 | TCP | Ray client |
| 8076 | TCP | Ray metrics |
| 3000 | TCP | Open WebUI |
| 9000 | TCP | Portainer |

Check: `sudo ufw status numbered`

---

## Services — Lenovo

| Service | URL | Notes |
|---------|-----|-------|
| Open WebUI | http://192.168.12.215:3000 | Runs in Docker |
| Portainer | http://192.168.12.215:9000 | Docker management |
| Qdrant | http://192.168.12.215:6333 | Vector DB, Docker |
| Ray dashboard | http://192.168.12.215:8265 | Systemd service |

---

## Services — Mac Mini

| Service | URL | Notes |
|---------|-----|-------|
| Ollama | http://192.168.12.200:11434 | Bound to 0.0.0.0 via OLLAMA_HOST env; wired into Open WebUI |
| CivicResilience API | http://localhost:8080 / https://api.civicresilience.net | FastAPI — health, ingest, embed, search, document, models, generate |

### Ollama models

| Model | Size | Capabilities |
|-------|------|-------------|
| gemma3:4b | 4B | completion (API default) |
| llama3:latest | 8B Q4_0 | completion |
| llama3.2:latest / :3b | 3.2B Q4_K_M | completion, tools |
| llava:latest | 7B Q4_0 | completion, vision |
| mistral:latest | 7.2B Q4_K_M | completion, tools |
| codellama:latest | 7B Q4_0 | completion |
| nomic-embed-text:latest | 137M F16 | embedding (used by RAG pipeline) |

---

## Ollama ↔ Open WebUI Wiring

Ollama runs on the Mac Mini and is consumed by Open WebUI on the Lenovo.

### Mac Mini — expose Ollama on LAN
Launchd env plist: `/Library/LaunchDaemons/com.ollama.env.plist`
Sets `OLLAMA_HOST=0.0.0.0` at boot so Ollama binds to all interfaces.

Verify: `curl http://192.168.12.200:11434/api/tags`

Restart Ollama after config changes:
```bash
sudo launchctl stop com.ollama.ollama 2>/dev/null || killall ollama
open -a Ollama
```

### Lenovo — Open WebUI Docker config
Open WebUI container is started with:
```
-e OLLAMA_BASE_URL=http://192.168.12.200:11434
```

To recreate the container (data volume `open-webui` is preserved):
```bash
docker stop open-webui && docker rm open-webui
docker run -d \
  --name open-webui \
  --restart always \
  -p 3000:8080 \
  -e OLLAMA_BASE_URL=http://192.168.12.200:11434 \
  -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

Verify connection: Settings → Connections in Open WebUI UI.

### macOS firewall (if Ollama unreachable)
```bash
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /usr/local/bin/ollama
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp /usr/local/bin/ollama
```

---

## Qdrant ↔ Open WebUI + Ray (RAG Pipeline)

### Docker networking
Containers are on the default `bridge` network. There is no shared user-defined network, so containers cannot reach each other by name — use host IPs instead. Qdrant runs as a Docker Swarm service on the `labnet` overlay and is reachable at `192.168.12.215:6333`.

Verify Qdrant reachable from Lenovo host: `curl -s http://192.168.12.215:6333/healthz`

### Open WebUI Docker config (full — includes Ollama + Qdrant + embeddings)
```bash
docker stop open-webui && docker rm open-webui
docker run -d \
  --name open-webui \
  --restart always \
  -p 3000:8080 \
  -e OLLAMA_BASE_URL=http://192.168.12.200:11434 \
  -e VECTOR_DB=qdrant \
  -e QDRANT_URI=http://192.168.12.215:6333 \
  -e RAG_EMBEDDING_ENGINE=ollama \
  -e RAG_OLLAMA_BASE_URL=http://192.168.12.200:11434 \
  -e RAG_EMBEDDING_MODEL=nomic-embed-text \
  -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

### Embedding model (Mac Mini)
```bash
ollama pull nomic-embed-text
```

### Verify RAG
- Open WebUI → Settings → Documents → Qdrant should show as connected
- Upload a doc in chat, ask a question about it
- Check collections: `curl -s http://192.168.12.215:6333/collections | python3 -m json.tool`

### Ray nodes — Qdrant client
```bash
# Lenovo
source ~/ray-env/bin/activate && pip install qdrant-client
# Mac Mini
pip3 install qdrant-client
# HP (PowerShell)
pip install qdrant-client
```

UFW rule to allow Ray workers to reach Qdrant:
```bash
sudo ufw allow from 192.168.12.0/24 to any port 6333 proto tcp
```

### Ray task pattern
```python
import ray
from qdrant_client import QdrantClient

ray.init(address="192.168.12.215:6379")

@ray.remote
def query_qdrant(query_vector, collection):
    client = QdrantClient(host="192.168.12.215", port=6333)
    return client.search(collection_name=collection, query_vector=query_vector, limit=5)
```

---

## NAS Ingestion Pipeline (Ray + Qdrant)

Scans the Seagate NAS (SMB share from Mac Mini), chunks documents, embeds via Ollama `nomic-embed-text`, and stores vectors in Qdrant. Runs distributed across the Ray cluster.

### NAS mount (Lenovo)
Mount point: `/mnt/nas`

`/etc/fstab` entry:
```
//192.168.12.200/SharedData  /mnt/nas  cifs  credentials=/etc/.nascreds,uid=1000,iocharset=utf8,vers=3.0,_netdev,nofail  0  0
```

`/etc/.nascreds` format (chmod 600, owned root):
```
username=jamesflanagan
password=YOUR_PASSWORD
```

Install deps: `sudo apt install -y cifs-utils`
Mount now: `sudo mount -a`

### Pipeline script
Location: `~/ingest/pipeline.py`

Config constants at top of file:
| Constant | Value |
|----------|-------|
| `OLLAMA_URL` | http://192.168.12.200:11434 |
| `QDRANT_URL` | http://192.168.12.215:6333 |
| `EMBED_MODEL` | nomic-embed-text |
| `COLLECTION` | nas-docs |
| `NAS_MOUNT` | /mnt/nas |
| `CHUNK_WORDS` | 500 (overlap 50) |
| `EXCLUDE_DIRS` | `{"Docker"}` |

Supported file types: `.pdf`, `.txt`, `.md`, `.rst`, `.emlx`

Qdrant payload fields per chunk: `source` (absolute path), `rel_path` (relative to NAS root), `text`, `chunk`, `nas_subdir`, `file_type`, `r2_url` (if R2 configured).

Install deps:
```bash
source ~/ray-env/bin/activate
pip install qdrant-client pypdf requests boto3
```

Run manually:
```bash
source ~/ray-env/bin/activate
python ~/ingest/pipeline.py
```

Watch progress:
```bash
watch -n5 'curl -s http://192.168.12.215:6333/collections/nas-docs | python3 -m json.tool'
```

### Nightly systemd timer
- Service: `nas-ingest.service`
- Timer: `nas-ingest.timer` (runs at 02:00 daily)
- Enable: `sudo systemctl enable --now nas-ingest.timer`
- Next run: `systemctl list-timers nas-ingest`
- Logs: `journalctl -u nas-ingest`

### Query pattern (any Ray node)
```python
import ray, requests
from qdrant_client import QdrantClient

ray.init(address="192.168.12.215:6379")

@ray.remote
def search(query, top_k=5):
    emb = requests.post(
        "http://192.168.12.200:11434/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": query},
    ).json()["embedding"]
    client = QdrantClient(url="http://192.168.12.215:6333")
    return client.search(collection_name="nas-docs", query_vector=emb, limit=top_k)

results = ray.get(search.remote("your query here"))
for r in results:
    print(r.score, r.payload["source"], r.payload["text"][:200])
```

---

## Monitoring Stack

### Services
| Service | URL | Notes |
|---------|-----|-------|
| Grafana | http://192.168.12.215:3001 | Container: `ray-grafana`; admin/admin on first login |
| Prometheus | http://192.168.12.215:9090 | Container: `ray-prometheus`; 30d retention |
| cAdvisor | http://192.168.12.215:8081 | Container: `cadvisor`; Docker container metrics (8080 taken by Open WebUI) |
| Uptime Kuma | http://192.168.12.215:3002 | Container: `uptime-kuma`; endpoint monitoring + alerts |

### Docker Compose
Location: `~/monitoring/docker-compose.yml`
Prometheus config: `~/monitoring/prometheus.yml`

```bash
cd ~/monitoring
docker compose up -d
docker compose down
docker compose logs -f ray-prometheus
```

Grafana runs on the `host` network. Prometheus data in `prometheus-data` volume, Grafana data in `grafana-data` volume.

cAdvisor runs standalone (not in the compose file) on port 8081 (8080 is taken by Open WebUI):
```bash
docker run -d --name cadvisor --restart always -p 8081:8080 \
  --volume=/:/rootfs:ro --volume=/var/run:/var/run:ro \
  --volume=/sys:/sys:ro --volume=/var/lib/docker/:/var/lib/docker:ro \
  gcr.io/cadvisor/cadvisor:latest
```

Uptime Kuma runs standalone on port 3002:
```bash
docker run -d --name uptime-kuma --restart always \
  -p 3002:3001 -v uptime-kuma:/app/data louislam/uptime-kuma:1
```

### Prometheus scrape targets
| Job | Target | Status |
|-----|--------|--------|
| node-lenovo | 192.168.12.215:9100 | up |
| node-macmini | 192.168.12.200:9100 | up |
| node-hp | 192.168.12.212:9182 | down — windows_exporter not responding |
| cadvisor | 192.168.12.215:8081 | up |
| ray | 192.168.12.215:8076 | up |
| qdrant | 192.168.12.215:6333/metrics | up |

### node_exporter per node
- **Lenovo**: systemd service `node-exporter`, binary `/usr/local/bin/node_exporter`, port 9100
- **Mac Mini**: Homebrew — `brew services start node_exporter`, port 9100
- **HP**: windows_exporter MSI install, port 9182, runs as Windows service — currently not responding; check service is running and firewall allows inbound 9182 from 192.168.12.215

### Grafana dashboard IDs (import via Dashboards → Import → ID)
| ID | Dashboard |
|----|-----------|
| 1860 | Node Exporter Full — CPU/RAM/disk/network per host |
| 14282 | Docker cAdvisor — container resource usage |
| 17323 | Ray — cluster tasks and actors |

Prometheus data source URL (set in Grafana): `http://localhost:9090` (ray-grafana runs on host network)

### UFW ports (Lenovo)
```bash
sudo ufw allow 9090/tcp   # Prometheus
sudo ufw allow 3001/tcp   # Grafana
sudo ufw allow 3002/tcp   # Uptime Kuma
sudo ufw allow 8081/tcp   # cAdvisor
sudo ufw allow 9100/tcp   # node_exporter
```

---

## CivicResilience API — Status

- `~/api/main.py` live, all deps installed
- Cloudflared tunnel live — `api.civicresilience.net` publicly reachable
- Tunnel ID: `2fc55e8c-b256-467e-ac4c-ab46182b34af`
- Ollama up on 192.168.12.200, bound to 0.0.0.0 via `/Library/LaunchDaemons/com.ollama.env.plist`
- NAS mounted at `/Volumes/clusterstorage`
- SSH key auth Mac Mini → Lenovo: working (`~/.ssh/id_ed25519_new`)
- `~/ingest/pipeline.py` exists on Lenovo; `nas-docs` Qdrant collection populated
- Nightly ingest timer active (02:00 CDT)

### Pending — R2 credentials
R2 upload code is in `pipeline.py` but env vars are not set in the systemd service unit.
Add to `/etc/systemd/system/nas-ingest.service` under `[Service]`:
```ini
Environment=R2_ACCOUNT_ID=your_account_id
Environment=R2_ACCESS_KEY=your_access_key_id
Environment=R2_SECRET_KEY=your_secret_access_key
```
Then: `sudo systemctl daemon-reload`

---

## Common Tasks

### Restart the full Ray cluster
```bash
# On Lenovo
sudo systemctl restart ray-head

# Workers reconnect automatically on their next keepalive cycle (~30s)
# Or force-restart manually on each worker machine
```

### Check Ray logs on Mac Mini worker
```bash
ssh james@192.168.12.200
tail -f /tmp/ray-worker.log
```

### Check Ray logs on HP worker
```powershell
# PowerShell on HP
Get-EventLog -LogName Application -Source "RayWorker" -Newest 20
```

### Update Ray on all nodes
```bash
# Lenovo
source ~/ray-env/bin/activate && pip install --upgrade ray

# Mac Mini
pip3 install --upgrade ray

# HP (PowerShell)
pip install --upgrade ray
```

---

## Cloudflare Tunnel — Mac Mini

Exposes Mac Mini services to the internet via civicresilience.net without open ports.

### One-time setup
```bash
brew install cloudflared
cloudflared tunnel login          # browser auth → civicresilience.net
cloudflared tunnel create civicresilience
cloudflared tunnel route dns civicresilience api.civicresilience.net
cloudflared tunnel route dns civicresilience media.civicresilience.net
```

### Config — `~/.cloudflared/config.yml`
```yaml
tunnel: <tunnel-id>
credentials-file: /Users/jamesflanagan/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: api.civicresilience.net
    service: http://localhost:8080   # CivicResilience FastAPI app
  - hostname: media.civicresilience.net
    service: http://localhost:8081   # optional static file server
  - service: http_status:404
```

### Install as system service
```bash
sudo cloudflared service install
```

This handles macOS code signing and launchd registration automatically.

Check running: `sudo launchctl list | grep cloudflared`
Logs: `tail -f /tmp/cloudflared.log`

---

## Cloudflare R2 — Large File / Video Storage

Large files and video from the NAS pipeline are pushed to R2 (zero egress fees) and served via `media.civicresilience.net`.

### R2 upload in `~/ingest/pipeline.py` (Lenovo)

R2 upload code is already in `pipeline.py`. It activates automatically when env vars are present; if absent, uploads are silently skipped. Threshold: files ≥ 10 MB or video extensions (`.mp4 .mov .mkv .webm`).

Deps already installed: `boto3` (included in `pip install` above).

### R2 env vars — add to `nas-ingest.service` under `[Service]`
```ini
Environment=R2_ACCOUNT_ID=your_account_id
Environment=R2_ACCESS_KEY=your_access_key_id
Environment=R2_SECRET_KEY=your_secret_access_key
```

Then: `sudo systemctl daemon-reload`

---

## CivicResilience FastAPI — Mac Mini

Runs at `localhost:8080`, exposed publicly via Cloudflare Tunnel as `api.civicresilience.net`.

Source: `~/api/main.py`. Key: `/etc/civicresilience-key` (chmod 600, root-owned).
SSH key used for ingest trigger: `~/.ssh/id_ed25519_new`.

### Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | /health | — | Ollama, Qdrant, NAS status |
| POST | /ingest | key | SSH → Lenovo, runs in background |
| GET | /ingest/status | key | Live log tail + exit code |
| POST | /embed | key | Ollama embeddings proxy |
| POST | /search | key | Vector search; optional `subdir`, `file_type` filters |
| GET | /document/{path} | key | Serves file directly from NAS |
| GET | /models | key | Lists Ollama models |
| POST | /generate | key | Ollama generate proxy; default model `gemma3:4b` |

### Launchd — `~/Library/LaunchAgents/com.civicresilience.api.plist`

Uses a LaunchAgent (not a LaunchDaemon) — runs automatically as `jamesflanagan` on login, no sudo required. LaunchDaemon with `UserName` fails with "input/output error" on this macOS version.

Uses a bash wrapper (`~/api/start.sh`) so launchd invokes `/bin/bash` rather than the Python.framework binary directly.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.civicresilience.api</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/jamesflanagan/api/start.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/civicresilience-api.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/civicresilience-api-error.log</string>
  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
```

`~/api/start.sh` reads `/etc/civicresilience-key` (chmod 644, owned root) and execs uvicorn.

Load / reload (no sudo):
```bash
launchctl bootout gui/$(id -u)/com.civicresilience.api 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.civicresilience.api.plist
```

Logs: `tail -f /tmp/civicresilience-api.log`
Status: `launchctl list | grep civicresilience`

### Requirements
```
fastapi
uvicorn[standard]
httpx
qdrant-client
```

```bash
pip3 install -r ~/api/requirements.txt
```

### Usage
```bash
# health (no auth)
curl https://api.civicresilience.net/health

# trigger ingest
curl -X POST https://api.civicresilience.net/ingest \
  -H "x-api-key: YOUR_KEY"

# ingest status + last 50 log lines
curl https://api.civicresilience.net/ingest/status \
  -H "x-api-key: YOUR_KEY"

# semantic search (optional filters: subdir, file_type)
curl -X POST https://api.civicresilience.net/search \
  -H "x-api-key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "emergency water supply", "top_k": 5, "file_type": "pdf"}'

# generate
curl -X POST https://api.civicresilience.net/generate \
  -H "x-api-key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarize community resilience strategies", "model": "gemma3:4b"}'
```

### Request flow
```
Internet → Cloudflare CDN/Tunnel
    ↓
api.civicresilience.net → Mac Mini :8080 (FastAPI)
    ↓
Ollama :11434 (local inference + embeddings)
Qdrant 192.168.12.215:6333 (vector search)
SSH → Lenovo james@192.168.12.215 (ingest trigger)
    ↓
NAS /mnt/nas (Lenovo) / /Volumes/clusterstorage (Mac Mini)
```
