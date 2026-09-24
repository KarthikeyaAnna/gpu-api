# AI Agent Integration Guide: Remote GPU Cluster API

This guide provides instructions and API specifications for **AI Agents** (e.g., Antigravity, Claude Code, Cursor, automated scripts) to safely execute code, monitor training, and manage GPU resources on the IIITD cluster without human intervention and **with zero interference to other server users**.

---

## 1. System Coordinates & Authentication

| Parameter | Value |
|:---|:---|
| **Base URL** | `http://192.168.24.140:8888` |
| **Cluster Hostname** | `sbilab` / `iiitd` |
| **Server User Account** | `karthikeya` (home: `/home/karthikeya`) |
| **Hardware** | 2× NVIDIA RTX A4000 (16 GB VRAM each; index `0` and `1`) |
| **Auth Header** | `Authorization: Bearer <API_KEY>` |
| **Auth Query Param** | `?key=<API_KEY>` (supported on all endpoints) |
| **API Key Location** | Laptop: `~/.gpu_api_key`<br>Cluster: `/home/karthikeya/.gpu_api_key` |

> [!IMPORTANT]
> **Authentication:** Pass the API key via `Authorization: Bearer <API_KEY>` or query parameter `?key=<API_KEY>`.
> Store your secret key securely in `~/.gpu_api_key` or as environment variable `GPU_API_KEY`.
> **Never commit or hardcode raw API keys into public repositories or shared documentation.**

---

## 2. Hard Constraints & Zero-Interference Policy

AI agents and automated scripts interacting with this cluster MUST adhere strictly to the following rules. Failure to observe these rules can disrupt active academic research, crash ongoing training runs, or corrupt colleagues' experiments.

### 2.1 Code, Data, & Filesystem Isolation
1. **Never Touch Other Users' Files:** Under NO circumstances should an agent read, edit, execute, copy, or overwrite code, notebooks, scripts, logs, model weights, or datasets belonging to any other cluster user (e.g., `/home/arush/`, `/home/divya/`, `/home/*`).
2. **Strict `$HOME` Confinement:** All work, scratch files, data downloads, checkpoints, and execution directories MUST reside strictly within `/home/karthikeya/`.
3. **No Cross-User Dependencies:** Never import Python modules, reference virtualenvs, or call scripts located inside another user's home directory.
4. **Filesystem Protections:** The API enforces a strict boundary (`is_safe_user_path()`). Any attempt to traverse directories, create symlinks outside `$HOME`, or touch system configs returns HTTP `403 Forbidden`.

### 2.2 Process & Compute Isolation
1. **Never Interfere with Other Users' Processes:** Never attempt to signal, terminate, inspect, pause, or renice processes (`kill`, `pkill`, `killall`, `renice`, etc.) owned by other lab members (`arush`, `divya`, `root`, etc.).
2. **Safe API Termination Only:** Always terminate jobs through `POST /api/stop/{job_id}` or `gpu stop <job_id>`. The server manages its own process groups (`os.setsid`) and will never signal external processes.

### 2.3 Strict Zero GPU Sharing (No Co-Tenancy)
1. **Strict Dedicated Allocation:** GPU sharing is strictly disabled on this cluster. Even if a GPU currently in use by another researcher has unused VRAM (e.g., 6 GB free), co-tenancy is forbidden because concurrent allocations cause memory fragmentation and Out-of-Memory (OOM) crashes for their running job.
2. **Explicit Assignment Only:** Always pass an explicit GPU ID (`"gpu": "0"` or `"gpu": "1"`). Auto-allocation and multi-GPU requests are rejected.
3. **Handling Busy GPUs:** If your target GPU is occupied, the API returns HTTP `409 Conflict`. You must:
   - Target the alternate idle GPU if available, OR
   - Pass `"wait": true` (or `-w` on CLI) to place the job in the background queue until the GPU becomes 100% idle. Never poll aggressively in tight loops.

### 2.4 Environment & Package Isolation
1. **No Shared Environment Pollution:** Do not install packages into system Python or another user's Conda environments.
2. **Dedicated Envs Only:** Always execute inside your own isolated Conda environments located in `/home/karthikeya/miniconda3/envs/` or the default `/home/karthikeya/miniconda3/bin/python` (`base`).

---

## 3. API Endpoints Reference

### 3.1 Check GPU Availability & Status
**Endpoint:** `GET /api/gpus`

