#!/usr/bin/env bash
# Open the EPEver monitor: run epever_server.py on 127.0.0.1:8765 if not already running (it
# keeps the log in epever.db), then open Chrome in the dialout group. Always the same address,
# so the browser keeps its log and the serial-port permission.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PORT=8765
URL="http://127.0.0.1:$PORT/epever_monitor.html"

if ! (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  setsid -f uv-3.13 run --quiet epever_server.py "$PORT" >/dev/null 2>&1
  for _ in {1..100}; do (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break; sleep 0.1; done
fi

# A running Chrome only opens a new tab in itself, so it must already have dialout.
gid=$(getent group dialout | cut -d: -f3)
for p in $(pgrep -u "$(id -u)" -f '^/opt/google/chrome/chrome' || true); do
  [[ $(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null) == *--type=* ]] && continue
  if ! grep -qE "^Groups:.*\b$gid\b" "/proc/$p/status"; then
    echo "Chrome is already running without the dialout group, so it can't open the port."
    echo "Close Chrome completely, then run this again."
    exit 1
  fi
done

setsid -f sg dialout -c "google-chrome '$URL' >/dev/null 2>&1"
