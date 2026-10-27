# Ingest automation

This runs existing Python ingestion scripts on the `victoriametrics` host with
systemd user timers.
Runtime dependencies are installed in Docker image `ingest-runner:current` at
deploy time (no host Python/venv dependency).

Polling uses S3 `LastModified` upload time plus persisted cursor state
(`/home/ubuntu/ingest/cursor/*.json`) and an overlap buffer. This catches files
uploaded now even when their embedded recording timestamps are old. First run
after migration from a recording-time cursor rescans the previous 24 hours.
Cursor files live under `/home/ubuntu/ingest/cursor` so they survive release
switches and can be copied to new server.

Set `D65_SERVER` and `B3SR_SERVER` in `/etc/ingest/ingest.env` to local/container
addresses so ingestion avoids tailnet routing on-host.

## First-time server setup

The deploy script creates the release/cursor/log directories, installs the
systemd user units, and enables the timers automatically. The only manual
first-time step is providing the env file:

- `sudo mkdir -p /etc/ingest`
- `sudo cp /home/ubuntu/ingest/current/deploy/env/ingest.env.example /etc/ingest/ingest.env`
- Edit `/etc/ingest/ingest.env` and fill in private values.

`/etc/ingest/ingest.env` is optional; wrappers run with built-in defaults if it
is missing.

## Deploy from local machine

```bash
./scripts/deploy_victoriametrics.sh
```

## Send D65 files from a local folder

```bash
python -m decoder.D65.send_d65_data --input-folder /mnt/d/d65files
```

`--input-folder` (alias `--folder`) scans MF4 files recursively and skips S3
download. Existing `--start`/`--end` filters still apply (default: last 7 days).
Files need an `Upper`/`Lower` folder or device MAC in their path for job
selection. This local mode cannot be combined with `--s3-streaming`; scheduled
server ingestion continues using S3 streaming.

## Status and logs

```bash
tailscale ssh ubuntu@victoriametrics
systemctl --user list-timers | grep ingest
systemctl --user status d65-ingest.service b3sr-ingest.service
tail -n 200 /home/ubuntu/ingest/logs/d65.log
tail -n 200 /home/ubuntu/ingest/logs/b3sr.log
```

## Rollback

```bash
./scripts/rollback_victoriametrics.sh <release-id>
```

## Clayton locomotive batteries (independent deployment)

Clayton reads **all available history** from `fvt-telematics`, using the bundled
device/DBC configuration and existing AWS credentials. No Lambda/Influx changes.
Each run processes at most 10 files per device, one file at a time; subsequent
runs resume per-object SQLite checkpoints. No generic time-window cursor is used.
The timer runs every five minutes and never overlaps its own active service.

The selective deploy leaves D65/B3SR units, `ingest/current`, and
`ingest-runner:current` untouched. It builds dependencies with
`deploy/Dockerfile.ingest` (no pre-existing runner image required), packages the
decoder runtime including Clayton DBC/assets, and creates:

- `/home/ubuntu/ingest/releases/clayton-<release-id>` and `clayton-current`;
- `ingest-runner:clayton-<release-id>` and alias `ingest-runner:clayton`;
- `/home/ubuntu/ingest/cursor/clayton.sqlite` (retain across deployments);
- Compose project `clayton` on existing external `victoriametrics_default`;
- VM data at `/home/ubuntu/victoriametrics/data/victoriametrics-clayton`.

VM retention is 10 years, **not** a disk quota. Monitor free disk during backfill
(host initially has about 69 GB free). Inventory is roughly 172,000 MF4 objects /
768 GiB raw across five devices: full backfill takes many polls and storage must
be monitored/expanded. VM refuses writes below 10 GB free using
`-storage.minFreeDiskSpaceBytes=10GB`; failed sends must not advance checkpoints.
There is no automatic retention adjustment or downsampling. VM is limited to 1 GiB RAM / 1 CPU,
MCP to 768 MiB / 0.5 CPU (Go memory target 512 MiB), ingestion to 1 GiB /
1 CPU (no swap). Existing MCP instances use about 400-465 MiB; a 256 MiB
cap prevents startup. Containers
restart automatically; keep user systemd lingering enabled for unattended runs.