```bash
curl -s -H "Authorization: Bearer $GPU_API_KEY" \
  http://192.168.24.140:8888/api/gpus
```

**Response (JSON):**
```json
{
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA RTX A4000",
      "status": "BUSY",
      "utilization_pct": 100.0,
      "memory_total_mb": 16376,
      "memory_used_mb": 10859,
      "memory_free_mb": 5114,
      "memory_free_gb": 4.99,
      "memory_used_pct": 66.3,
      "temperature_c": 92.0,
      "power_w": 134.33,
      "processes": [
        {
          "pid": 3324451,
          "user": "arush",
          "process_name": "/home/arush/miniconda3/envs/interp/bin/python",
          "memory_used_mb": 10850.0
        }
      ]
    },
    {
      "index": 1,
      "name": "NVIDIA RTX A4000",
      "status": "AVAILABLE",
      "utilization_pct": 0.0,
      "memory_total_mb": 16376,
      "memory_used_mb": 1,
      "memory_free_mb": 15974,
      "memory_free_gb": 15.6,
      "memory_used_pct": 0.0,
      "temperature_c": 51.0,
      "power_w": 15.13,
      "processes": []
    }
  ]
}
```

---

### 3.2 Quick Availability Query
**Endpoint:** `GET /api/available`

```bash
curl -s -H "Authorization: Bearer $GPU_API_KEY" \
  http://192.168.24.140:8888/api/available
```

**Response (JSON):**
```json
{
  "available_gpus": [1],
  "count": 1,
  "recommended_cuda_device": "1"
}
```

---

### 3.3 Launch a Python Job
**Endpoint:** `POST /api/run`

**Request Payload (JSON):**
```json
{
  "script": "~/my_project/train.py",
  "gpu": "1",
  "conda_env": "base",
  "args": ["--epochs", "20", "--batch_size", "32"],
  "wait": true
}
```
*Alternatively, pass raw Python code using `"code": "import torch\n..."` instead of `"script"`.*

**Parameters:**
| Field | Type | Required | Description |
|:---|:---|:---|:---|
| `script` | `string` | Yes* | Absolute or `~/` relative path on cluster |
| `code` | `string` | Yes* | Direct python code snippet (saved automatically) |
| `gpu` | `string` | **Yes** | Must be `"0"` or `"1"` |
| `conda_env` | `string` | No | Conda environment (default: `"base"`) |
| `args` | `list`/`str` | No | Arguments passed to the script |
| `wait` | `boolean` | No | If `true`, queues job if GPU is currently busy |
| `cwd` | `string` | No | Working directory (default: `/home/karthikeya`) |

**Response — GPU Free (Immediate Launch, HTTP 200):**
```json
{
  "status": "SUCCESS",
  "message": "Job job_de401f86 launched on GPU 1",
  "job": {
    "id": "job_de401f86",
    "script": "/home/karthikeya/my_project/train.py",
    "args": ["--epochs", "20"],
    "gpu": "1",
    "conda_env": "base",
    "python_bin": "/home/karthikeya/miniconda3/bin/python",
    "pid": 3348129,
    "status": "RUNNING",
    "created_time": "2026-09-07T11:05:00",
    "start_time": "2026-09-07T11:05:00",
    "log_file": "/home/karthikeya/gpu-api/logs/job_de401f86.log"
  }
}
```

**Response — GPU Busy with `"wait": true` (Queued, HTTP 200):**
```json
{
  "status": "QUEUED",
  "message": "GPU 0 is busy. Job job_70c8ece1 placed in queue; will automatically launch when GPU 0 is free.",
  "job": {
    "id": "job_70c8ece1",
    "gpu": "0",
    "status": "QUEUED",
    "start_time": null
  }
}
```

**Response — GPU Busy with `"wait": false` (Rejected, HTTP 409):**
```json
{
  "error": "Refusing to run: GPU 0 is currently IN USE by 'arush' (10859 MB VRAM used). GPU sharing is strictly disabled. Run with --wait (-w) to queue until this GPU is free."
}
```

---

### 3.4 Poll Job Status
**Endpoint:** `GET /api/jobs`

```bash
curl -s -H "Authorization: Bearer $GPU_API_KEY" \
  http://192.168.24.140:8888/api/jobs
```

