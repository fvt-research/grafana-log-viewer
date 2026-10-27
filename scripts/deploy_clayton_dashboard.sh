#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-ubuntu@victoriametrics}"

tar -C "$REPO_ROOT/provisioning/dashboards" -cf - clayton.yaml clayton |
  ssh "$REMOTE_HOST" 'docker cp - grafana:/etc/grafana/provisioning/dashboards'

ssh "$REMOTE_HOST" python3 - <<'PY'
import base64
import json
import subprocess
import urllib.request

container = json.loads(subprocess.check_output(["docker", "inspect", "grafana"]))[0]
environment = dict(entry.split("=", 1) for entry in container["Config"]["Env"])
password = environment.get("GF_SECURITY_ADMIN_PASSWORD")
if not password:
    raise SystemExit("Grafana admin password is not configured in container environment")
user = environment.get("GF_SECURITY_ADMIN_USER", "admin")
authorization = base64.b64encode(f"{user}:{password}".encode()).decode()

def request(path, data=None):
    req = urllib.request.Request(
        "http://127.0.0.1:3000" + path,
        data=data,
        headers={"Authorization": "Basic " + authorization},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

request("/api/admin/provisioning/datasources/reload", b"")
request("/api/admin/provisioning/dashboards/reload", b"")
dashboard = request("/api/dashboards/uid/clayton-batteries")
resource = request(
    "/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards/clayton-batteries"
)
annotations = resource["metadata"].get("annotations", {})
if annotations.get("grafana.app/managerId") != "Clayton":
    raise SystemExit("Clayton dashboard was not loaded from provisioning")
if annotations.get("grafana.app/managerAllowsEdits") != "true":
    raise SystemExit("Clayton dashboard provisioning does not permit UI saves")
print("Provisioned dashboard:", dashboard["meta"]["url"])
PY
