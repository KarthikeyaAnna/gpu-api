#!/usr/bin/env python3
"""
AI Agent Remote GPU & Compute API Daemon
========================================
A lightweight, zero-dependency HTTP API designed specifically for AI Agents
(Antigravity, Claude Code, Cursor, Codex, CI/CD, and autonomous scripts) to
safely coordinate with remote cloud GPU instances (AWS EC2, GCP, Lambda, etc.)
or on-premise clusters.

Key Features for AI Agents:
  - 100% Machine-Readable JSON APIs with structured error schemas.
  - Zero external dependencies: pure Python 3 standard library.
  - POST /api/exec: Synchronous command execution (returns stdout, stderr, exit_code directly).
  - POST /api/run: Asynchronous long-running jobs (training, evaluation) with GPU scheduling.
  - GET /api/status: Complete system snapshot (GPUs, CPUs, RAM, Disk, Active Jobs, Python Envs).
  - Flexible GPU targeting: "auto", specific index ("0"), multi-GPU ("0,1", "all"), or "cpu".
  - Log retrieval with reverse tailing and byte offsets for efficient polling.
  - Python Environment discovery (Conda, Venvs, Pyenv, System Python).
  - Sandboxed file transfer (upload, download, directory listing) strictly confined to $HOME.
  - Clean process isolation & tree termination (SIGTERM -> SIGKILL via process groups).
  - Token-based authentication (Bearer header, X-API-Key, or ?key= query parameter).
"""

import os
import sys
import json
import time
import signal
import socket
import io
import tarfile
import tempfile
import re
import secrets
import threading
import subprocess
import shutil
import getpass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Server Configurations ---
CURRENT_USER = getpass.getuser()
USER_HOME = os.path.expanduser("~")
HOST = os.environ.get("GPU_SERVER_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("GPU_SERVER_PORT", "8888"))
FALLBACK_PORT = int(os.environ.get("GPU_SERVER_FALLBACK_PORT", "8889"))

API_DIR = os.environ.get("GPU_API_DIR", os.path.join(USER_HOME, "gpu-api"))
LOGS_DIR = os.path.join(API_DIR, "logs")
SCRIPTS_DIR = os.path.join(API_DIR, "scripts")
KEY_FILE = os.environ.get("GPU_API_KEY_FILE", os.path.join(USER_HOME, ".gpu_api_key"))
PID_FILE = os.path.join(API_DIR, "server.pid")
JOBS_FILE = os.path.join(API_DIR, "jobs.json")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SCRIPTS_DIR, exist_ok=True)

MAX_UPLOAD_SIZE = int(os.environ.get("GPU_MAX_UPLOAD_MB", "1024")) * 1024 * 1024   # default 1 GB
MAX_JSON_BODY = 20 * 1024 * 1024      # 20 MB max JSON payload

PROTECTED_PATHS = [
    os.path.join(USER_HOME, ".ssh"),
    os.path.join(USER_HOME, ".gnupg"),
    os.path.join(USER_HOME, ".bashrc"),
    os.path.join(USER_HOME, ".bash_profile"),
    os.path.join(USER_HOME, ".profile"),
    os.path.join(USER_HOME, ".zshrc"),
    os.path.join(USER_HOME, ".gpu_api_key"),
    os.path.abspath(__file__),
]


# --- Security & Path Sandboxing ---
def normalize_server_path(p):
    """Normalize input path to an absolute path within USER_HOME."""
    if not p:
        return USER_HOME
    p = p.strip()
    if p.startswith("~"):
        return os.path.normpath(os.path.join(USER_HOME, p[1:].lstrip("/\\")))
    if not os.path.isabs(p):
        return os.path.normpath(os.path.join(USER_HOME, p))
    return os.path.normpath(p)


def is_safe_user_path(target_path, root_dir=USER_HOME):
    """Validates that target_path resolves strictly inside root_dir (protects against symlink traversal)."""
    try:
        real_target = os.path.realpath(target_path)
        real_root = os.path.realpath(root_dir)
        return os.path.commonpath([real_root, real_target]) == real_root
    except (ValueError, OSError):
        return False


def is_protected_path(target_path):
    """Prevents reading, writing, or executing sensitive credentials and shell configs."""
    try:
        real_target = os.path.realpath(target_path)
        for prot in PROTECTED_PATHS:
            real_prot = os.path.realpath(prot)
            if real_target == real_prot or real_target.startswith(real_prot + os.sep):
                return True
    except (ValueError, OSError):
        return True
    return False


def safe_extract_tar(tar, dest_dir):
    """Safely extracts a tar archive into dest_dir, preventing path traversal and protecting sensitive files."""
    real_dest = os.path.realpath(dest_dir)
    os.makedirs(real_dest, exist_ok=True)
    count = 0
    for member in tar.getmembers():
        member_target = os.path.realpath(os.path.join(real_dest, member.name))
        if os.path.commonpath([real_dest, member_target]) != real_dest:
            raise ValueError(f"Path traversal detected in archive member: {member.name}")
        if is_protected_path(member_target):
            raise ValueError(f"Archive attempts to overwrite protected file: {member.name}")
        if member.isdev() or member.isfifo():
            continue
        tar.extract(member, path=real_dest)
        count += 1
    return count


