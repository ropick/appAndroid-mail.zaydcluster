#!/bin/bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/mailpush
CFG_DIR=/etc/mailpush

apt-get install -y -qq python3-venv >/dev/null

mkdir -p "$APP_DIR" "$CFG_DIR"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet imapclient requests google-auth

cp "$SRC_DIR/mailpush.py" "$APP_DIR/"
chmod 700 "$APP_DIR/mailpush.py"

if [ ! -f "$CFG_DIR/config.json" ]; then
    cp "$SRC_DIR/config.example.json" "$CFG_DIR/config.json"
    echo "config baru dibuat di $CFG_DIR/config.json - ISI PASSWORD!"
fi

touch "$CFG_DIR/tokens.json"
echo '{"tokens": []}' > "$CFG_DIR/tokens.json"
chmod 600 "$CFG_DIR"/config.json "$CFG_DIR"/tokens.json

cp "$SRC_DIR/mailpush.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable mailpush >/dev/null

echo "== instalasi selesai =="
echo "1. Edit password:  sudo nano $CFG_DIR/config.json"
echo "2. Jalankan:       sudo systemctl restart mailpush"
echo "3. Cek log:        journalctl -u mailpush -f"
