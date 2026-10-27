#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-ubuntu@victoriametrics}"
REMOTE_BASE="${REMOTE_BASE:-/home/ubuntu/ingest}"
RELEASE_ID="${RELEASE_ID:-$(cd "$REPO_ROOT" && git rev-parse --short HEAD)}"
REMOTE_RELEASE="$REMOTE_BASE/releases/$RELEASE_ID"
TAR_NAME="ingest-$RELEASE_ID.tar.gz"

cd "$REPO_ROOT"

TMP_TAR="$(mktemp "/tmp/$TAR_NAME.XXXX")"
trap 'rm -f "$TMP_TAR"' EXIT

tar \
--exclude='.git' \
--exclude='venv' \
--exclude='data' \
--exclude='__pycache__' \
--exclude='decoder/test_*.py' \
--exclude='decoder/notebooks' \
-czf "$TMP_TAR" \
decoder/__init__.py \
decoder/config.py \
decoder/utils.py \
decoder/s3_helper.py \
decoder/sending.py \
decoder/livelogger \
decoder/D65 \
decoder/B3SR \
decoder/Clayton \
deploy \
requirements.txt

ssh "$REMOTE_HOST" "
  set -euo pipefail
  mkdir -p '$REMOTE_BASE/releases' '$REMOTE_BASE/shared' '$REMOTE_BASE/cursor' '$REMOTE_BASE/logs' \"\$HOME/.config/systemd/user\"
"
scp "$TMP_TAR" "$REMOTE_HOST:$REMOTE_RELEASE.tar.gz"
ssh "$REMOTE_HOST" "
  set -euo pipefail
  mkdir -p '$REMOTE_RELEASE'
  tar -xzf '$REMOTE_RELEASE.tar.gz' -C '$REMOTE_RELEASE'
  rm -f '$REMOTE_RELEASE.tar.gz'
  docker build -t ingest-runner:$RELEASE_ID -f '$REMOTE_RELEASE/deploy/Dockerfile.ingest' '$REMOTE_RELEASE'
  docker tag ingest-runner:$RELEASE_ID ingest-runner:current
  ln -sfn '$REMOTE_RELEASE' '$REMOTE_BASE/current'
  chmod +x '$REMOTE_BASE/current/deploy/bin/'*.sh '$REMOTE_BASE/current/deploy/bin/'*.py
  cp '$REMOTE_BASE/current/deploy/systemd/'{d65,b3sr}-ingest.service \"\$HOME/.config/systemd/user/\"
  cp '$REMOTE_BASE/current/deploy/systemd/'{d65,b3sr}-ingest.timer \"\$HOME/.config/systemd/user/\"
  systemctl --user daemon-reload
  systemctl --user enable --now d65-ingest.timer b3sr-ingest.timer
  systemctl --user restart d65-ingest.service b3sr-ingest.service
"

echo "Deployed release $RELEASE_ID to $REMOTE_HOST:$REMOTE_RELEASE"

if [[ "${ENABLE_CLAYTON:-0}" == 1 ]]; then
  "$REPO_ROOT/scripts/deploy_clayton.sh"
fi
