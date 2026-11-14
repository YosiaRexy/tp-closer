#!/usr/bin/env bash
# ============================================================
# TP Auto-Closer - Oracle Free Tier one-paste installer
# Usage (di Oracle Cloud Shell):
#   curl -sSL https://raw.githubusercontent.com/USER/tp-closer/main/setup-oracle.sh | bash
# Atau paste isi file ini ke Cloud Shell.
# ============================================================
set -e

APP_DIR="$HOME/tp-closer"
REPO_URL="${REPO_URL:-https://raw.githubusercontent.com/USER/tp-closer/main}"

echo "==> [1/6] Update apt + install deps"
sudo apt-get update -y >/dev/null
sudo apt-get install -y python3 python3-pip python3-venv curl wget unzip >/dev/null

echo "==> [2/6] Buat folder app"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

echo "==> [3/6] Download bot.py + index.html"
curl -sSL "$REPO_URL/bot.py" -o bot.py
curl -sSL "$REPO_URL/index.html" -o index.html

echo "==> [4/6] Install Python packages (ARM-compatible)"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install fastapi uvicorn[standard] httpx ccxt python-dotenv >/dev/null

echo "==> [5/6] Buat .env template (isi manual)"
if [ ! -f .env ]; then
cat > .env << 'EOF'
# === MT5 via MetaApi (Exness/XM) ===
META_API_TOKEN=PASTE_META_API_TOKEN_HERE
META_ACCOUNT_ID=PASTE_META_ACCOUNT_ID_HERE

# === Bybit Testnet (opsional) ===
BYBIT_API_KEY=
BYBIT_SECRET=
BYBIT_TESTNET=true

# === Bot ===
POLL_INTERVAL_SEC=15
APP_PASSWORD=GANTI_PASSWORD_KAMU
PORT=8000
EOF
echo "   .env dibuat di $APP_DIR/.env  ->  EDIT DULU sebelum start"
fi

echo "==> [6/6] systemd service (auto-start on reboot)"
sudo tee /etc/systemd/system/tp-closer.service >/dev/null << EOF
[Unit]
Description=TP Auto-Closer (EMA 3/8)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable tp-closer >/dev/null 2>&1 || true

echo ""
echo "============================================================"
echo "  INSTALL SELESAI"
echo "============================================================"
echo "  1. Edit config:   nano $APP_DIR/.env"
echo "  2. Start bot:     sudo systemctl start tp-closer"
echo "  3. Cek status:    sudo systemctl status tp-closer"
echo "  4. Log live:      sudo journalctl -u tp-closer -f"
echo "  5. Buka HP link:  cloudflared tunnel --url http://localhost:8000"
echo "============================================================"
echo "  (Opsional) Anti-idle cron (biar Oracle ga reclaim VM):"
echo "  crontab -e  ->  tambah: */30 * * * * /bin/true"
echo "============================================================"
