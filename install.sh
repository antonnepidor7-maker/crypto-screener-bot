#!/bin/bash
set -e

APP_DIR="/opt/crypto-screener-bot"
SERVICE_NAME="crypto-screener"

echo "=== Crypto Screener Bot - VPS Install ==="

# Check args
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "❌ TELEGRAM_BOT_TOKEN not set!"
    echo "Usage: TELEGRAM_BOT_TOKEN='your_token' bash install.sh"
    echo "   or: export TELEGRAM_BOT_TOKEN='your_token' && bash install.sh"
    exit 1
fi

# Install Xray if not present
if ! command -v xray &> /dev/null; then
    echo "[*] Installing Xray..."
    curl -L -o /tmp/xray.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
    unzip -o /tmp/xray.zip -d /usr/local/bin/
    chmod +x /usr/local/bin/xray
    rm /tmp/xray.zip
    echo "[✓] Xray installed"
else
    echo "[✓] Xray already installed"
fi

# Install Python deps
echo "[*] Installing Python dependencies..."
pip3 install --no-cache-dir aiohttp aiohttp-socks Pillow requests PySocks

# Copy app files
echo "[*] Copying app to ${APP_DIR}..."
mkdir -p "$APP_DIR"
cp -r . "$APP_DIR/"
chmod +x "$APP_DIR/install.sh" 2>/dev/null || true

# Create .env file
echo "[*] Creating .env..."
cat > "$APP_DIR/.env" << EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
MARKET_TYPE=${MARKET_TYPE:-both}
EOF
chmod 600 "$APP_DIR/.env"

# Create systemd service for Xray
echo "[*] Creating xray systemd service..."
cat > /etc/systemd/system/xray-screener.service << EOF
[Unit]
Description=Xray Proxy for Crypto Screener
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xray run -c ${APP_DIR}/xray.json
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# Create systemd service for the bot
echo "[*] Creating crypto-screener systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Crypto Screener Telegram Bot
After=network.target xray-screener.service
Wants=xray-screener.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=/usr/bin/python3 ${APP_DIR}/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
echo "[*] Enabling services..."
systemctl daemon-reload
systemctl enable xray-screener
systemctl enable ${SERVICE_NAME}
systemctl restart xray-screener
sleep 2
systemctl restart ${SERVICE_NAME}

echo ""
echo "=== ✅ Install complete! ==="
echo ""
echo "Commands:"
echo "  systemctl status ${SERVICE_NAME}     — check status"
echo "  journalctl -u ${SERVICE_NAME} -f     — view logs"
echo "  systemctl restart ${SERVICE_NAME}     — restart"
echo ""
echo "Admin user: admin / changeme123"
echo "⚠️  Change the admin password immediately!"
echo ""