### Roll out Clayton only

Ensure `/etc/ingest/ingest.env` has valid existing AWS credentials/profile
and optionally append the Clayton keys from `deploy/env/ingest.env.example`.
Do not overwrite existing D65/B3SR values. AWS config under `~/.aws` is mounted
read-only, and existing CA/TLS environment conventions are preserved.

From the local checkout:

```bash
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)" \
CLAYTON_BIND_IP=100.87.35.92 ./scripts/deploy_clayton.sh
```

Default binding is loopback only. Setting the tailnet IPv4 publishes **both**
`127.0.0.1` and that tailnet address: VM `8433`, MCP `9433`. No public-interface
binding; these unauthenticated endpoints rely on host/tailnet access controls.
The deploy checks VM health and initializes `/mcp` before starting ingestion.
It stops/restarts only Clayton; no Grafana or existing container restart.
If deployment fails after stopping Clayton, follow rollback below.

The script copies **only** `deploy/clayton-datasource.yaml` to the existing
Grafana provisioning directory as `clayton.yaml`. Existing datasource files
remain untouched. Deployment reloads datasource/dashboard provisioning
automatically. To reload just datasources manually on the host:

```bash
# Grafana server-admin credentials required; curl prompts for password.
curl --fail --user admin -X POST \
  http://127.0.0.1:3000/api/admin/provisioning/datasources/reload
```

Datasource name: `VictoriaMetrics-Clayton`; UID: `victoriametrics-clayton`.
Grafana's existing host network reaches VM at `http://127.0.0.1:8433`.
`deploy/clayton-mcp.json` is an HTTP MCP client example, **not** installed into
any shared user configuration. Its URL is `http://100.87.35.92:9433/mcp`.

For intentional combined D65/B3SR **and** Clayton deployment only:
`ENABLE_CLAYTON=1 CLAYTON_BIND_IP=100.87.35.92 ./scripts/deploy_victoriametrics.sh`.
Without `ENABLE_CLAYTON=1`, that script does not install/start Clayton units.
Use the selective script above to avoid touching other ingestion.

### Local CLI and backfill status

With repository requirements installed and AWS credentials available:

```bash
python -m decoder.Clayton.send_clayton \
  --server http://100.87.35.92:8433 \
  --state-file ./clayton.sqlite --max-files-per-device 10
```

Re-run with the **same state file** to advance all-history backfill; do not
delete checkpoints to restart a service. Avoid simultaneously running local
and server ingestion against the same source/destination with separate states.

On the host:

```bash
systemctl --user status clayton-ingest.service clayton-ingest.timer
systemctl --user list-timers clayton-ingest.timer
tail -n 100 /home/ubuntu/ingest/logs/clayton.log
ls -lh /home/ubuntu/ingest/cursor/clayton.sqlite*
curl --fail http://127.0.0.1:8433/health
curl --fail --get http://127.0.0.1:8433/api/v1/query \
  --data-urlencode 'query=count({__name__!=""})'
df -h /home/ubuntu/victoriametrics/data/victoriametrics-clayton
docker stats --no-stream
```

Use ingestion logs to track successful objects, remaining batches and errors;
an inactive oneshot between timer runs is normal. Stop backfill safely with
`systemctl --user stop clayton-ingest.timer clayton-ingest.service`.
Jobs are `LB101` through `LB105`; `bus=CAN1` / `bus=CAN2` distinguishes shared-ID
signals from the two DBCs. A roughly 4 MB source file can expand to about 437,000
samples / 1,558 signals, so source size is not a storage forecast.
SQLite state and VM storage are persistent host bind mounts. Back them up
separately; stop ingestion before copying SQLite plus any journal/WAL files.

