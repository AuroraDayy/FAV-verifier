#!/usr/bin/env bash
# End-to-end test of server.py against tests/mock_tv.py (no real TV needed).
# Usage: bash tests/run_mock_test.sh <work-dir>
set -u
cd "$(dirname "$0")/.."
WORK="$1"
mkdir -p "$WORK"
CFG="$WORK/cfg.json"
echo '{}' > "$CFG"

python3 tests/mock_tv.py 8090 &
MOCK=$!
start_server() {
  TV_PORT=8090 TV_REMOTE_PORT=8799 TV_REMOTE_CONFIG="$CFG" python3 server.py > "$WORK/server.log" 2>&1 &
  SRV=$!
  sleep 1
}
start_server

p() {
  local body="$2"
  echo "> $1 $body"
  curl -s -X POST "localhost:8799$1" -H 'Content-Type: application/json' -d "$body"
  echo
}

p /api/state '{}'
p /api/select '{"host":"127.0.0.1"}'
p /api/key '{"name":"Home"}'
p /api/pair/start '{}'
p /api/pair/finish '{"pin":"0000"}'
p /api/pair/finish '{"pin":"5678"}'
p /api/status '{}'
p /api/key '{"name":"Home"}'
p /api/key '{"name":"VolumeUp"}'
p /api/text '{"text":"hello"}'
p /api/power '{"action":"off"}'
p /api/status '{}'
p /api/power '{"action":"on"}'
p /api/power '{"action":"toggle"}'

echo "--- stale cookie -> should auto-refresh using stored PIN"
python3 - "$CFG" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1])); c["cookie"] = "auth=stale"; json.dump(c, open(sys.argv[1], "w"))
EOF
kill "$SRV"; sleep 0.3
start_server
p /api/key '{"name":"Mute"}'
grep cookie "$CFG"

echo "--- PSK path"
p /api/select '{"host":"127.0.0.1"}'
p /api/psk '{"psk":"wrong"}'
p /api/psk '{"psk":"1234"}'
p /api/power '{"action":"off"}'

echo "--- mock TV state"
curl -s localhost:8090/_state; echo
curl -s -o /dev/null -w "index %{http_code}\n" localhost:8799/
curl -s -o /dev/null -w "traversal %{http_code}\n" --path-as-is localhost:8799/../server.py

kill "$SRV" "$MOCK"
