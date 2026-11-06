#!/usr/bin/env bash
# Install the desk as a 15-minute systemd timer on this machine (the same
# VPS as the Nakoda News collector, so it can read its database).
#   sudo ./deploy/install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv .venv -p 3.12; else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null; then uv pip install --python .venv/bin/python -r requirements.txt
else .venv/bin/pip install -q -r requirements.txt; fi

[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example: fill it in."; }
.venv/bin/python -m desk check || true

for unit in nakoda-linkedin.service nakoda-linkedin.timer; do
  sed "s#__DIR__#$DIR#g" "deploy/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now nakoda-linkedin.timer
echo "Installed. Follow with: journalctl -u nakoda-linkedin -f"
systemctl list-timers 'nakoda-linkedin*' --no-pager