### Roll back Clayton only

On the host, replace `<previous-release-id>` with the recorded prior Clayton ID:

```bash
set -e
previous_id=REPLACE_WITH_PREVIOUS_RELEASE_ID
previous="/home/ubuntu/ingest/releases/clayton-$previous_id"
test -d "$previous"
docker image inspect "ingest-runner:clayton-$previous_id" >/dev/null
systemctl --user stop clayton-ingest.timer clayton-ingest.service
docker compose --project-name clayton \
  --env-file "$previous/deploy/clayton.compose.env" \
  -f "$previous/deploy/clayton.compose.yaml" up -d --wait
docker tag "ingest-runner:clayton-$previous_id" ingest-runner:clayton
ln -sfn "$previous" /home/ubuntu/ingest/clayton-current
cp "$previous"/deploy/systemd/clayton-ingest.{service,timer} \
  "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now clayton-ingest.timer
systemctl --user start --no-block clayton-ingest.service
```

Keep SQLite checkpoints and VM data; do not use `compose down --volumes` or
delete storage. These commands roll back decoder/dependency releases, not VM/MCP
images tagged `latest`; record deployed image digests before deliberate upgrades.
If no prior release exists, stop/disable only `clayton-ingest.timer`, stop its
service and run the current Clayton Compose project `stop`; retain data.

### Clayton dashboard

The **Clayton** folder contains **Locomotive batteries diagnostics dashboard**
at `/d/clayton-batteries`. Its 47 panels retain the cloud dashboard layout,
thresholds, value mappings and legends, using the Clayton VM datasource.
Cloud library panels are embedded so no cloud Grafana connection is required.
External logo images retain their original public asset URLs.

Deploy dashboard updates without interrupting ingestion or restarting Grafana:

```bash
./scripts/deploy_clayton_dashboard.sh
```

The script copies only Clayton provisioning files, reloads dashboard providers
using the existing Grafana container admin credentials without printing them,
and verifies the provisioned dashboard. Normal Clayton deployment also invokes
this script. Existing dashboards are not replaced.

UI edits can be saved in Grafana's database. Export saved dashboard JSON back
to `provisioning/dashboards/clayton/locomotive-batteries.json` before changing or
redeploying that source: provisioning updates can overwrite database edits.

The battery selector uses VM jobs `LB101` through `LB105`; default `LB103` has
recent recordings. For offline batteries, use their recording dates in the time
picker. The five last-seen cards use actual sample timestamps over 90 days,
independent of the dashboard time range. Missing recordings stay empty.

The deleted cloud PLC-status library panel was rebuilt from recorded CAN2 PLC
status bits. The cloud alert list is an explicit notice: no source alert rules
were available, and recorded fault panels are not live alert evaluations.
Runtime/distance graphs show reset-aware cumulative increases since the first
displayed interval. Rollups use trailing display intervals in MetricsQL rather
than Flux's window-end timestamps.
Runtime labels distinguish PLC and screen counters. SOC and charger-current
graphs select pack SOC and measured current, excluding similarly named status
bits and setpoints now present in the full raw decode. Active fault panels use
point plots: Grafana's status-history panel errors on empty Prometheus frames.
Empty fault plots mean no matching active samples, not proof of no faults.

Migration/check commands (cloud exports remain outside git under `data/`):

```bash
python -m scripts.migrate_clayton_dashboard data/clayton-dashboards \
  provisioning/dashboards/clayton/locomotive-batteries.json
python -m scripts.test_migrate_clayton_dashboard \
  --server http://100.87.35.92:8433
```

The export directory needs `source.json` from the cloud dashboard API and
`library-<uid>.json` from its library-element API. The migration is specific to
that dashboard, not a general Flux converter. The check executes each target
for all five batteries at their latest recorded times, distinguishing absent
recordings and inactive faults from query failures.
