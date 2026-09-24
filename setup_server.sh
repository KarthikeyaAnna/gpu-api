#!/usr/bin/env bash
# ==============================================================================
# AI Agent GPU API Daemon Setup Script (AWS EC2 / Cloud / Remote Host)
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/gpu-api"
LOGS_DIR="${TARGET_DIR}/logs"
SCRIPTS_DIR="${TARGET_DIR}/scripts"
KEY_FILE="${HOME}/.gpu_api_key"
SERVICE_NAME="gpu-server"
PYTHON_BIN="$(which python3 || echo "/usr/bin/python3")"

echo "================================================================"
echo "⚡ Deploying AI Agent GPU Compute Daemon for ${USER}"
echo "================================================================"

# 1. Create directory structure
mkdir -p "${LOGS_DIR}" "${SCRIPTS_DIR}"
echo "✓ Directories initialized at ${TARGET_DIR}"

# 2. Install server script
if [ -f "${DIR}/gpu_server.py" ]; then
  cp "${DIR}/gpu_server.py" "${TARGET_DIR}/gpu_server.py"
else
  echo "Downloading gpu_server.py from GitHub..."
  curl -sSL "https://raw.githubusercontent.com/KarthikeyaAnna/gpu-api/main/gpu_server.py" -o "${TARGET_DIR}/gpu_server.py"
fi
chmod +x "${TARGET_DIR}/gpu_server.py"
echo "✓ Installed ${TARGET_DIR}/gpu_server.py"

# 3. Ensure API key exists
if [ ! -f "${KEY_FILE}" ] || [ ! -s "${KEY_FILE}" ]; then
  python3 -c "import secrets; print(secrets.token_hex(32))" > "${KEY_FILE}"
  chmod 600 "${KEY_FILE}"
  echo "✓ Generated new 256-bit Secret Key in ${KEY_FILE}"
else
  echo "✓ Using existing API Key from ${KEY_FILE}"
fi
API_KEY="$(cat "${KEY_FILE}" | tr -d '[:space:]')"

# 4. Configure systemd service if root/sudo available
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
  cat <<EOF > /etc/systemd/system/${SERVICE_NAME}.service
[Unit]
Description=AI Agent GPU Compute API Daemon
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${HOME}
ExecStart=${PYTHON_BIN} ${TARGET_DIR}/gpu_server.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=GPU_SERVER_PORT=8888

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable ${SERVICE_NAME}
  systemctl restart ${SERVICE_NAME}
  echo "✓ Systemd service installed & started (${SERVICE_NAME}.service)"
elif command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
  mkdir -p "${HOME}/.config/systemd/user"
  cat <<EOF > "${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
[Unit]
Description=AI Agent GPU Compute API Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=${HOME}
ExecStart=${PYTHON_BIN} ${TARGET_DIR}/gpu_server.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=GPU_SERVER_PORT=8888

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable ${SERVICE_NAME}
  systemctl --user restart ${SERVICE_NAME}
  echo "✓ User systemd service installed & started (${SERVICE_NAME}.service)"
else
  # Background daemon fallback
  PID_FILE="${TARGET_DIR}/server.pid"
  if [ -f "${PID_FILE}" ]; then
    OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
      echo "Stopping existing instance (PID ${OLD_PID})..."
      kill "${OLD_PID}" || true
      sleep 1
    fi
  fi
  nohup "${PYTHON_BIN}" "${TARGET_DIR}/gpu_server.py" > "${TARGET_DIR}/server.log" 2>&1 &
  echo "✓ Started in background with nohup (PID $!)"
fi

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")"

echo ""
echo "================================================================"
echo "🎉 AI Agent GPU API Daemon is Live!"
echo "================================================================"
echo "Host        : $(hostname)"
echo "Internal IP : ${LOCAL_IP}"
echo "Port        : 8888"
echo "API Key     : ${API_KEY}"
echo ""
echo "----------------------------------------------------------------"
echo "👉 Connect from your local AI agent or terminal:"
echo "----------------------------------------------------------------"
echo "Option A (SSH Tunnel - Recommended for AWS EC2):"
echo "  ssh -N -L 8888:localhost:8888 -i ~/.ssh/your-aws-key.pem ${USER}@<AWS_PUBLIC_IP>"
echo ""
echo "  Then your AI agent can query:"
echo "  curl -H \"Authorization: Bearer ${API_KEY}\" http://localhost:8888/api/status"
echo ""
echo "Option B (Direct via Public IP if Port 8888 open in Security Group):"
echo "  curl -H \"Authorization: Bearer ${API_KEY}\" http://<AWS_PUBLIC_IP>:8888/api/status"
echo ""
echo "----------------------------------------------------------------"
echo "🤖 Quick Test Commands for AI Agents:"
echo "----------------------------------------------------------------"
echo "# 1. Run a quick sync command:"
echo "curl -X POST -H \"Authorization: Bearer ${API_KEY}\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -d '{\"command\": \"nvidia-smi\"}' http://localhost:8888/api/exec"
echo ""
echo "# 2. Run Python code asynchronously on GPU:"
echo "curl -X POST -H \"Authorization: Bearer ${API_KEY}\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -d '{\"code\": \"import torch; print(torch.cuda.is_available())\", \"gpu\": \"auto\"}' \\"
echo "  http://localhost:8888/api/run"
echo "================================================================"
