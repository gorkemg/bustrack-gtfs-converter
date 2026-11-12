"""Helpers for building the MOTIS routing dataset in CI.

The converter publishes a normalized `{agency}.gtfs.zip` next to the SQLite
release. This module turns `config/routing.json` into a MOTIS `config.yml`,
derives the OSM clip commands, writes build metadata and smoke-tests a running
MOTIS server. Pure functions are unit tested; the CLI wires them for the
workflow in `.github/workflows/motis-data.yml`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTING_CONFIG_PATH = REPO_ROOT / "config" / "routing.json"
AGENCY_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def load_routing_config(path: Path = ROUTING_CONFIG_PATH) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    validate_routing_config(config)
    return config


def validate_routing_config(config: dict) -> None:
    motis = config.get("motis")
    if not isinstance(motis, dict) or not str(motis.get("version", "")).strip():
        raise ValueError("routing config needs motis.version")
    agencies = config.get("agencies")
    if not isinstance(agencies, list) or not agencies:
        raise ValueError("routing config needs a non-empty agencies list")
    seen: set[str] = set()
    for agency in agencies:
        agency_id = agency.get("id", "")
        if not agency_id or not set(agency_id) <= AGENCY_ID_ALLOWED or agency_id in seen:
            raise ValueError(f"invalid or duplicate agency id: {agency_id!r}")
        seen.add(agency_id)
        bbox = agency.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            raise ValueError(f"{agency_id}: bbox must be [min_lon, min_lat, max_lon, max_lat]")
        if not str(agency.get("osm_url", "")).startswith("https://"):
            raise ValueError(f"{agency_id}: osm_url must be https")
        for url in agency.get("realtime", []):
            if not str(url).startswith("https://"):
                raise ValueError(f"{agency_id}: realtime URLs must be https")
        ZoneInfo(agency.get("timezone", ""))


def yaml_quote(value: str) -> str:
    return json.dumps(value)


def render_motis_config(config: dict, osm_file: str = "merged.osm.pbf") -> str:
    """Render the MOTIS config.yml. Dataset tags equal agency ids, so MOTIS
    trip ids carry `_{agency}_{gtfs_trip_id}`."""
    lines = [
        "server:",
        "  host: 127.0.0.1",
        "  port: 8080",
        f"osm: {yaml_quote(osm_file)}",
        "timetable:",
        "  first_day: TODAY",
        "  datasets:",
    ]
    for agency in config["agencies"]:
        agency_id = agency["id"]
        lines.append(f"    {agency_id}:")
        lines.append(f"      path: {yaml_quote(agency_id + '.gtfs.zip')}")
        realtime = agency.get("realtime", [])
        if realtime:
            lines.append("      rt:")
            for url in realtime:
                lines.append(f"        - url: {yaml_quote(url)}")
    lines += [
        "street_routing: true",
        "osr_footpath: true",
        "geocoding: true",
        "reverse_geocoding: true",
        "",
    ]
    return "\n".join(lines)


def osm_clip_commands(config: dict) -> list[list[str]]:
    commands = []
    for agency in config["agencies"]:
        min_lon, min_lat, max_lon, max_lat = agency["bbox"]
        commands.append([
            "osmium", "extract", "--overwrite", "--strategy", "complete_ways",
            "-b", f"{min_lon},{min_lat},{max_lon},{max_lat}",
            f"osm/{agency['id']}-source.osm.pbf", "-o", f"osm/{agency['id']}-clip.osm.pbf",
        ])
    return commands


def next_weekday_morning(tz_name: str, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(tz_name))
    candidate = (local + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def plan_url(base_url: str, origin: list[float], destination: list[float], when: datetime) -> str:
    query = urllib.parse.urlencode({
        "fromPlace": f"{origin[0]},{origin[1]}",
        "toPlace": f"{destination[0]},{destination[1]}",
        "time": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    return f"{base_url.rstrip('/')}/api/v6/plan?{query}"


def transit_trip_ids(plan: dict) -> list[str]:
    trip_ids = []
    for itinerary in plan.get("itineraries", []):
        for leg in itinerary.get("legs", []):
            trip_id = leg.get("tripId")
            if leg.get("mode") not in (None, "WALK", "BIKE", "CAR") and trip_id:
                trip_ids.append(str(trip_id))
    return trip_ids


def smoke_check(plan: dict, agency_id: str) -> str | None:
    """Return an error message, or None when the plan has a transit leg of
    this agency's dataset."""
    trip_ids = transit_trip_ids(plan)
    if not trip_ids:
        return f"{agency_id}: no transit leg in {len(plan.get('itineraries', []))} itineraries"
    if not any(f"_{agency_id}_" in trip_id for trip_id in trip_ids):
        return f"{agency_id}: no trip id tagged with the {agency_id} dataset: {trip_ids[:3]}"
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_info(config: dict, inputs: dict[str, Path], channel: str, now: datetime) -> dict:
    return {
        "channel": channel,
        "built_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "motis_version": config["motis"]["version"],
        "agencies": [agency["id"] for agency in config["agencies"]],
        "inputs": {name: file_sha256(path) for name, path in sorted(inputs.items())},
    }


def wait_for_server(base_url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError as error:
            last_error = error
        time.sleep(1)
    raise TimeoutError(f"MOTIS did not become healthy: {last_error}")


def cmd_config(args: argparse.Namespace) -> int:
    config = load_routing_config(args.routing)
    args.out.write_text(render_motis_config(config), encoding="utf-8")
    return 0


def cmd_clip_commands(args: argparse.Namespace) -> int:
    config = load_routing_config(args.routing)
    for command in osm_clip_commands(config):
        print(" ".join(command))
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    config = load_routing_config(args.routing)
    wait_for_server(args.base_url, args.timeout)
    now = datetime.now(timezone.utc)
    failures = []
    for agency in config["agencies"]:
        trip = agency.get("smoke_trip")
        if not trip:
            continue
        when = next_weekday_morning(agency["timezone"], now)
        url = plan_url(args.base_url, trip["from"], trip["to"], when)
        with urllib.request.urlopen(url, timeout=60) as response:
            plan = json.load(response)
        error = smoke_check(plan, agency["id"])
        if error:
            failures.append(error)
        else:
            print(f"{agency['id']}: ok ({len(plan.get('itineraries', []))} itineraries)")
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


def cmd_info(args: argparse.Namespace) -> int:
    config = load_routing_config(args.routing)
    inputs = {path.name: path for path in args.inputs}
    info = build_info(config, inputs, args.channel, datetime.now(timezone.utc))
    args.out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", type=Path, default=ROUTING_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    p_config = sub.add_parser("config", help="write MOTIS config.yml")
    p_config.add_argument("--out", type=Path, required=True)
    p_config.set_defaults(func=cmd_config)
    p_clip = sub.add_parser("clip-commands", help="print osmium extract commands")
    p_clip.set_defaults(func=cmd_clip_commands)
    p_smoke = sub.add_parser("smoke", help="query a running MOTIS server")
    p_smoke.add_argument("--base-url", default="http://127.0.0.1:8080")
    p_smoke.add_argument("--timeout", type=float, default=120)
    p_smoke.set_defaults(func=cmd_smoke)
    p_info = sub.add_parser("info", help="write build-info.json")
    p_info.add_argument("--out", type=Path, required=True)
    p_info.add_argument("--channel", choices=["production", "beta"], required=True)
    p_info.add_argument("inputs", type=Path, nargs="+")
    p_info.set_defaults(func=cmd_info)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