**Response (JSON):**
```json
{
  "jobs": [
    {
      "id": "job_de401f86",
      "script": "/home/karthikeya/my_project/train.py",
      "gpu": "1",
      "pid": 3348129,
      "status": "RUNNING",
      "returncode": null,
      "start_time": "2026-09-07T11:05:00",
      "end_time": null
    }
  ]
}
```
*Job statuses: `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `STOPPED`.*

---

### 3.5 Fetch Job Logs
**Endpoint:** `GET /api/logs/{job_id}?tail={n}`

```bash
curl -s -H "Authorization: Bearer $GPU_API_KEY" \
  "http://192.168.24.140:8888/api/logs/job_de401f86?tail=100"
```

**Response (JSON):**
```json
{
  "job_id": "job_de401f86",
  "status": "RUNNING",
  "logs": "Epoch 1/20 - loss: 0.452 - acc: 0.82\nEpoch 2/20 - loss: 0.381 - acc: 0.87\n"
}
```

---

### 3.6 Terminate / Cancel a Job
**Endpoint:** `POST /api/stop/{job_id}`

```bash
curl -s -X POST -H "Authorization: Bearer $GPU_API_KEY" \
  http://192.168.24.140:8888/api/stop/job_de401f86
```

**Response (JSON):**
```json
{
  "status": "SUCCESS",
  "message": "Job job_de401f86 stopped"
}
```

---

### 3.7 Download Artifacts / Models
**Endpoint:** `GET /api/download?path={path}`

```bash
curl -s -H "Authorization: Bearer $GPU_API_KEY" \
  "http://192.168.24.140:8888/api/download?path=~/outputs/checkpoint.pt" \
  -o ./checkpoint.pt
```

---

### 3.8 Upload Code / Data to Server
**Endpoint:** `POST /api/upload?dest={destination_path}`

```bash
curl -s -X POST -H "Authorization: Bearer $GPU_API_KEY" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @./train.py \
  "http://192.168.24.140:8888/api/upload?dest=~/my_project/train.py"
```

---

## 4. Standard AI Agent Workflow (Copy-Pasteable Python)

AI agents can use this drop-in Python snippet to autonomously check GPUs, upload/run a script, poll for completion, and inspect results:

```python
import os
import time
import urllib.request
import json

API_BASE = "http://192.168.24.140:8888"

# Retrieve API key securely from environment or local key file
API_KEY = os.environ.get("GPU_API_KEY")
if not API_KEY and os.path.exists(os.path.expanduser("~/.gpu_api_key")):
    with open(os.path.expanduser("~/.gpu_api_key"), "r") as f:
        API_KEY = f.read().strip()

def api(endpoint, method="GET", payload=None):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{API_BASE}{endpoint}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def run_agent_job(script_path, args=None):
    # 1. Inspect available GPUs
    avail = api("/api/available")
    free_gpus = avail.get("available_gpus", [])
    
    # Pick a free GPU, or GPU 1 by default with wait=True
    target_gpu = str(free_gpus[0]) if free_gpus else "1"
    print(f"Targeting GPU {target_gpu} (Free GPUs: {free_gpus})")
    
    # 2. Launch job (with wait=True to auto-queue if busy)
    run_resp = api("/api/run", method="POST", payload={
        "script": script_path,
        "gpu": target_gpu,
        "args": args or [],
        "wait": True
    })
    job_id = run_resp["job"]["id"]
    print(f"Submitted {job_id} — Initial Status: {run_resp['job']['status']}")

    # 3. Poll status until completion
    while True:
        time.sleep(3)
        jobs_data = api("/api/jobs")
        job = next((j for j in jobs_data["jobs"] if j["id"] == job_id), None)
        if not job:
            raise RuntimeError("Job disappeared from tracker!")
            
        status = job["status"]
        if status in ("COMPLETED", "FAILED", "STOPPED"):
            print(f"Job {job_id} terminated with status: {status} (returncode: {job.get('returncode')})")
            break
        print(f"Job {job_id} is {status}...")

    # 4. Fetch final logs
    logs = api(f"/api/logs/{job_id}?tail=50")
    print("--- Final Logs ---")
    print(logs.get("logs", ""))
    return job

if __name__ == "__main__":
    run_agent_job("~/test_gpu_job.py")
```

---

## 5. Laptop CLI Tool Alternative

If the agent operates inside a bash shell, it can invoke `gpu` directly:

```bash
# Check status
gpu status
gpu available

# Run script (with wait queue)
gpu run ~/train.py --gpu 1 -w

# Follow live output
gpu logs <job_id> -f

# Download checkpoint
gpu download ~/checkpoints/best.pt ./best.pt
```