def get_or_create_api_key():
    """Retrieve existing API key or generate a 256-bit cryptographically secure hex token."""
    if "GPU_API_KEY" in os.environ and os.environ["GPU_API_KEY"].strip():
        return os.environ["GPU_API_KEY"].strip()
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, "r") as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception:
            pass
    # Generate 64 hex characters = 256 bits of pure cryptographic entropy
    key = secrets.token_hex(32)
    try:
        with open(KEY_FILE, "w") as f:
            f.write(key + "\n")
        os.chmod(KEY_FILE, 0o600)
    except Exception as e:
        sys.stderr.write(f"Warning: Could not write API key to {KEY_FILE}: {e}\n")
    return key


API_KEY = get_or_create_api_key()

# --- Security: IP Rate Limiting & Brute-Force Lockout ---
failed_auth_lock = threading.Lock()
failed_auth_attempts = {}   # ip -> list of float timestamps
banned_ips = {}             # ip -> float timestamp until which banned

MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SEC = 60
BAN_DURATION_SEC = 900      # 15 minutes lockout

# --- Jobs In-Memory Store & Persistence ---
jobs_lock = threading.Lock()
jobs = {}


def load_jobs():
    global jobs
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, "r") as f:
                saved = json.load(f)
                with jobs_lock:
                    jobs.update(saved)
        except Exception:
            pass


def save_jobs():
    with jobs_lock:
        to_save = {}
        for jid, data in jobs.items():
            to_save[jid] = {k: v for k, v in data.items() if k != "proc"}
        try:
            temp_file = JOBS_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(to_save, f, indent=2)
            os.replace(temp_file, JOBS_FILE)
        except Exception:
            pass


load_jobs()


def tail_file(filepath, n_lines=200, chunk_size=8192):
    """Efficiently read the last n lines of a file without loading entire file into memory."""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return ""
            remaining = file_size
            buffer = bytearray()
            lines_found = 0
            while remaining > 0 and lines_found <= n_lines:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                f.seek(remaining, os.SEEK_SET)
                chunk = f.read(read_size)
                buffer = chunk + buffer
                lines_found = buffer.count(b"\n")
            parts = buffer.split(b"\n")
            if len(parts) > n_lines:
                buffer = b"\n".join(parts[-n_lines:])
            return buffer.decode("utf-8", errors="replace")
    except Exception:
        try:
            with open(filepath, "r", errors="replace") as f:
                lines = f.readlines()
                if n_lines:
                    lines = lines[-n_lines:]
                return "".join(lines)
        except Exception as e:
            return f"(Error reading log file: {e})"


