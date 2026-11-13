# Routing node (MOTIS)

The routing node serves `/api/v6/plan` for the BusTrack backend (`GET /v1/:agency/plan`). It never imports data: CI builds the dataset (`.github/workflows/motis-data.yml`) and the node pulls it.

```
GitHub release motis / motis-prerelease
        │  motis-update.timer (every 30 min)
        ▼
motis_update.py ── verify sha256 ── extract to /srv/motis/releases/<id>
        │  start idle colour (blue :8081 / green :8082), health + smoke test
        ▼
/etc/caddy/motis-upstream.caddy → caddy reload → stop old colour
```

| Path | Purpose |
| :--- | :--- |
| `/srv/motis/motis_update.py` | updater (`update`, `rollback`, `status`) |
| `/srv/motis/releases/<id>/data` | active and previous dataset; older ones are pruned |
| `/srv/motis/bin/<version>/motis` | MOTIS binary per version named in `build-info.json` |
| `/srv/motis/state.json` | active/previous release and colour |
| `/etc/caddy/motis.env` | `ROUTING_HOST`, `MOTIS_TOKEN` (root:caddy 0640) |
| `/etc/default/motis-update` | `MOTIS_CHANNEL=production` or `beta`, optional `GITHUB_TOKEN` |
| `/srv/motis/public/status.json` | monitoring snapshot, rewritten every minute by `routing-status.timer` |

## Setup

1. Create a Linode (Nanode 1 GB is enough for PVTA + UTA; Ubuntu 24.04) and point a DNS name at it.
2. Copy this directory to the node and run `ROUTING_HOST=<dns name> MOTIS_CHANNEL=production ./install.sh` as root. It installs Caddy, zstd and a 1 GB swap file, creates the `motis` user, installs the units, generates the bearer token (printed once) and runs the first update.
3. Store the backend secrets `MOTIS_URL=https://<dns name>` and `MOTIS_TOKEN=<token>` in the backend's GitHub environment (reinstall the backend deploy helper first) and redeploy the backend.

A beta node uses `MOTIS_CHANNEL=beta`; one node per channel keeps beta experiments away from production.

## Operations

- Status: `python3 -I /srv/motis/motis_update.py status`, logs: `journalctl -u motis-update -u 'motis@*'`.
- Force a re-fetch: `systemctl start motis-update` (or `motis_update.py update --force`).
- Roll back to the previous dataset: `python3 -I /srv/motis/motis_update.py rollback`.
- Probe: `curl -H "Authorization: Bearer $MOTIS_TOKEN" https://<dns name>/api/v1/health`.

Swap strategy: with at least 700 MB available, the new colour starts beside the old one and traffic switches without a gap. Below that the updater stops the old colour first (a few seconds of 503 for the backend, which retries after 30 s) and restarts it if the new dataset fails its checks. Any failure leaves the previous dataset serving.

Capacity: serving RSS was 180 to 490 MB for PVTA + UTA in the spike. Each additional city adds roughly its GTFS plus clipped OSM size; watch `systemctl status motis@<colour>` and raise `MemoryMax` together with the node size.

## Monitoring

The backend's alert engine watches this node (checks `routing.*`, see the backend README). The node only publishes data, it never pushes anything itself.

| Route | Auth | Content |
| :--- | :--- | :--- |
| `/_status` | bearer token | `status.json`: boot id and time, memory, disk, load, pending (security) updates, `reboot-required`, active release and colour, `motis@<colour>` state, restarts and last result, updater success/failure counters |
| `/healthz` | none | MOTIS `/api/v1/health` of the active colour, for the external watchdog |

`motis_update.py report` writes the snapshot; every probe is best effort, a missing value is `null`. Pending updates come from `apt-check`, cached for an hour.

Updates: `unattended-upgrades` installs security updates; when one needs a reboot the node reboots at 09:00 UTC (04:00 US Eastern). The backend treats a boot in 09:00 to 09:20 UTC after `reboot-required` as planned. After changing anything in this directory, re-run `install.sh` (idempotent; it validates the Caddyfile before restarting Caddy).
