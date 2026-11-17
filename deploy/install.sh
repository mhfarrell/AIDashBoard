#!/bin/sh
# Install the dashboard on a Raspberry Pi (any model, Raspberry Pi OS / Debian).
# Usage (on the Pi, from the copied project folder):  sudo sh deploy/install.sh
# Options (env):  AIDASH_HOSTNAME=AIdashboard  (reachable as http://AIdashboard.local)
#                 AIDASH_PORT=80               (set in deploy/aidash.service)
#                 AIDASH_KEEP_HOSTNAME=1       (don't rename the Pi)
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/aidash
DATA=/var/lib/aidash
NAME="${AIDASH_HOSTNAME:-AIdashboard}"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi
command -v python3 >/dev/null || { echo "python3 missing: sudo apt install python3"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 7), sys.version' || { echo "Need Python 3.7+"; exit 1; }
python3 -c 'import sqlite3, ssl' || { echo "Python lacks sqlite3/ssl modules"; exit 1; }

id aidash >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin aidash
mkdir -p "$APP" "$DATA"
rm -rf "$APP/aidash"
cp -r "$SRC/run.py" "$SRC/aidash" "$APP/"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root "$APP"
chown -R aidash:aidash "$DATA"
chmod 700 "$DATA"

# ---- network name: <NAME>.local via mDNS (avahi) -------------------------------------
if [ "${AIDASH_KEEP_HOSTNAME:-0}" != "1" ]; then
  OLD="$(hostname)"
  if [ "$OLD" != "$NAME" ]; then
    if command -v hostnamectl >/dev/null 2>&1 && hostnamectl set-hostname "$NAME" 2>/dev/null; then :; else
      echo "$NAME" > /etc/hostname; hostname "$NAME" 2>/dev/null || true
    fi
    # Keep sudo happy with the new name (non-fatal: a failed hosts edit mustn't abort the install).
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
      sed "s/^127\.0\.1\.1.*/127.0.1.1\t$NAME/" /etc/hosts > /tmp/hosts.aidash && cat /tmp/hosts.aidash > /etc/hosts || true
      rm -f /tmp/hosts.aidash
    else
      printf '127.0.1.1\t%s\n' "$NAME" >> /etc/hosts || true
    fi
    echo "Hostname: $OLD -> $NAME"
  fi
  if ! command -v avahi-daemon >/dev/null 2>&1; then
    echo "Installing avahi-daemon (mDNS, for $NAME.local)..."
    apt-get install -y avahi-daemon >/dev/null 2>&1 || echo "WARN: could not install avahi-daemon; use the Pi's IP address instead"
  fi
  systemctl enable avahi-daemon >/dev/null 2>&1 || true
  systemctl restart avahi-daemon >/dev/null 2>&1 || true
fi

# Make sure the clock syncs at boot (no RTC on a Pi).
if systemctl list-unit-files 2>/dev/null | grep -q '^systemd-timesyncd'; then
  systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || true
  systemctl enable systemd-time-wait-sync >/dev/null 2>&1 || true
fi

cp "$SRC/deploy/aidash.service" /etc/systemd/system/aidash.service
systemctl daemon-reload
systemctl enable aidash
systemctl restart aidash

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Installed.  Open  http://$NAME.local   (or http://${IP:-<pi-ip>})  from a device on your network."
echo "Logs:  journalctl -u aidash -f      Data: $DATA"
echo "From your PC, copy settings + API keys:  python tools/push_to_pi.py --to http://$NAME.local"