# --- Hardware & System Introspection ---
def get_gpu_info():
    """Query nvidia-smi for real-time GPU statistics and processes. Returns [] if no GPU."""
    gpus = []
    has_nvidia = False
    try:
        subprocess.check_call(["which", "nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        has_nvidia = True
    except Exception:
        has_nvidia = False

    if not has_nvidia:
        return []

    try:
        cmd_gpus = [
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free,temperature.gpu,power.draw,gpu_bus_id",
            "--format=csv,noheader,nounits"
        ]
        out_gpus = subprocess.check_output(cmd_gpus, text=True, timeout=5).strip().splitlines()

        cmd_apps = [
            "nvidia-smi",
            "--query-compute-apps=gpu_bus_id,pid,process_name,used_memory",
            "--format=csv,noheader,nounits"
        ]
        out_apps = []
        try:
            out_apps = subprocess.check_output(cmd_apps, text=True, timeout=5).strip().splitlines()
        except Exception:
            pass

        apps_by_bus = {}
        for line in out_apps:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                bus_id, pid, pname, mem = parts[0], parts[1], parts[2], parts[3]
                user = "unknown"
                try:
                    user = subprocess.check_output(["ps", "-p", pid, "-o", "user="], text=True, timeout=2).strip()
                except Exception:
                    pass
                apps_by_bus.setdefault(bus_id, []).append({
                    "pid": int(pid) if pid.isdigit() else pid,
                    "user": user,
                    "process_name": pname,
                    "memory_used_mb": float(mem) if mem.replace(".", "").isdigit() else mem
                })

        for line in out_gpus:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 9:
                idx = int(parts[0])
                name = parts[1]
                util = float(parts[2]) if parts[2].replace(".", "").isdigit() else 0.0
                mem_tot = float(parts[3]) if parts[3].replace(".", "").isdigit() else 0.0
                mem_used = float(parts[4]) if parts[4].replace(".", "").isdigit() else 0.0
                mem_free = float(parts[5]) if parts[5].replace(".", "").isdigit() else 0.0
                temp = float(parts[6]) if parts[6].replace(".", "").isdigit() else 0.0
                power = float(parts[7]) if parts[7].replace(".", "").isdigit() else 0.0
                bus_id = parts[8]

                procs = apps_by_bus.get(bus_id, [])
                is_available = (mem_used < 600) and (util < 10) and (len(procs) == 0)

                gpus.append({
                    "index": idx,
                    "name": name,
                    "status": "AVAILABLE" if is_available else "BUSY",
                    "utilization_pct": util,
                    "memory_total_mb": mem_tot,
                    "memory_used_mb": mem_used,
                    "memory_free_mb": mem_free,
                    "memory_free_gb": round(mem_free / 1024.0, 2),
                    "memory_used_pct": round((mem_used / mem_tot) * 100.0, 1) if mem_tot > 0 else 0,
                    "temperature_c": temp,
                    "power_w": power,
                    "bus_id": bus_id,
                    "processes": procs
                })
    except Exception as e:
        gpus = [{"error": f"Failed to query nvidia-smi: {str(e)}"}]
    return gpus


def get_system_metrics():
    """Returns general host hardware metrics: CPU cores, RAM, and Disk space."""
    metrics = {
        "hostname": socket.gethostname(),
        "user": CURRENT_USER,
        "home": USER_HOME,
        "cpu_count": os.cpu_count() or 1,
    }

    # Memory info from /proc/meminfo
    try:
        with open("/proc/meminfo", "r") as f:
            mem_data = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().split()[0]
                    if v.isdigit():
                        mem_data[k] = int(v)
            if "MemTotal" in mem_data and "MemAvailable" in mem_data:
                total_mb = mem_data["MemTotal"] / 1024.0
                avail_mb = mem_data["MemAvailable"] / 1024.0
                used_mb = total_mb - avail_mb
                metrics["ram"] = {
                    "total_mb": round(total_mb, 1),
                    "used_mb": round(used_mb, 1),
                    "free_mb": round(avail_mb, 1),
                    "used_pct": round((used_mb / total_mb) * 100.0, 1)
                }
    except Exception:
        pass

    # Disk space of USER_HOME
    try:
        usage = shutil.disk_usage(USER_HOME)
        metrics["disk"] = {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "used_pct": round((usage.used / usage.total) * 100.0, 1)
        }
    except Exception:
        pass

    # Load average
    try:
        metrics["load_avg"] = list(os.getloadavg())
    except Exception:
        pass

    return metrics


def get_available_gpus():
    """Returns available GPU indices and the recommended device for new jobs."""
    gpu_list = get_gpu_info()
    available = []
    best_gpu = None
    max_free = -1

    for g in gpu_list:
        if "error" in g:
            continue
        if g.get("status") == "AVAILABLE" and len(g.get("processes", [])) == 0:
            available.append(g["index"])
            if g.get("memory_free_mb", -1) > max_free:
                max_free = g["memory_free_mb"]
                best_gpu = g["index"]

    return {
        "available_gpus": available,
        "count": len(available),
        "total_gpus": len([g for g in gpu_list if "error" not in g]),
        "recommended_cuda_device": str(best_gpu) if best_gpu is not None else None
    }


# --- Python Environments Discovery ---
def get_available_environments():
    """Discovers conda environments, virtualenvs, pyenvs, and system Python."""
    envs = {}

    conda_base_dirs = [
        os.path.join(USER_HOME, "miniconda3"),
        os.path.join(USER_HOME, "anaconda3"),
        os.path.join(USER_HOME, ".conda"),
        "/opt/conda",
        "/opt/miniconda"
    ]
    for c_base in conda_base_dirs:
        base_py = os.path.join(c_base, "bin", "python")
        if os.path.exists(base_py):
            envs["conda:base"] = base_py
        envs_dir = os.path.join(c_base, "envs")
        if os.path.isdir(envs_dir):
            for d in os.listdir(envs_dir):
                env_py = os.path.join(envs_dir, d, "bin", "python")
                if os.path.exists(env_py):
                    envs[f"conda:{d}"] = env_py

    venv_locations = [
        os.path.join(USER_HOME, ".venv", "bin", "python"),
        os.path.join(USER_HOME, "venv", "bin", "python"),
        os.path.join(USER_HOME, "env", "bin", "python"),
    ]
    for vp in venv_locations:
        if os.path.exists(vp):
            name = os.path.basename(os.path.dirname(os.path.dirname(vp)))
            envs[f"venv:{name}"] = vp

    if "VIRTUAL_ENV" in os.environ:
        act_py = os.path.join(os.environ["VIRTUAL_ENV"], "bin", "python")
        if os.path.exists(act_py):
            envs["venv:active"] = act_py

    envs["system"] = sys.executable
    return envs


def resolve_python_path(env_identifier="base"):
    """Resolves an environment name, alias, or direct python path into an executable path."""
    if not env_identifier:
        env_identifier = "base"

    if os.path.isabs(env_identifier) and os.path.exists(env_identifier):
        return env_identifier

    if env_identifier.startswith("~"):
        exp = os.path.expanduser(env_identifier)
        if os.path.exists(exp):
            return exp

    all_envs = get_available_environments()

    if env_identifier in all_envs:
        return all_envs[env_identifier]

    for key, path in all_envs.items():
        subname = key.split(":", 1)[1] if ":" in key else key
        if subname == env_identifier:
            return path

    cand = os.path.join(USER_HOME, env_identifier, "bin", "python")
    if os.path.exists(cand):
        return cand

    cand2 = os.path.join(USER_HOME, f".{env_identifier}", "bin", "python")
    if os.path.exists(cand2):
        return cand2

    return sys.executable


# --- Job Execution & Management ---
def spawn_job(job):
    """Spawns process for a background job with process group isolation."""
    python_bin = job["python_bin"]
    full_script = job["script"]
    args = job.get("args", [])
    cwd = job.get("cwd", USER_HOME)
    gpu_spec = str(job.get("gpu", "0")).strip().lower()
    log_file = job["log_file"]

    env = os.environ.copy()
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["PYTHONUNBUFFERED"] = "1"
    env["HOME"] = USER_HOME

    if gpu_spec in ["cpu", "none", "-1"]:
        env["CUDA_VISIBLE_DEVICES"] = ""
    elif gpu_spec == "all":
        all_g = get_gpu_info()
        valid_indices = [str(g["index"]) for g in all_g if "index" in g]
        env["CUDA_VISIBLE_DEVICES"] = ",".join(valid_indices) if valid_indices else "0"
    else:
        env["CUDA_VISIBLE_DEVICES"] = gpu_spec

    cmd = [python_bin, full_script]
    if isinstance(args, list):
        cmd.extend([str(a) for a in args])
    elif isinstance(args, str) and args.strip():
        cmd.extend(args.split())

    log_fp = open(log_file, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        cwd=cwd if os.path.isdir(cwd) else USER_HOME,
        env=env,
        preexec_fn=os.setsid
    )
    log_fp.close()

    job["pid"] = proc.pid
    job["proc"] = proc
    job["status"] = "RUNNING"
    job["start_time"] = datetime.now().isoformat()
    return proc


def monitor_jobs():
    """Background daemon to update job statuses and launch queued jobs."""
    while True:
        time.sleep(2)
        changed = False
        queued_jobs = []

        # 1. Update running jobs
        with jobs_lock:
            for jid, job in list(jobs.items()):
                if job.get("status") == "RUNNING":
                    pid = job.get("pid")
                    proc = job.get("proc")
                    if proc is not None:
                        ret = proc.poll()
                        if ret is not None:
                            job["status"] = "COMPLETED" if ret == 0 else "FAILED"
                            job["returncode"] = ret
                            job["end_time"] = datetime.now().isoformat()
                            changed = True
                    elif pid:
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            job["status"] = "UNKNOWN"
                            job["error"] = "Server was restarted while job ran."
                            job["end_time"] = datetime.now().isoformat()
                            changed = True

            for jid, job in jobs.items():
                if job.get("status") == "QUEUED":
                    queued_jobs.append((jid, str(job.get("gpu", "0"))))

        # 2. Process queue
        if queued_jobs:
            gpu_list = get_gpu_info()
            with jobs_lock:
                busy_gpus_in_queue = set()
                for jid, req_gpu in queued_jobs:
                    job = jobs.get(jid)
                    if not job or job.get("status") != "QUEUED":
                        continue

                    target_gpu_spec = str(req_gpu).strip().lower()
                    if target_gpu_spec in ["cpu", "none", "-1"]:
                        try:
                            spawn_job(job)
                            changed = True
                        except Exception as e:
                            job["status"] = "FAILED"
                            job["error"] = str(e)
                            changed = True
                        continue

                    try:
                        target_idx = int(target_gpu_spec)
                    except ValueError:
                        target_idx = 0

                    target_gpu = next((g for g in gpu_list if g.get("index") == target_idx), None)
                    has_running = any(
                        j.get("status") == "RUNNING" and str(j.get("gpu")) == str(target_idx)
                        for j in jobs.values()
                    )

                    if (
                        target_gpu
                        and target_gpu.get("status") == "AVAILABLE"
                        and len(target_gpu.get("processes", [])) == 0
                        and not has_running
                        and str(target_idx) not in busy_gpus_in_queue
                    ):
                        try:
                            spawn_job(job)
                            busy_gpus_in_queue.add(str(target_idx))
                            changed = True
                        except Exception as e:
                            job["status"] = "FAILED"
                            job["error"] = str(e)
                            changed = True

        if changed:
            save_jobs()


threading.Thread(target=monitor_jobs, daemon=True).start()


# --- HTTP Server & API Endpoints ---
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class GPUApiHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        msg = format % args
        msg = re.sub(r'([?&]key=)[^&\s]+', r'\1[REDACTED]', msg)
        msg = re.sub(r'(Bearer\s+)[a-zA-Z0-9_-]+', r'\1[REDACTED]', msg)
        sys.stderr.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} - {msg}\n")

    def send_security_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400, details=None):
        payload = {"success": False, "error": message}
        if details:
            payload["details"] = details
        self.send_json(payload, status=status)

    def get_client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def check_and_authenticate(self, query_params):
        """
        Validates token and enforces progressive lockout against brute-force attacks.
        Returns (is_authenticated, error_message, is_locked_out).
        """
        ip = self.get_client_ip()
        now = time.time()

        with failed_auth_lock:
            ban_until = banned_ips.get(ip, 0)
            if ban_until > now:
                remaining = int(ban_until - now)
                return False, f"IP {ip} temporarily locked out for {remaining}s due to repeated auth failures.", True
            elif ip in banned_ips:
                del banned_ips[ip]

        success = False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if secrets.compare_digest(token, API_KEY):
                success = True
        if not success:
            x_key = self.headers.get("X-API-Key", "").strip()
            if x_key and secrets.compare_digest(x_key, API_KEY):
                success = True
        if not success:
            key_param = query_params.get("key", [None])[0]
            if key_param and secrets.compare_digest(key_param, API_KEY):
                success = True

        if success:
            with failed_auth_lock:
                if ip in failed_auth_attempts:
                    del failed_auth_attempts[ip]
            return True, None, False
        else:
            with failed_auth_lock:
                attempts = [t for t in failed_auth_attempts.get(ip, []) if now - t < FAILURE_WINDOW_SEC]
                attempts.append(now)
                failed_auth_attempts[ip] = attempts
                if len(attempts) >= MAX_FAILED_ATTEMPTS:
                    banned_ips[ip] = now + BAN_DURATION_SEC
                    sys.stderr.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚨 SECURITY ALERT: IP {ip} locked out for {BAN_DURATION_SEC // 60}m after {len(attempts)} failed attempts.\n")
                    return False, f"IP {ip} locked out for {BAN_DURATION_SEC // 60}m due to repeated auth failures.", True
            return False, "Unauthorized: Invalid or missing API key", False

    def authenticate(self, query_params):
        ok, _, _ = self.check_and_authenticate(query_params)
        return ok

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_security_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 1. Root / Documentation: return Agent Protocol & Spec
        if path in ["/", "/api", "/api/docs"]:
            auth_ok, _, _ = self.check_and_authenticate(query)
            self.send_json({
                "service": "AI Agent GPU Compute API",
                "version": "2.0.0",
                "authenticated": auth_ok,
                "auth_instructions": "Send 'Authorization: Bearer <key>' or 'X-API-Key: <key>' or '?key=<key>'",
                "endpoints": {
                    "GET /api/status": "Full system snapshot (GPUs, CPUs, RAM, Disk, Environments, Active Jobs)",
                    "GET /api/gpus": "Live GPU telemetry (utilization, VRAM, temp, active compute apps)",
                    "GET /api/available": "Query free GPU IDs and recommended CUDA device index",
                    "GET /api/envs": "List discovered Python environments (Conda, Venvs, system)",
                    "GET /api/jobs": "List all active, queued, and historical jobs",
                    "GET /api/jobs/<job_id>": "Detailed status and exit info for a specific job",
                    "GET /api/logs/<job_id>?tail=N": "Tail logs for a job",
                    "GET /api/files?path=<dir>": "List directory contents inside home directory",
                    "GET /api/download?path=<file>": "Download a file or artifact",
                    "POST /api/exec": "Synchronously execute a shell command or python snippet (returns exit_code, stdout, stderr)",
                    "POST /api/run": "Asynchronously launch a background script or code job with GPU allocation",
                    "POST /api/stop/<job_id>": "Terminate a running or queued job",
                    "POST /api/upload?dest=<file>": "Upload a script, notebook, or dataset to server"
                }
            })
            return

        auth_ok, auth_err, is_locked = self.check_and_authenticate(query)
        if is_locked:
            self.send_error_json(auth_err, 429)
            return
        if not auth_ok:
            self.send_error_json(auth_err, 401)
            return

        # 2. Comprehensive System Snapshot (One-stop shop for AI Agents)
        if path == "/api/status" or path == "/api/system":
            with jobs_lock:
                active_jobs = [j["id"] for j in jobs.values() if j.get("status") == "RUNNING"]
                queued_jobs = [j["id"] for j in jobs.values() if j.get("status") == "QUEUED"]
            self.send_json({
                "success": True,
                "system": get_system_metrics(),
                "gpus": get_gpu_info(),
                "available_gpus": get_available_gpus(),
                "environments": get_available_environments(),
                "jobs": {
                    "active_count": len(active_jobs),
                    "active_jobs": active_jobs,
                    "queued_count": len(queued_jobs),
                    "queued_jobs": queued_jobs
                }
            })
        elif path == "/api/gpus":
            self.send_json({"success": True, "gpus": get_gpu_info()})
        elif path == "/api/available":
            self.send_json({"success": True, **get_available_gpus()})
        elif path == "/api/envs":
            self.send_json({"success": True, "environments": get_available_environments()})
        elif path == "/api/jobs":
            with jobs_lock:
                clean_jobs = []
                status_filter = query.get("status", [None])[0]
                limit = int(query.get("limit", ["100"])[0])
                for jid, j in jobs.items():
                    if status_filter and j.get("status") != status_filter.upper():
                        continue
                    clean_jobs.append({k: v for k, v in j.items() if k != "proc"})
                clean_jobs.sort(key=lambda x: (x.get("start_time") or x.get("created_time") or ""), reverse=True)
            self.send_json({"success": True, "total": len(clean_jobs), "jobs": clean_jobs[:limit]})
        elif path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):].strip()
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_error_json(f"Job '{job_id}' not found", 404)
                return
            clean_job = {k: v for k, v in job.items() if k != "proc"}
            self.send_json({"success": True, "job": clean_job})
        elif path.startswith("/api/logs/"):
            job_id = path[len("/api/logs/"):].strip()
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_error_json(f"Job '{job_id}' not found", 404)
                return
            log_path = job.get("log_file")
            if not log_path or not os.path.exists(log_path):
                self.send_json({"success": True, "job_id": job_id, "logs": "", "status": job.get("status")})
                return
            tail_lines = query.get("tail", [None])[0]
            try:
                if tail_lines and tail_lines.isdigit():
                    content = tail_file(log_path, int(tail_lines))
                else:
                    if os.path.getsize(log_path) > 1024 * 1024:
                        content = tail_file(log_path, 500)
                    else:
                        with open(log_path, "r", errors="replace") as f:
                            content = f.read()
                self.send_json({
                    "success": True,
                    "job_id": job_id,
                    "status": job.get("status"),
                    "returncode": job.get("returncode"),
                    "logs": content
                })
            except Exception as e:
                self.send_error_json(f"Failed to read logs: {str(e)}", 500)
        elif path == "/api/files":
            target = query.get("path", ["."])[0]
            full_path = normalize_server_path(target)
            if not is_safe_user_path(full_path):
                self.send_error_json("Access denied: path outside home directory", 403)
                return
            if not os.path.exists(full_path) or not os.path.isdir(full_path):
                self.send_error_json(f"Directory '{full_path}' not found", 404)
                return
            try:
                items = []
                for entry in sorted(os.listdir(full_path)):
                    epath = os.path.join(full_path, entry)
                    is_dir = os.path.isdir(epath)
                    items.append({
                        "name": entry,
                        "is_dir": is_dir,
                        "size_bytes": os.path.getsize(epath) if not is_dir else None
                    })
                self.send_json({"success": True, "path": full_path, "items": items})
            except Exception as e:
                self.send_error_json(f"Failed to list directory: {str(e)}", 500)
        elif path == "/api/download":
            target = query.get("path", [None])[0]
            if not target:
                self.send_error_json("Missing 'path' query parameter", 400)
                return
            full_path = normalize_server_path(target)
            if not is_safe_user_path(full_path):
                self.send_error_json("Access denied: path must resolve inside home directory", 403)
                return
            if is_protected_path(full_path):
                self.send_error_json("Access denied: protected file", 403)
                return
            if not os.path.exists(full_path):
                self.send_error_json(f"Path '{full_path}' not found", 404)
                return

            # Directory download: package as tar.gz
            if os.path.isdir(full_path):
                try:
                    tar_buffer = io.BytesIO()
                    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
                        tar.add(full_path, arcname=".")
                    archive_bytes = tar_buffer.getvalue()
                    filename = f"{os.path.basename(full_path) or 'folder'}.tar.gz"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(archive_bytes)))
                    self.send_security_headers()
                    self.end_headers()
                    self.wfile.write(archive_bytes)
                except Exception as e:
                    self.send_error_json(f"Failed to archive folder: {str(e)}", 500)
                return

            # Single file download
            try:
                filename = os.path.basename(full_path)
                file_size = os.path.getsize(full_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(file_size))
                self.send_security_headers()
                self.end_headers()
                with open(full_path, "rb") as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)
            except Exception as e:
                self.send_error_json(f"Download failed: {str(e)}", 500)
        else:
            self.send_error_json("Endpoint not found", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        auth_ok, auth_err, is_locked = self.check_and_authenticate(query)
        if is_locked:
            self.send_error_json(auth_err, 429)
            return
        if not auth_ok:
            self.send_error_json(auth_err, 401)
            return

        content_len = int(self.headers.get("Content-Length", 0))

        # --- 1. Synchronous Execution (for quick diagnostic / CLI commands) ---
        if path == "/api/exec":
            if content_len > MAX_JSON_BODY:
                self.send_error_json("Payload too large", 413)
                return
            post_body = self.rfile.read(content_len) if content_len > 0 else b""
            try:
                payload = json.loads(post_body.decode("utf-8")) if post_body else {}
            except Exception:
                self.send_error_json("Invalid JSON body", 400)
                return

            command = payload.get("command") or payload.get("cmd")
            python_code = payload.get("code")
            cwd = normalize_server_path(payload.get("cwd", USER_HOME))
            env_name = payload.get("env") or "base"
            timeout = min(int(payload.get("timeout", 60)), 300)  # max 5 mins

            if not command and not python_code:
                self.send_error_json("Provide 'command' (shell string) or 'code' (python snippet)", 400)
                return

            if not is_safe_user_path(cwd) or not os.path.isdir(cwd):
                cwd = USER_HOME

            env_vars = os.environ.copy()
            env_vars["HOME"] = USER_HOME
            env_vars["PYTHONUNBUFFERED"] = "1"
            gpu_spec = str(payload.get("gpu", "")).strip().lower()
            if gpu_spec in ["cpu", "none", "-1"]:
                env_vars["CUDA_VISIBLE_DEVICES"] = ""
            elif gpu_spec:
                env_vars["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
                env_vars["CUDA_VISIBLE_DEVICES"] = gpu_spec

            start_t = time.time()
            try:
                if python_code:
                    py_bin = resolve_python_path(env_name)
                    cmd_to_run = [py_bin, "-c", python_code]
                    shell_mode = False
                else:
                    cmd_to_run = command
                    shell_mode = True

                res = subprocess.run(
                    cmd_to_run,
                    shell=shell_mode,
                    cwd=cwd,
                    env=env_vars,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                duration = round(time.time() - start_t, 3)
                self.send_json({
                    "success": True,
                    "exit_code": res.returncode,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "duration_sec": duration
                })
            except subprocess.TimeoutExpired:
                self.send_error_json(f"Command timed out after {timeout} seconds", 408)
            except Exception as e:
                self.send_error_json(f"Execution failed: {str(e)}", 500)
            return

        # --- 2. Asynchronous Job Launch (Training, Long Evaluation) ---
        elif path == "/api/run":
            if content_len > MAX_JSON_BODY:
                self.send_error_json("Payload too large", 413)
                return
            post_body = self.rfile.read(content_len) if content_len > 0 else b""
            try:
                payload = json.loads(post_body.decode("utf-8")) if post_body else {}
            except Exception:
                self.send_error_json("Invalid JSON body", 400)
                return

            script_path = payload.get("script")
            code_snippet = payload.get("code")
            args = payload.get("args", [])
            gpu_choice = str(payload.get("gpu", "auto")).strip().lower()
            env_name = payload.get("env") or payload.get("conda_env", "base")
            cwd = payload.get("cwd", USER_HOME)
            wait_in_queue = bool(payload.get("wait", False))
            force = bool(payload.get("force", False))

            if not script_path and not code_snippet:
                self.send_error_json("Either 'script' (path) or 'code' (raw python string) must be provided", 400)
                return

            if code_snippet:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                script_path = os.path.join(SCRIPTS_DIR, f"agent_job_{timestamp}.py")
                with open(script_path, "w") as f:
                    f.write(code_snippet)

            full_script = normalize_server_path(script_path)
            if not is_safe_user_path(full_script):
                self.send_error_json("Script path must resolve inside home directory", 403)
                return
            if is_protected_path(full_script):
                self.send_error_json("Access denied: cannot execute protected files", 403)
                return
            if not os.path.exists(full_script):
                self.send_error_json(f"Script file '{full_script}' does not exist on server", 404)
                return

            full_cwd = normalize_server_path(cwd)
            if not is_safe_user_path(full_cwd) or not os.path.isdir(full_cwd):
                full_cwd = USER_HOME

            all_gpus = [g for g in get_gpu_info() if "error" not in g]

            if gpu_choice == "auto":
                avail_info = get_available_gpus()
                rec = avail_info.get("recommended_cuda_device")
                if rec is not None:
                    gpu_choice = str(rec)
                elif all_gpus:
                    gpu_choice = "0"
                else:
                    gpu_choice = "cpu"

            is_cpu = gpu_choice in ["cpu", "none", "-1"] or len(all_gpus) == 0

            is_busy = False
            target_gpu = None
            if not is_cpu and gpu_choice not in ["all"]:
                try:
                    target_idx = int(gpu_choice)
                    target_gpu = next((g for g in all_gpus if g.get("index") == target_idx), None)
                    if not target_gpu:
                        self.send_error_json(f"GPU {gpu_choice} not found on this system (found {len(all_gpus)} GPUs)", 404)
                        return
                    procs = target_gpu.get("processes", [])
                    with jobs_lock:
                        has_running_job = any(j.get("status") == "RUNNING" and str(j.get("gpu")) == str(gpu_choice) for j in jobs.values())
                    is_busy = (target_gpu.get("status") != "AVAILABLE") or (len(procs) > 0) or has_running_job
                except ValueError:
                    pass

            if force:
                is_busy = False

            python_bin = resolve_python_path(env_name)
            job_id = f"job_{secrets.token_hex(4)}"
            log_file = os.path.join(LOGS_DIR, f"{job_id}.log")

            job_info = {
                "id": job_id,
                "script": full_script,
                "args": args,
                "gpu": gpu_choice,
                "env": env_name,
                "python_bin": python_bin,
                "cwd": full_cwd,
                "pid": None,
                "status": "QUEUED" if is_busy else "RUNNING",
                "returncode": None,
                "created_time": datetime.now().isoformat(),
                "start_time": None if is_busy else datetime.now().isoformat(),
                "end_time": None,
                "log_file": log_file,
                "proc": None
            }

            if is_busy:
                if not wait_in_queue:
                    procs = target_gpu.get("processes", []) if target_gpu else []
                    mem_used = target_gpu.get("memory_used_mb", 0) if target_gpu else 0
                    users = [p.get("user", "unknown") for p in procs]
                    user_list = ", ".join(sorted(list(set(users)))) if users else "active process"
                    self.send_error_json(
                        f"GPU {gpu_choice} is busy ({mem_used:.0f} MB VRAM used by {user_list}). "
                        f"Set 'wait': true to queue, or 'force': true to override.", 409
                    )
                    return

                try:
                    with open(log_file, "w") as f:
                        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Job {job_id} queued for GPU {gpu_choice}...\n")
                except Exception:
                    pass

                with jobs_lock:
                    jobs[job_id] = job_info
                save_jobs()

                clean_job = {k: v for k, v in job_info.items() if k != "proc"}
                self.send_json({
                    "success": True,
                    "status": "QUEUED",
                    "message": f"GPU {gpu_choice} is busy. Job {job_id} placed in queue.",
                    "job": clean_job
                })
                return

            try:
                spawn_job(job_info)
                with jobs_lock:
                    jobs[job_id] = job_info
                save_jobs()

                clean_job = {k: v for k, v in job_info.items() if k != "proc"}
                self.send_json({
                    "success": True,
                    "status": "SUCCESS",
                    "message": f"Job {job_id} launched on {'CPU' if is_cpu else f'GPU {gpu_choice}'}",
                    "job": clean_job
                })
            except Exception as e:
                self.send_error_json(f"Failed to launch job: {str(e)}", 500)

        # --- 3. Job Termination ---
        elif path.startswith("/api/stop/"):
            job_id = path[len("/api/stop/"):].strip()
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_error_json(f"Job '{job_id}' not found", 404)
                return
            if job.get("status") == "QUEUED":
                job["status"] = "STOPPED"
                job["end_time"] = datetime.now().isoformat()
                save_jobs()
                self.send_json({"success": True, "message": f"Queued job {job_id} cancelled"})
                return
            if job.get("status") != "RUNNING":
                self.send_json({"success": True, "message": f"Job '{job_id}' is already {job.get('status')}"})
                return

            pid = job.get("pid")
            if not pid:
                self.send_error_json("No PID associated with job", 500)
                return

            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass

                job["status"] = "STOPPED"
                job["end_time"] = datetime.now().isoformat()
                save_jobs()
                self.send_json({"success": True, "message": f"Job {job_id} stopped"})
            except ProcessLookupError:
                job["status"] = "STOPPED"
                job["end_time"] = datetime.now().isoformat()
                save_jobs()
                self.send_json({"success": True, "message": f"Process {pid} already terminated"})
            except Exception as e:
                self.send_error_json(f"Failed to stop process: {str(e)}", 500)

        # --- 4. File / Folder Upload ---
        elif path == "/api/upload":
            dest = query.get("dest", [None])[0]
            extract = query.get("extract", ["false"])[0].lower() in ["true", "1", "yes"]
            if not dest:
                self.send_error_json("Missing 'dest' query parameter", 400)
                return
            full_dest = normalize_server_path(dest)
            if not is_safe_user_path(full_dest):
                self.send_error_json("Destination must resolve inside home directory", 403)
                return
            if is_protected_path(full_dest):
                self.send_error_json("Access denied: cannot overwrite protected system files", 403)
                return
            if content_len > MAX_UPLOAD_SIZE:
                self.send_error_json(f"Upload exceeds maximum limit of {MAX_UPLOAD_SIZE // (1024*1024)} MB", 413)
                return

            if extract:
                # Folder upload (tar.gz archive extraction)
                try:
                    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                        tmp_path = tmp.name
                        remaining = content_len
                        while remaining > 0:
                            chunk_size = min(remaining, 65536)
                            chunk = self.rfile.read(chunk_size)
                            if not chunk:
                                break
                            tmp.write(chunk)
                            remaining -= len(chunk)
                    try:
                        with tarfile.open(tmp_path, "r:*") as tar:
                            extracted = safe_extract_tar(tar, full_dest)
                        self.send_json({
                            "success": True,
                            "message": f"Successfully extracted folder ({extracted} items) into {full_dest}",
                            "extracted_files": extracted,
                            "path": full_dest
                        })
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                except Exception as e:
                    self.send_error_json(f"Folder upload extraction failed: {str(e)}", 500)
                return

            # Single file upload
            try:
                os.makedirs(os.path.dirname(full_dest), exist_ok=True)
                bytes_written = 0
                remaining = content_len
                with open(full_dest, "wb") as f:
                    while remaining > 0:
                        chunk_size = min(remaining, 65536)
                        chunk = self.rfile.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                        bytes_written += len(chunk)
                self.send_json({"success": True, "message": f"Uploaded to {full_dest}", "bytes": bytes_written, "path": full_dest})
            except Exception as e:
                self.send_error_json(f"Upload failed: {str(e)}", 500)
        else:
            self.send_error_json("Endpoint not found", 404)


def run_server():
    port = DEFAULT_PORT
    server = None
    try:
        server = ThreadedHTTPServer((HOST, port), GPUApiHandler)
    except OSError:
        sys.stderr.write(f"Port {port} busy, attempting fallback port {FALLBACK_PORT}...\n")
        port = FALLBACK_PORT
        try:
            server = ThreadedHTTPServer((HOST, port), GPUApiHandler)
        except OSError as e:
            sys.stderr.write(f"Error: Could not bind to port {DEFAULT_PORT} or {FALLBACK_PORT}: {e}\n")
            sys.exit(1)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()) + "\n")

    print(json.dumps({
        "status": "ONLINE",
        "service": "AI Agent GPU Compute API",
        "host": HOST,
        "port": port,
        "api_key": API_KEY,
        "pid": os.getpid(),
        "endpoints": f"http://{HOST}:{port}/api/status"
    }, indent=2))
    sys.stdout.flush()

    def handle_sig(sig, frame):
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    server.serve_forever()


if __name__ == "__main__":
    run_server()
