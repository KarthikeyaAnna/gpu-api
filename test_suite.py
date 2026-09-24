#!/usr/bin/env python3
"""
Comprehensive Local Test Suite for AI Agent GPU API & Client
"""

import os
import sys
import time
import json
import socket
import shutil
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import tarfile
import io

TEST_PORT = 8899
TEST_KEY = "test_agent_secret_key_9876543210"
SERVER_URL = f"http://127.0.0.1:{TEST_PORT}"
SANDBOX_DIR = os.path.expanduser("~/test_gpu_sandbox")
LOCAL_TEMP_DIR = "/home/karthik/.gemini/antigravity-ide/scratch/gpu_api_test_tmp"

os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)


def log(msg, status="INFO"):
    icons = {"INFO": "ℹ️ ", "PASS": "✅", "FAIL": "❌", "WARN": "⚠️ "}
    print(f"{icons.get(status, '')} [{status}] {msg}")


def http_req(endpoint, method="GET", data=None, headers=None, expect_status=200):
    url = f"{SERVER_URL}{endpoint}"
    hdrs = headers or {}
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            hdrs["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        elif isinstance(data, bytes):
            body = data
        elif isinstance(data, str):
            body = data.encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "application/json" in content_type:
                return resp.status, json.loads(raw.decode("utf-8"))
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw.decode("utf-8", errors="ignore")


def run_tests():
    passed = 0
    failed = 0

    log("Starting local test suite for GPU Server and Client...")

    # 1. Start Server Process
    env = os.environ.copy()
    env["GPU_SERVER_PORT"] = str(TEST_PORT)
    env["GPU_API_KEY"] = TEST_KEY
    env["GPU_SERVER_HOST"] = "127.0.0.1"

    server_script = "/home/karthik/UDic/gpu_api/gpu_server.py"
    client_script = "/home/karthik/UDic/gpu_api/client.py"

    log(f"Launching server on {SERVER_URL}...")
    server_proc = subprocess.Popen(
        [sys.executable, server_script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # Wait for server port to open
    ready = False
    for _ in range(30):
        time.sleep(0.2)
        s = socket.socket()
        if s.connect_ex(("127.0.0.1", TEST_PORT)) == 0:
            ready = True
            s.close()
            break
        s.close()

    if not ready:
        log("Server failed to start in time!", "FAIL")
        server_proc.kill()
        sys.exit(1)

    log("Server is running and accepting connections!", "PASS")

    auth_header = {"Authorization": f"Bearer {TEST_KEY}"}

    try:
        # TEST 1: Unauthenticated request should fail with 401
        log("Test 1: Authentication Enforcement...")
        code, resp = http_req("/api/status")
        if code == 401:
            log("Unauthenticated request correctly returned 401", "PASS")
            passed += 1
        else:
            log(f"Expected 401, got {code}: {resp}", "FAIL")
            failed += 1

        # TEST 2: Invalid Key should fail with 401
        code, resp = http_req("/api/status", headers={"Authorization": "Bearer wrong_key"})
        if code == 401:
            log("Invalid key correctly returned 401", "PASS")
            passed += 1
        else:
            log(f"Expected 401, got {code}: {resp}", "FAIL")
            failed += 1

        # TEST 3: Valid Auth via Bearer header
        code, resp = http_req("/api/status", headers=auth_header)
        if code == 200 and resp.get("success"):
            log("Valid Bearer auth succeeded with 200", "PASS")
            passed += 1
        else:
            log(f"Failed valid auth: {code} {resp}", "FAIL")
            failed += 1

        # TEST 4: Query parameter auth (?key=...)
        code, resp = http_req(f"/api/status?key={TEST_KEY}")
        if code == 200 and resp.get("success"):
            log("Query parameter auth (?key=...) succeeded", "PASS")
            passed += 1
        else:
            log(f"Failed ?key= auth: {code} {resp}", "FAIL")
            failed += 1

        # TEST 5: System Status Schema
        log("Test 5: System Status Schema Inspection...")
        sys_info = resp.get("system", {})
        if "hostname" in sys_info and "cpu_count" in sys_info and "ram" in sys_info:
            log(f"System metrics detected: {sys_info['hostname']}, {sys_info['cpu_count']} CPUs, {sys_info['ram']['total_mb']} MB RAM", "PASS")
            passed += 1
        else:
            log(f"Incomplete system metrics: {sys_info}", "FAIL")
            failed += 1

        # TEST 6: GPU Info and Envs
        code, gpus_resp = http_req("/api/gpus", headers=auth_header)
        code2, envs_resp = http_req("/api/envs", headers=auth_header)
        if code == 200 and "gpus" in gpus_resp and code2 == 200 and "environments" in envs_resp:
            log(f"GPUs detected: {len(gpus_resp['gpus'])}, Environments discovered: {list(envs_resp['environments'].keys())[:4]}", "PASS")
            passed += 1
        else:
            log("Failed /api/gpus or /api/envs", "FAIL")
            failed += 1

        # TEST 7: Synchronous Exec (Shell Command)
        log("Test 7: POST /api/exec (Shell command)...")
        code, exec_resp = http_req("/api/exec", method="POST", data={"command": "echo 'AGENT_EXEC_OK'"}, headers=auth_header)
        if code == 200 and exec_resp.get("exit_code") == 0 and "AGENT_EXEC_OK" in exec_resp.get("stdout", ""):
            log("Synchronous shell command executed successfully", "PASS")
            passed += 1
        else:
            log(f"Shell command exec failed: {code} {exec_resp}", "FAIL")
            failed += 1

        # TEST 8: Synchronous Exec (Python Code Snippet)
        log("Test 8: POST /api/exec (Python snippet)...")
        code, py_resp = http_req("/api/exec", method="POST", data={"code": "import math; print(math.factorial(6))"}, headers=auth_header)
        if code == 200 and py_resp.get("exit_code") == 0 and "720" in py_resp.get("stdout", ""):
            log("Synchronous Python code snippet executed successfully (720)", "PASS")
            passed += 1
        else:
            log(f"Python snippet exec failed: {code} {py_resp}", "FAIL")
            failed += 1

        # TEST 9: Single File Upload & Download
        log("Test 9: Single File Upload & Download...")
        test_file_content = b"AI Agent Remote API Test File Contents\nTimestamp: " + str(time.time()).encode("utf-8")
        code, up_resp = http_req(
            "/api/upload?dest=~/test_gpu_sandbox/hello.txt",
            method="POST",
            data=test_file_content,
            headers={"Authorization": f"Bearer {TEST_KEY}", "Content-Type": "application/octet-stream"}
        )
        if code == 200 and up_resp.get("success"):
            log("File upload succeeded", "PASS")
            passed += 1
        else:
            log(f"File upload failed: {code} {up_resp}", "FAIL")
            failed += 1

        code, down_content = http_req("/api/download?path=~/test_gpu_sandbox/hello.txt", headers=auth_header)
        if code == 200 and down_content == test_file_content:
            log("File download succeeded and content verified", "PASS")
            passed += 1
        else:
            log("File download content mismatch", "FAIL")
            failed += 1

        # TEST 10: Directory Listing (/api/files)
        log("Test 10: GET /api/files (Directory listing)...")
        code, list_resp = http_req("/api/files?path=~/test_gpu_sandbox", headers=auth_header)
        if code == 200 and any(item["name"] == "hello.txt" for item in list_resp.get("items", [])):
            log("Directory listing successfully confirmed hello.txt", "PASS")
            passed += 1
        else:
            log(f"Directory listing failed: {code} {list_resp}", "FAIL")
            failed += 1

        # TEST 11: Folder Push (Packaging & Extraction via extract=true)
        log("Test 11: Folder Push & Safe Archive Extraction...")
        local_pkg = os.path.join(LOCAL_TEMP_DIR, "sample_project")
        os.makedirs(os.path.join(local_pkg, "models"), exist_ok=True)
        with open(os.path.join(local_pkg, "train.py"), "w") as f:
            f.write("print('Model training initialized!')\n")
        with open(os.path.join(local_pkg, "models", "config.json"), "w") as f:
            f.write('{"lr": 0.001, "batch_size": 32}\n')

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            tar.add(local_pkg, arcname=".")
        tar_bytes = tar_buf.getvalue()

        code, push_resp = http_req(
            "/api/upload?dest=~/test_gpu_sandbox/sample_project&extract=true",
            method="POST",
            data=tar_bytes,
            headers={"Authorization": f"Bearer {TEST_KEY}", "Content-Type": "application/octet-stream"}
        )
        if code == 200 and push_resp.get("success") and push_resp.get("extracted_files", 0) >= 2:
            log(f"Folder push succeeded: {push_resp['extracted_files']} items extracted", "PASS")
            passed += 1
        else:
            log(f"Folder push extraction failed: {code} {push_resp}", "FAIL")
            failed += 1

        # Verify extracted files on disk
        train_file_path = os.path.expanduser("~/test_gpu_sandbox/sample_project/train.py")
        if os.path.exists(train_file_path):
            log("Verified extracted file train.py exists on server filesystem", "PASS")
            passed += 1
        else:
            log("Extracted file train.py not found on disk", "FAIL")
            failed += 1

        # TEST 12: Folder Pull (Directory download as tar.gz)
        log("Test 12: Folder Pull (Download directory as archive)...")
        code, pulled_tar = http_req("/api/download?path=~/test_gpu_sandbox/sample_project", headers=auth_header)
        if code == 200 and isinstance(pulled_tar, bytes) and pulled_tar.startswith(b"\x1f\x8b"):
            # Unpack in local temp
            pulled_extract_dir = os.path.join(LOCAL_TEMP_DIR, "pulled_sample_project")
            os.makedirs(pulled_extract_dir, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(pulled_tar), mode="r:gz") as tar:
                tar.extractall(path=pulled_extract_dir)
            log("Folder successfully pulled and extracted locally", "PASS")
            passed += 1
        else:
            log(f"Folder pull failed: status {code}", "FAIL")
            failed += 1

        # TEST 13: Asynchronous Job Run & Log Streaming
        log("Test 13: Asynchronous Job Execution (POST /api/run)...")
        job_code = """
import time
print("BATCH_1_DONE")
time.sleep(0.5)
print("BATCH_2_DONE")
"""
        code, run_resp = http_req(
            "/api/run",
            method="POST",
            data={"code": job_code, "gpu": "cpu"},
            headers=auth_header
        )
        if code == 200 and run_resp.get("status") == "SUCCESS":
            job_id = run_resp["job"]["id"]
            log(f"Launched job {job_id} successfully", "PASS")
            passed += 1

            # Poll for completion
            completed = False
            for _ in range(25):
                time.sleep(0.3)
                _, j_info = http_req(f"/api/jobs/{job_id}", headers=auth_header)
                if j_info.get("job", {}).get("status") == "COMPLETED":
                    completed = True
                    break

            if completed:
                log(f"Job {job_id} finished with status COMPLETED", "PASS")
                passed += 1
            else:
                log(f"Job {job_id} did not complete in time: {j_info}", "FAIL")
                failed += 1

            # Check logs
            _, logs_resp = http_req(f"/api/logs/{job_id}?tail=20", headers=auth_header)
            if "BATCH_1_DONE" in logs_resp.get("logs", "") and "BATCH_2_DONE" in logs_resp.get("logs", ""):
                log("Job logs verified with correct stdout stream", "PASS")
                passed += 1
            else:
                log(f"Logs missing expected output: {logs_resp}", "FAIL")
                failed += 1
        else:
            log(f"Failed to launch job: {code} {run_resp}", "FAIL")
            failed += 1

        # TEST 14: Job Cancellation (POST /api/stop/<job_id>)
        log("Test 14: Job Cancellation (POST /api/stop)...")
        code, run_resp = http_req(
            "/api/run",
            method="POST",
            data={"code": "import time\ntime.sleep(60)\n", "gpu": "cpu"},
            headers=auth_header
        )
        if code == 200:
            long_job_id = run_resp["job"]["id"]
            time.sleep(0.5)
            code, stop_resp = http_req(f"/api/stop/{long_job_id}", method="POST", headers=auth_header)
            if code == 200 and stop_resp.get("success"):
                log(f"Job {long_job_id} cancellation acknowledged", "PASS")
                passed += 1
            else:
                log(f"Job cancellation failed: {code} {stop_resp}", "FAIL")
                failed += 1
        else:
            log(f"Failed to start job for cancellation test: {code} {run_resp}", "FAIL")
            failed += 1

        # TEST 15: Client CLI Testing
        log("Test 15: CLI Client (client.py)...")
        cmd_status = [
            sys.executable, client_script,
            "--server", SERVER_URL,
            "--key", TEST_KEY,
            "status"
        ]
        res = subprocess.run(cmd_status, capture_output=True, text=True)
        if res.returncode == 0 and "REMOTE GPU & HOST STATUS" in res.stdout:
            log("CLI 'client.py status' succeeded", "PASS")
            passed += 1
        else:
            log(f"CLI status failed: {res.stderr or res.stdout}", "FAIL")
            failed += 1

        # CLI push folder
        cli_push_dest = "~/test_gpu_sandbox/cli_pushed"
        cmd_push = [
            sys.executable, client_script,
            "--server", SERVER_URL,
            "--key", TEST_KEY,
            "push", local_pkg, cli_push_dest
        ]
        res_push = subprocess.run(cmd_push, capture_output=True, text=True)
        if res_push.returncode == 0 and ("Successfully extracted" in res_push.stdout or "Folder pushed successfully" in res_push.stdout):
            log("CLI 'client.py push <dir>' succeeded", "PASS")
            passed += 1
        else:
            log(f"CLI push failed: {res_push.stderr or res_push.stdout}", "FAIL")
            failed += 1

        # CLI pull folder
        cli_pulled_dest = os.path.join(LOCAL_TEMP_DIR, "cli_pulled")
        cmd_pull = [
            sys.executable, client_script,
            "--server", SERVER_URL,
            "--key", TEST_KEY,
            "pull", cli_push_dest, cli_pulled_dest
        ]
        res_pull = subprocess.run(cmd_pull, capture_output=True, text=True)
        if res_pull.returncode == 0 and os.path.exists(os.path.join(cli_pulled_dest, "train.py")):
            log("CLI 'client.py pull <dir>' succeeded and extracted", "PASS")
            passed += 1
        else:
            log(f"CLI pull failed: {res_pull.stderr or res_pull.stdout}", "FAIL")
            failed += 1

        # CLI exec
        cmd_cli_exec = [
            sys.executable, client_script,
            "--server", SERVER_URL,
            "--key", TEST_KEY,
            "exec", "echo 'CLI_EXEC_VERIFIED'"
        ]
        res_exec = subprocess.run(cmd_cli_exec, capture_output=True, text=True)
        if res_exec.returncode == 0 and "CLI_EXEC_VERIFIED" in res_exec.stdout:
            log("CLI 'client.py exec' succeeded", "PASS")
            passed += 1
        else:
            log(f"CLI exec failed: {res_exec.stderr or res_exec.stdout}", "FAIL")
            failed += 1

        # TEST 16: Anti-Brute-Force & Lockout Defense
        log("Test 16: Brute-Force Defense & IP Lockout (HTTP 429)...")
        got_429 = False
        for i in range(7):
            c, r = http_req("/api/status", headers={"Authorization": f"Bearer fake_attempt_{i}"})
            if c == 429:
                got_429 = True
                break
        if got_429:
            log("Brute-force attack successfully detected & IP locked out (HTTP 429)", "PASS")
            passed += 1
        else:
            log(f"Expected HTTP 429 lockout, got status {c}", "FAIL")
            failed += 1

    finally:
        # Shutdown server
        log("Shutting down test server...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server_proc.kill()

        # Clean sandbox
        if os.path.exists(SANDBOX_DIR):
            shutil.rmtree(SANDBOX_DIR, ignore_errors=True)
        if os.path.exists(LOCAL_TEMP_DIR):
            shutil.rmtree(LOCAL_TEMP_DIR, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"  TEST RESULTS: {passed} PASSED, {failed} FAILED")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
