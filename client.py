#!/usr/bin/env python3
"""
GPU Remote CLI Client
Runs directly on your laptop to manage jobs and monitor GPUs on the IIITD cluster.
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.parse
import urllib.error
import io
import tarfile
import secrets

DEFAULT_SERVER_URL = os.environ.get("GPU_SERVER_URL", "http://192.168.24.140:8888")
KEY_FILE = os.path.expanduser("~/.gpu_api_key")

CURRENT_SERVER_URL = DEFAULT_SERVER_URL
CURRENT_API_KEY = None

def get_api_key():
    if CURRENT_API_KEY:
        return CURRENT_API_KEY
    if "GPU_API_KEY" in os.environ and os.environ["GPU_API_KEY"].strip():
        return os.environ["GPU_API_KEY"].strip()
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "r") as f:
            k = f.read().strip()
            if k:
                return k
    print("Error: GPU API key not found. Store your key in ~/.gpu_api_key, export GPU_API_KEY, or pass --key.", file=sys.stderr)
    sys.exit(1)

def api_request(endpoint, method="GET", data=None, is_json=True, timeout=None):
    url = f"{CURRENT_SERVER_URL.rstrip('/')}{endpoint}"
    key = get_api_key()
    headers = {
        "Authorization": f"Bearer {key}"
    }
    body = None
    if data is not None:
        if is_json:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        else:
            headers["Content-Type"] = "application/octet-stream"
            body = data

    if timeout is None:
        timeout = 900 if ("/api/upload" in endpoint or "/api/download" in endpoint) else 30

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if "application/json" in res.headers.get("Content-Type", ""):
                return json.loads(res.read().decode("utf-8"))
            return res.read()
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_msg)
            print(f"Error ({e.code}): {err_json.get('error', err_msg)}", file=sys.stderr)
        except Exception:
            print(f"Error ({e.code}): {err_msg}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Failed to connect to GPU server at {url}: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_status(args):
    if getattr(args, "job_id", None):
        data = api_request("/api/jobs")
        jobs = data.get("jobs", [])
        for j in jobs:
            if j.get("id") == args.job_id:
                status_icon = "🟡 RUNNING" if j["status"] == "RUNNING" else ("🟢 COMPLETED" if j["status"] == "COMPLETED" else "🔴 " + j["status"])
                print(f"\nJob ID   : {j['id']}")
                print(f"Status   : {status_icon}")
                print(f"GPU      : {j.get('gpu')}")
                print(f"PID      : {j.get('pid')}")
                print(f"Script   : {j.get('script')}")
                print(f"Started  : {j.get('start_time')}")
                if j.get("end_time"):
                    print(f"Finished : {j.get('end_time')}")
                print(f"Log file : {j.get('log_file')}\n")
                return
        print(f"Job '{args.job_id}' not found.", file=sys.stderr)
        return

    data = api_request("/api/status")
    sys_info = data.get("system", {})
    gpus = data.get("gpus", [])
    hostname = sys_info.get("hostname", CURRENT_SERVER_URL)
    print("\n" + "=" * 65)
    print(f"  ⚡ REMOTE GPU & HOST STATUS ({hostname})")
    print("=" * 65)
    if "ram" in sys_info:
        r = sys_info["ram"]
        print(f"  Host RAM: {r['used_mb']:.0f} / {r['total_mb']:.0f} MB ({r['used_pct']}%) | CPUs: {sys_info.get('cpu_count')}")
    if "disk" in sys_info:
        d = sys_info["disk"]
        print(f"  Host Disk: {d['used_gb']:.1f} / {d['total_gb']:.1f} GB ({d['used_pct']}%) | Free: {d['free_gb']:.1f} GB")

    if not gpus:
        print("\n  Hardware: CPU-Only Mode (No NVIDIA GPUs detected)")
    for g in gpus:
        if "error" in g:
            print(f"\nGPU Query Error: {g['error']}")
            continue
        status_icon = "🟢 AVAILABLE" if g["status"] == "AVAILABLE" else "🔴 BUSY"
        print(f"\n[GPU {g['index']}] {g['name']}  -->  {status_icon}")
        print(f"  VRAM: {g['memory_used_mb']:.0f} / {g['memory_total_mb']:.0f} MB ({g['memory_used_pct']}%) | Free: {g['memory_free_gb']} GB")
        print(f"  Compute Util: {g['utilization_pct']}% | Temp: {g['temperature_c']}°C | Power: {g['power_w']}W")
        procs = g.get("processes", [])
        if procs:
            print("  Active compute processes:")
            for p in procs:
                print(f"    - User: {p['user']} (PID {p['pid']}) | VRAM: {p['memory_used_mb']} MB")
        else:
            print("  Active compute processes: None (Idle)")
    print("=" * 65 + "\n")

def cmd_available(args):
    data = api_request("/api/available")
    avail = data.get("available_gpus", [])
    rec = data.get("recommended_cuda_device")
    if avail and rec is not None:
        print(f"🟢 Available GPUs: {avail}")
        print(f"👉 Recommended CUDA_VISIBLE_DEVICES={rec}")
    else:
        print("🔴 All GPUs currently busy.")
        print("👉 Use --wait (-w) with 'gpu run' to queue until a GPU becomes free.")

def cmd_exec(args):
    payload = {
        "command": args.command,
        "env": getattr(args, "env", "base"),
        "gpu": getattr(args, "gpu", "")
    }
    res = api_request("/api/exec", method="POST", data=payload)
    if res.get("stdout"):
        sys.stdout.write(res["stdout"])
    if res.get("stderr"):
        sys.stderr.write(res["stderr"])
    sys.exit(res.get("exit_code", 0))

def cmd_run(args):
    payload = {
        "script": args.script,
        "gpu": args.gpu,
        "env": args.env,
        "args": args.args if args.args else [],
        "wait": getattr(args, "wait", False),
        "force": getattr(args, "force", False)
    }
    res = api_request("/api/run", method="POST", data=payload)
    job = res.get("job", {})

    if res.get("status") == "QUEUED":
        print(f"\n⏳ {res.get('message')}")
        print(f"   Job ID : {job.get('id')}")
        print(f"   GPU    : {job.get('gpu')}")
        print(f"   Script : {job.get('script')}")
        if getattr(args, "wait", False):
            import time
            print(f"\nWaiting for GPU {job.get('gpu')} to become free (Press Ctrl+C to detach wait)...")
            try:
                while True:
                    time.sleep(2)
                    jres = api_request("/api/jobs")
                    current_job = next((j for j in jres.get("jobs", []) if j.get("id") == job.get("id")), None)
                    if current_job and current_job.get("status") != "QUEUED":
                        print(f"\n🚀 GPU {job.get('gpu')} is now free! Job {job.get('id')} has started (PID: {current_job.get('pid')})!")
                        print(f"👉 View live logs with: gpu logs {job.get('id')} -f\n")
                        break
            except KeyboardInterrupt:
                print(f"\n\nDetached. Job {job.get('id')} remains queued on cluster and will start when GPU {job.get('gpu')} is free.")
                print(f"Check status anytime with: gpu status {job.get('id')}\n")
        return

    print(f"\n✅ Job successfully launched on cluster!")
    print(f"   Job ID : {job.get('id')}")
    print(f"   GPU    : {job.get('gpu')}")
    print(f"   PID    : {job.get('pid')}")
    print(f"   Env    : {job.get('conda_env')}")
    print(f"   Script : {job.get('script')}")
    print(f"\n👉 View logs with: gpu logs {job.get('id')} -f\n")

def cmd_jobs(args):
    data = api_request("/api/jobs")
    jobs = data.get("jobs", [])
    if not jobs:
        print("No jobs recorded.")
        return
    print("\n" + "-" * 75)
    print(f"{'JOB ID':<12} {'GPU':<6} {'STATUS':<12} {'STARTED':<20} {'SCRIPT'}")
    print("-" * 75)
    for j in jobs:
        started = j.get("start_time") or j.get("created_time") or ""
        if "T" in started:
            started = started.split("T")[1][:8]
        if j.get("status") == "QUEUED":
            started = f"Queued {started}"
        script_name = os.path.basename(j.get("script", ""))
        print(f"{j.get('id'):<12} {j.get('gpu'):<6} {j.get('status'):<12} {started:<20} {script_name}")
    print("-" * 75 + "\n")

def cmd_logs(args):
    if not getattr(args, "follow", False):
        url = f"/api/logs/{args.job_id}"
        if args.tail:
            url += f"?tail={args.tail}"
        res = api_request(url)
        logs = res.get("logs", "")
        print(f"\n--- Logs for {args.job_id} ({res.get('status', 'UNKNOWN')}) ---")
        print(logs.strip())
        print("--- End of logs ---\n")
        return

    import time
    print(f"\n📡 Streaming live logs for {args.job_id} (Press Ctrl+C to stop)...")
    print("-" * 65)
    printed_len = 0
    try:
        while True:
            res = api_request(f"/api/logs/{args.job_id}")
            logs = res.get("logs", "")
            status = res.get("status", "UNKNOWN")
            if len(logs) > printed_len:
                sys.stdout.write(logs[printed_len:])
                sys.stdout.flush()
                printed_len = len(logs)
            if status != "RUNNING":
                print("-" * 65)
                print(f"🏁 Job finished with status: {status}\n")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n--- Stopped following logs ---")

def cmd_stop(args):
    res = api_request(f"/api/stop/{args.job_id}", method="POST")
    print(res.get("message", "Job stopped."))

def cmd_download(args):
    remote_path = args.remote_path
    local_path = args.local_path or os.path.basename(remote_path.rstrip("/")) or "download"
    url = f"/api/download?path={urllib.parse.quote(remote_path)}"
    print(f"Downloading {remote_path} to {local_path}...")
    content = api_request(url, method="GET")

    # If it's a tar.gz archive of a remote directory, extract it
    if isinstance(content, bytes) and content.startswith(b"\x1f\x8b") and getattr(args, "extract", False):
        buffer = io.BytesIO(content)
        with tarfile.open(fileobj=buffer, mode="r:gz") as tar:
            os.makedirs(local_path, exist_ok=True)
            tar.extractall(path=local_path)
        print(f"✅ Extracted folder to {local_path}")
        return

    dest_dir = os.path.dirname(local_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(content)
    print(f"✅ Downloaded ({len(content)} bytes) to {local_path}")

def cmd_upload(args):
    local_path = os.path.abspath(os.path.expanduser(args.local_path))
    remote_path = args.remote_path
    if not os.path.exists(local_path):
        print(f"Error: Local path '{local_path}' does not exist", file=sys.stderr)
        sys.exit(1)

    if os.path.isdir(local_path):
        print(f"📦 Packaging directory {local_path}...")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            tar.add(local_path, arcname=".")
        archive_bytes = buffer.getvalue()
        url = f"/api/upload?dest={urllib.parse.quote(remote_path)}&extract=true"
        print(f"🚀 Pushing folder ({len(archive_bytes) / 1024:.1f} KB) to remote {remote_path}...")
        res = api_request(url, method="POST", data=archive_bytes, is_json=False)
        print(f"✅ {res.get('message', 'Folder pushed successfully')}")
    else:
        with open(local_path, "rb") as f:
            file_bytes = f.read()
        url = f"/api/upload?dest={urllib.parse.quote(remote_path)}"
        print(f"Uploading {local_path} ({len(file_bytes)} bytes) to {remote_path}...")
        res = api_request(url, method="POST", data=file_bytes, is_json=False)
        print(f"✅ {res.get('message', 'File uploaded successfully')}")

def cmd_keygen(args):
    new_key = secrets.token_hex(32)
    print("\n" + "=" * 65)
    print("  🔑 Generated 256-Bit Cryptographic Secret Key")
    print("=" * 65)
    print(f"\n  {new_key}\n")
    print("  On Server:")
    print(f"    echo \"{new_key}\" > ~/.gpu_api_key && chmod 600 ~/.gpu_api_key")
    print("\n  On Local / Client:")
    print(f"    export GPU_API_KEY=\"{new_key}\"")
    print("=" * 65 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Remote GPU and AI Agent CLI Client")
    parser.add_argument("--server", "-s", default=None, help="Remote server URL (overrides GPU_SERVER_URL)")
    parser.add_argument("--key", "-k", default=None, help="API Key (overrides GPU_API_KEY)")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # keygen
    p_keygen = subparsers.add_parser("keygen", help="Generate a cryptographically secure 256-bit API secret key")
    p_keygen.set_defaults(func=cmd_keygen)

    # status
    p_status = subparsers.add_parser("status", help="Show system snapshot, GPU utilization, or job status")
    p_status.add_argument("job_id", nargs="?", default=None, help="Optional job ID to check status for")
    p_status.set_defaults(func=cmd_status)

    # available
    p_avail = subparsers.add_parser("available", help="Check available GPUs and recommended device")
    p_avail.set_defaults(func=cmd_available)

    # exec (synchronous command)
    p_exec = subparsers.add_parser("exec", help="Run a quick synchronous shell command on server")
    p_exec.add_argument("command", help="Command string to run (e.g. 'nvidia-smi' or 'pip list')")
    p_exec.add_argument("--env", default="base", help="Python environment name or path")
    p_exec.add_argument("--gpu", default="", help="Optional CUDA_VISIBLE_DEVICES")
    p_exec.set_defaults(func=cmd_exec)

    # run (asynchronous background job)
    p_run = subparsers.add_parser("run", help="Run a Python script on the server")
    p_run.add_argument("script", help="Path to script on server (e.g. ~/train.py)")
    p_run.add_argument("--gpu", default="auto", help="GPU assignment: 'auto', '0', '1', 'all', or 'cpu' (default: auto)")
    p_run.add_argument("--env", default="base", help="Python environment name or path (default: base)")
    p_run.add_argument("--args", default="", help="Arguments string to pass to script (e.g. '--epochs 10')")
    p_run.add_argument("-w", "--wait", action="store_true", help="If chosen GPU is busy, queue and automatically start when it becomes free")
    p_run.add_argument("--force", action="store_true", help="Force launch even if GPU is busy")
    p_run.set_defaults(func=cmd_run)

    # jobs
    p_jobs = subparsers.add_parser("jobs", help="List all jobs")
    p_jobs.set_defaults(func=cmd_jobs)

    # logs
    p_logs = subparsers.add_parser("logs", help="View or stream live logs for a job")
    p_logs.add_argument("job_id", help="Job ID (e.g. job_abc123)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Stream live logs in real-time (like tail -f)")
    p_logs.add_argument("--tail", type=int, default=100, help="Number of lines to tail when not following")
    p_logs.set_defaults(func=cmd_logs)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop a running job")
    p_stop.add_argument("job_id", help="Job ID to stop")
    p_stop.set_defaults(func=cmd_stop)

    # download / pull
    p_dl = subparsers.add_parser("download", help="Download a file or folder from server")
    p_dl.add_argument("remote_path", help="Path to file or directory on server")
    p_dl.add_argument("local_path", nargs="?", help="Local path destination")
    p_dl.add_argument("-x", "--extract", action="store_true", help="Extract if downloading a directory archive")
    p_dl.set_defaults(func=cmd_download)

    # upload / push
    p_ul = subparsers.add_parser("upload", help="Upload a file or folder to server")
    p_ul.add_argument("local_path", help="Local file or directory to upload")
    p_ul.add_argument("remote_path", help="Destination path on server (inside ~/)")
    p_ul.set_defaults(func=cmd_upload)

    # aliases: push and pull
    p_push = subparsers.add_parser("push", help="Push a file or folder to the server")
    p_push.add_argument("local_path", help="Local file or directory to push")
    p_push.add_argument("remote_path", help="Destination path on server")
    p_push.set_defaults(func=cmd_upload)

    p_pull = subparsers.add_parser("pull", help="Pull a file or folder from the server")
    p_pull.add_argument("remote_path", help="Remote path to pull")
    p_pull.add_argument("local_path", nargs="?", help="Local destination path")
    p_pull.add_argument("-x", "--extract", action="store_true", default=True, help="Extract folder archive")
    p_pull.set_defaults(func=cmd_download)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    global CURRENT_SERVER_URL, CURRENT_API_KEY
    if args.server:
        CURRENT_SERVER_URL = args.server
    if args.key:
        CURRENT_API_KEY = args.key

    args.func(args)

if __name__ == "__main__":
    main()
