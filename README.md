# ⚡ GPU Agent API

A lightweight, zero-dependency Remote GPU & Compute API Daemon designed specifically for **AI Agents** (Antigravity, Claude Code, Cursor, Codex, autonomous scripts) and developers to securely execute code, schedule GPU jobs, push/pull directories, and monitor hardware on remote cloud servers (AWS EC2, GCP, Lambda, on-premise clusters).

---

## 🚀 1-Command Server Setup (AWS EC2 / Linux)

Run this on your remote server (e.g. Ubuntu AWS EC2 instance):

```bash
curl -sSL https://raw.githubusercontent.com/KarthikeyaAnna/gpu-api/main/setup_server.sh | bash
```

Or clone and run manually:
```bash
git clone https://github.com/KarthikeyaAnna/gpu-api.git ~/gpu-api
bash ~/gpu-api/setup_server.sh
```

The installer will:
1. Initialize directories (`~/gpu-api/logs`, `~/gpu-api/scripts`).
2. Generate a cryptographically secure 256-bit secret key in `~/.gpu_api_key` with `chmod 600`.
3. Install and start a systemd background service (`gpu-server.service`).
4. Output your connection coordinates and secret key.

---

## 🔒 Security Hardening

Because remote GPU servers frequently execute code and scripts, security is strictly enforced:

* **256-Bit Cryptographic Authentication:** All requests require `Authorization: Bearer <KEY>` or `X-API-Key: <KEY>`. Tokens are verified in constant time (`secrets.compare_digest`) to prevent timing side-channel attacks.
* **Automated IP Brute-Force Lockout:** Any IP that fails authentication 5 times within 60 seconds is **automatically banned for 15 minutes** (HTTP 429 Too Many Requests).
* **Filesystem Boundary Sandboxing:** All operations (`exec`, `run`, `upload`, `download`, `files`) are strictly confined to `$HOME`. Symlink traversals to `/etc` or other users are rejected with HTTP 403.
* **Protected System Files:** The server refuses to read, overwrite, or execute `~/.ssh/`, `~/.bashrc`, `~/.profile`, `.gpu_api_key`, or daemon code.
* **Zero-Knowledge Token Redaction:** All access logs automatically redact API keys.

### Recommended: Zero Open Internet Ports (SSH Tunnel)
Do not open port 8888 in your AWS Security Group. Keep the server bound to `127.0.0.1` and connect via an SSH tunnel:
```bash
ssh -N -L 8888:localhost:8888 -i ~/.ssh/your-aws-key.pem ubuntu@<AWS_PUBLIC_IP>
```
Your local client/agent connects to `http://localhost:8888` fully encrypted over SSH.

---

## 💻 CLI Client Usage (`client.py`)

The CLI runs directly on your local machine:

```bash
# Set your server coordinates (or pass --server and --key flags)
export GPU_SERVER_URL="http://localhost:8888"
export GPU_API_KEY="<YOUR_256_BIT_SECRET_KEY>"

# Check server hardware status (GPUs, CPUs, RAM, Disk, Environments)
python client.py status

# Push an entire local folder to the remote server
python client.py push ./my_project ~/code/my_project

# Pull an entire remote folder back to your local machine
python client.py pull ~/code/my_project ./my_project_local

# Run a quick synchronous command on the server
python client.py exec "nvidia-smi"
python client.py exec "pip list"

# Launch an asynchronous training script on GPU
python client.py run ~/code/my_project/train.py --gpu auto

# Stream live execution logs
python client.py logs <job_id> -f

# Terminate a running job
python client.py stop <job_id>

# Generate a new 256-bit secret key
python client.py keygen
```

---

## 🤖 HTTP API Reference for AI Agents

Every endpoint returns predictable, machine-readable JSON:

| Method & Endpoint | Description | Request Example / Payload |
| :--- | :--- | :--- |
| `GET /api/status` | Complete system snapshot (GPUs, CPUs, RAM, Disk, Envs, Jobs) | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/status` |
| `GET /api/gpus` | Real-time GPU telemetry and active compute apps | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/gpus` |
| `GET /api/available` | Recommended CUDA device and list of free GPUs | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/available` |
| `GET /api/envs` | Discovered Conda, virtualenv, and system Python paths | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/envs` |
| `POST /api/exec` | Synchronous shell or Python execution (returns stdout/stderr) | `{"command": "git status"}` or `{"code": "import torch; print(torch.cuda.is_available())"}` |
| `POST /api/run` | Launch background job with GPU assignment & queueing | `{"script": "~/train.py", "gpu": "auto", "wait": true}` or `{"code": "...", "gpu": "0"}` |
| `GET /api/jobs` | List all historical and running jobs | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/jobs` |
| `GET /api/jobs/<job_id>` | Detailed status and exit code of a job | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/jobs/job_123` |
| `GET /api/logs/<job_id>?tail=100` | Fetch tail of job execution logs | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/logs/job_123?tail=100` |
| `POST /api/stop/<job_id>` | Gracefully kill a running job process group | `curl -X POST -H "Authorization: Bearer $KEY" http://localhost:8888/api/stop/job_123` |
| `POST /api/upload?dest=...&extract=true` | Upload and unpack an entire folder archive | Stream `.tar.gz` body to `?dest=~/my_project&extract=true` |
| `GET /api/download?path=...` | Download file or download entire folder as `.tar.gz` | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/download?path=~/my_project` |
| `GET /api/files?path=...` | List directory contents on remote server | `curl -H "Authorization: Bearer $KEY" http://localhost:8888/api/files?path=~/` |

---

## 🧪 Running Integration Tests

To run the automated 23-step verification test suite locally:

```bash
python test_suite.py
```

---

## 📄 License

MIT License. Designed for autonomous AI agent research and distributed cloud GPU workflows.
