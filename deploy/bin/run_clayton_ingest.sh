#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${CLAYTON_ROOT_DIR:-/home/ubuntu/ingest/clayton-current}"
STATE_DIR="${CLAYTON_STATE_DIR:-/home/ubuntu/ingest/cursor}"
RUNNER_IMAGE="${CLAYTON_RUNNER_IMAGE:-ingest-runner:clayton}"
ENV_FILE="${ENV_FILE:-/etc/ingest/ingest.env}"

mkdir -p "$STATE_DIR"
cd "$ROOT_DIR"
docker_args=(--rm --name clayton-ingest --network host --memory 1g --memory-swap 1g --cpus 1
  -v "$ROOT_DIR:/app:ro" -v "$STATE_DIR:/state" -w /app
  -e PYTHONDONTWRITEBYTECODE=1)
if [[ -f "$ENV_FILE" ]]; then
  docker_args+=(--env-file "$ENV_FILE")
fi
if [[ -d "${HOME}/.aws" ]]; then
  docker_args+=(-v "${HOME}/.aws:/root/.aws:ro")
fi
for var in AWS_S3_TLS_INSECURE AWS_CA_BUNDLE AWS_S3_CA_BUNDLE REQUESTS_CA_BUNDLE SSL_CERT_FILE CURL_CA_BUNDLE; do
  if value="$(printenv "$var")" && [[ -n "$value" ]]; then
    docker_args+=(-e "$var=$value")
    if [[ -f "$value" ]]; then
      docker_args+=(-v "$value:$value:ro")
    fi
  fi
done
exec docker run "${docker_args[@]}" \
  "$RUNNER_IMAGE" \
  python -m decoder.Clayton.send_clayton \
  --server "${CLAYTON_SERVER:-http://127.0.0.1:8433}" \
  --state-file /state/clayton.sqlite \
  --max-files-per-device "${CLAYTON_MAX_FILES_PER_DEVICE:-10}"
