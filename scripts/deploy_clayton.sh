#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-ubuntu@victoriametrics}"
RELEASE_ID="${RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$REPO_ROOT" rev-parse --short HEAD)}"
CLAYTON_BIND_IP="${CLAYTON_BIND_IP:-127.0.0.1}"

# Restrict published endpoints to loopback or tailnet; never all interfaces.
python3 - "$CLAYTON_BIND_IP" "$RELEASE_ID" <<'PY'
import ipaddress
import re
import sys

address = ipaddress.IPv4Address(sys.argv[1])
if not (address == ipaddress.IPv4Address("127.0.0.1")
        or address in ipaddress.IPv4Network("100.64.0.0/10")):
    raise SystemExit("CLAYTON_BIND_IP must be 127.0.0.1 or a tailnet IPv4 address")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", sys.argv[2]):
    raise SystemExit("Invalid RELEASE_ID")
PY

REMOTE_RELEASE="/home/ubuntu/ingest/releases/clayton-$RELEASE_ID"
cd "$REPO_ROOT"
test -f decoder/Clayton/send_clayton.py
# Reserve a fresh release before streaming; never replace a running release.
ssh "$REMOTE_HOST" "mkdir -p /home/ubuntu/ingest/releases && mkdir '$REMOTE_RELEASE'"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='test_*.py' \
  --exclude='decoder/tests' --exclude='decoder/notebooks' \
  --exclude='decoder/GUI' \
  -czf - decoder deploy requirements.txt |
  ssh "$REMOTE_HOST" "tar -xzf - -C '$REMOTE_RELEASE'"

ssh "$REMOTE_HOST" bash -s -- "$REMOTE_RELEASE" "$RELEASE_ID" "$CLAYTON_BIND_IP" <<'REMOTE'
set -euo pipefail
release="$1"
release_id="$2"
bind_ip="$3"
base=/home/ubuntu/ingest
provisioning=/home/ubuntu/victoriametrics/provisioning/datasources
mkdir -p "$base/cursor" "$base/logs" "$HOME/.config/systemd/user"
docker network inspect victoriametrics_default >/dev/null
test -d "$provisioning"
printf 'CLAYTON_BIND_IP=%s\n' "$bind_ip" > "$release/deploy/clayton.compose.env"
compose=(docker compose --project-name clayton
  --env-file "$release/deploy/clayton.compose.env"
  -f "$release/deploy/clayton.compose.yaml")
"${compose[@]}" config --quiet
docker build -t "ingest-runner:clayton-$release_id" \
  -f "$release/deploy/Dockerfile.ingest" "$release"
# Build must succeed before interrupting Clayton. Other ingestion stays untouched.
if [[ -f "$HOME/.config/systemd/user/clayton-ingest.service" ]]; then
  systemctl --user stop clayton-ingest.timer clayton-ingest.service
fi
"${compose[@]}" up -d --wait --wait-timeout 120
curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --retry 24 --retry-delay 5 \
  --retry-all-errors http://127.0.0.1:8433/health
curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --retry 24 --retry-delay 5 \
  --retry-all-errors -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"clayton-deploy","version":"1"}}}' \
  http://127.0.0.1:9433/mcp
docker tag "ingest-runner:clayton-$release_id" ingest-runner:clayton
ln -sfn "$release" "$base/clayton-current"
chmod +x "$release/deploy/bin/run_clayton_ingest.sh"
docker cp "$release/deploy/clayton-datasource.yaml" \
  grafana:/etc/grafana/provisioning/datasources/clayton.yaml
cp "$release/deploy/systemd/clayton-ingest."{service,timer} "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now clayton-ingest.timer
systemctl --user start --no-block clayton-ingest.service
systemctl --user is-active clayton-ingest.timer
REMOTE

echo "Deployed Clayton $RELEASE_ID to $REMOTE_HOST:$REMOTE_RELEASE"
if [[ "${SKIP_DASHBOARD_DEPLOY:-0}" != 1 ]]; then
  REMOTE_HOST="$REMOTE_HOST" "$REPO_ROOT/scripts/deploy_clayton_dashboard.sh"
fi
