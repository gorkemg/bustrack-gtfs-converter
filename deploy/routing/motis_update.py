#!/usr/bin/env python3
"""Pull-based updater for the BusTrack MOTIS routing node.

Runs on the routing server (systemd timer, as root). It never builds anything:
it downloads the dataset that CI published (`motis` or `motis-prerelease`),
verifies its SHA-256, makes sure the matching MOTIS binary is present, starts
the dataset on the idle colour (blue/green), repeats the CI smoke test against
it and only then points Caddy at it. Any failure leaves the active dataset
serving.

Standard library only, so the node needs nothing beyond python3, zstd and tar.

    motis_update.py update [--channel production|beta] [--force] [--strategy auto|bluegreen|stop-first]
    motis_update.py rollback
    motis_update.py status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPOSITORY = "gorkemg/bustrack-gtfs-converter"
MOTIS_REPOSITORY = "motis-project/motis"
CHANNEL_TAGS = {"production": "motis", "beta": "motis-prerelease"}
COLOURS = {"blue": 8081, "green": 8082}
DATA_ASSET = "motis-data.tar.zst"
USER_AGENT = "bustrack-routing-updater/1"
# Below this much available memory, two MOTIS processes do not fit side by
# side (serving RSS was 180-490 MB in the spike), so the old one stops first.
BLUEGREEN_MIN_AVAILABLE_KB = 700 * 1024
SERVICE_USER = "motis"


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def binaries(self) -> Path:
        return self.root / "bin"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    def env_file(self, colour: str) -> Path:
        return self.root / f"{colour}.env"


# ---------------------------------------------------------------- pure logic


def other_colour(colour: str | None) -> str:
    return "green" if colour == "blue" else "blue"


def parse_sha256_file(text: str) -> str:
    """`sha256sum` output: '<hex>  <name>'."""
    token = text.strip().split()[0] if text.strip() else ""
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token.lower()):
        raise ValueError("malformed sha256 file")
    return token.lower()


def release_id(data_sha256: str) -> str:
    return data_sha256[:16]


def set_server_port(config_text: str, port: int) -> str:
    """Rewrite `server.port` (and pin the host to loopback) in the config.yml
    rendered by scripts/motis_build.py."""
    lines = config_text.splitlines()
    out: list[str] = []
    in_server = False
    seen_port = False
    for line in lines:
        if line and not line.startswith((" ", "\t")):
            in_server = line.rstrip() == "server:"
        if in_server and line.strip().startswith("port:"):
            line = f"  port: {port}"
            seen_port = True
        elif in_server and line.strip().startswith("host:"):
            line = "  host: 127.0.0.1"
        out.append(line)
    if not seen_port:
        raise ValueError("config.yml has no server.port")
    return "\n".join(out) + "\n"


def choose_strategy(requested: str, mem_available_kb: int | None, has_active: bool) -> str:
    if not has_active:
        return "bluegreen"  # nothing to stop
    if requested != "auto":
        return requested
    if mem_available_kb is None or mem_available_kb < BLUEGREEN_MIN_AVAILABLE_KB:
        return "stop-first"
    return "bluegreen"


def parse_meminfo(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return None


def releases_to_prune(existing: list[str], keep: list[str | None]) -> list[str]:
    keep_set = {item for item in keep if item}
    return sorted(name for name in existing if name not in keep_set)


def smoke_time(tz_name: str, now: datetime) -> datetime:
    """Next weekday 08:00 local, the same probe time CI uses."""
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
    return f"{base_url}/api/v6/plan?{query}"


def smoke_error(plan: dict, agency_id: str) -> str | None:
    trip_ids = [
        str(leg.get("tripId"))
        for itinerary in plan.get("itineraries", [])
        for leg in itinerary.get("legs", [])
        if leg.get("mode") not in (None, "WALK", "BIKE", "CAR") and leg.get("tripId")
    ]
    if not any(f"_{agency_id}_" in trip_id for trip_id in trip_ids):
        return f"{agency_id}: no transit leg of this dataset ({len(trip_ids)} transit legs)"
    return None


def caddy_upstream(port: int) -> str:
    # MOTIS answers 400 "malformed URI or request" to any Authorization
    # header, and it has no use for the bearer token anyway.
    return f"reverse_proxy 127.0.0.1:{port} {{\n\theader_up -Authorization\n}}\n"


def safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Only regular files and directories below data/; no links, no escapes."""
    members = []
    for member in archive.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe path in archive: {name}")
        if Path(name).parts[0] != "data":
            raise ValueError(f"unexpected top-level entry: {name}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"unsupported entry type: {name}")
        members.append(member)
    return members


# ----------------------------------------------------------------- side effects


def log(message: str) -> None:
    print(message, flush=True)


def http_get(url: str, timeout: float = 60, accept: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download(url: str, target: Path) -> str:
    """Stream to disk and return the SHA-256."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=300) as response, tmp.open("wb") as handle:
        for block in iter(lambda: response.read(1 << 20), b""):
            digest.update(block)
            handle.write(block)
    tmp.replace(target)
    return digest.hexdigest()


def release_assets(repository: str, tag: str) -> dict[str, str]:
    raw = http_get(f"https://api.github.com/repos/{repository}/releases/tags/{tag}", accept="application/vnd.github+json")
    release = json.loads(raw)
    return {asset["name"]: asset["browser_download_url"] for asset in release.get("assets", [])}


def load_state(paths: Paths) -> dict:
    try:
        return json.loads(paths.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(paths: Paths, state: dict) -> None:
    tmp = paths.state.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(paths.state)


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True)


def ensure_binary(paths: Paths, version: str, expected_sha256: str) -> Path:
    target_dir = paths.binaries / version
    binary = target_dir / "motis"
    if binary.is_file():
        return binary
    assets = release_assets(MOTIS_REPOSITORY, f"v{version}")
    url = assets.get("motis-linux-amd64.tar.bz2")
    if not url:
        raise RuntimeError(f"MOTIS v{version} has no linux-amd64 asset")
    paths.downloads.mkdir(parents=True, exist_ok=True)
    archive_path = paths.downloads / f"motis-{version}.tar.bz2"
    actual = download(url, archive_path)
    if expected_sha256 and actual != expected_sha256:
        raise RuntimeError(f"MOTIS binary checksum mismatch: {actual}")
    if not expected_sha256:
        log(f"warning: MOTIS v{version} binary is not pinned (sha256 {actual})")
    staging = Path(tempfile.mkdtemp(dir=paths.binaries, prefix=f".{version}-"))
    staging.chmod(0o755)
    with tarfile.open(archive_path, "r:bz2") as archive:
        archive.extractall(staging, filter="data")
    if not (staging / "motis").is_file():
        raise RuntimeError("MOTIS release archive has no top-level motis binary")
    (staging / "motis").chmod(0o755)
    staging.replace(target_dir)
    archive_path.unlink(missing_ok=True)
    return binary


def fetch_dataset(paths: Paths, channel: str, force: bool, state: dict) -> tuple[str, dict] | None:
    tag = CHANNEL_TAGS[channel]
    assets = release_assets(REPOSITORY, tag)
    for name in (DATA_ASSET, DATA_ASSET + ".sha256", "build-info.json"):
        if name not in assets:
            raise RuntimeError(f"release {tag} lacks {name}")
    expected = parse_sha256_file(http_get(assets[DATA_ASSET + ".sha256"]).decode("utf-8"))
    rid = release_id(expected)
    if rid == state.get("active_release") and not force:
        log(f"{tag}: release {rid} already active")
        return None
    target = paths.releases / rid
    if not (target / "data" / "config.yml").is_file():
        paths.downloads.mkdir(parents=True, exist_ok=True)
        archive_path = paths.downloads / f"{rid}.tar.zst"
        actual = download(assets[DATA_ASSET], archive_path)
        if actual != expected:
            archive_path.unlink(missing_ok=True)
            raise RuntimeError(f"dataset checksum mismatch: expected {expected}, got {actual}")
        staging = Path(tempfile.mkdtemp(dir=paths.releases, prefix=f".{rid}-"))
        staging.chmod(0o755)
        tar_path = staging / "data.tar"
        subprocess.run(["zstd", "-q", "-d", str(archive_path), "-o", str(tar_path)], check=True)
        with tarfile.open(tar_path) as archive:
            archive.extractall(staging, members=safe_members(archive), filter="data")
        tar_path.unlink()
        archive_path.unlink(missing_ok=True)
        subprocess.run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(staging)], check=True)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    info = json.loads((target / "data" / "build-info.json").read_text(encoding="utf-8"))
    return rid, info


def write_env(paths: Paths, colour: str, binary: Path, data_dir: Path) -> None:
    paths.env_file(colour).write_text(f"MOTIS_BIN={binary}\nMOTIS_DATA={data_dir}\n", encoding="utf-8")


def wait_healthy(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last = error
        time.sleep(2)
    raise RuntimeError(f"MOTIS on :{port} not healthy: {last}")


def smoke(port: int, info: dict) -> None:
    trips = info.get("smoke_trips") or {}
    if not trips:
        raise RuntimeError("build-info.json has no smoke_trips")
    now = datetime.now(timezone.utc)
    failures = []
    for agency_id, trip in sorted(trips.items()):
        url = plan_url(f"http://127.0.0.1:{port}", trip["from"], trip["to"], smoke_time(trip["timezone"], now))
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                error = smoke_error(json.loads(response.read()), agency_id)
        except (OSError, ValueError) as exc:
            error = f"{agency_id}: {exc}"
        if error:
            failures.append(error)
    if failures:
        raise RuntimeError("smoke test failed: " + "; ".join(failures))


def switch_caddy(caddy_snippet: Path, port: int) -> None:
    tmp = caddy_snippet.with_suffix(".tmp")
    tmp.write_text(caddy_upstream(port), encoding="utf-8")
    tmp.replace(caddy_snippet)
    systemctl("reload", "caddy")


def activate(paths: Paths, caddy_snippet: Path, rid: str, info: dict, strategy: str, state: dict) -> None:
    active_colour = state.get("active_colour")
    candidate = other_colour(active_colour)
    port = COLOURS[candidate]
    data_dir = paths.releases / rid / "data"
    config_path = data_dir / "config.yml"
    config_path.write_text(set_server_port(config_path.read_text(encoding="utf-8"), port), encoding="utf-8")
    binary = ensure_binary(paths, info["motis_version"], info.get("motis_sha256", ""))
    write_env(paths, candidate, binary, data_dir)
    chosen = choose_strategy(strategy, read_mem_available(), bool(active_colour))
    log(f"activating {rid} on {candidate}:{port} ({chosen})")
    if chosen == "stop-first" and active_colour:
        systemctl("stop", f"motis@{active_colour}")
    try:
        systemctl("restart", f"motis@{candidate}")
        wait_healthy(port, timeout=180)
        smoke(port, info)
    except Exception:
        subprocess.run(["systemctl", "stop", f"motis@{candidate}"], check=False)
        if chosen == "stop-first" and active_colour:
            log(f"restoring {active_colour}")
            systemctl("start", f"motis@{active_colour}")
        raise
    switch_caddy(caddy_snippet, port)
    systemctl("enable", f"motis@{candidate}")
    if active_colour:
        subprocess.run(["systemctl", "disable", "--now", f"motis@{active_colour}"], check=False)
    state.update({
        "previous_release": state.get("active_release"),
        "previous_colour": active_colour,
        "active_release": rid,
        "active_colour": candidate,
        "active_built_at": info.get("built_at"),
        "activated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    save_state(paths, state)
    existing = [p.name for p in paths.releases.iterdir() if p.is_dir() and not p.name.startswith(".")]
    for name in releases_to_prune(existing, [state["active_release"], state["previous_release"]]):
        shutil.rmtree(paths.releases / name, ignore_errors=True)
    log(f"active: {rid} built {info.get('built_at')} on {candidate}")


def read_mem_available() -> int | None:
    try:
        return parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except OSError:
        return None


def cmd_update(args: argparse.Namespace, paths: Paths) -> int:
    paths.releases.mkdir(parents=True, exist_ok=True)
    paths.binaries.mkdir(parents=True, exist_ok=True)
    state = load_state(paths)
    fetched = fetch_dataset(paths, args.channel, args.force, state)
    if fetched is None:
        return 0
    rid, info = fetched
    activate(paths, args.caddy_snippet, rid, info, args.strategy, state)
    return 0


def cmd_rollback(args: argparse.Namespace, paths: Paths) -> int:
    state = load_state(paths)
    previous = state.get("previous_release")
    if not previous or not (paths.releases / previous / "data").is_dir():
        log("no previous release on disk")
        return 1
    info = json.loads((paths.releases / previous / "data" / "build-info.json").read_text(encoding="utf-8"))
    activate(paths, args.caddy_snippet, previous, info, args.strategy, state)
    return 0


def cmd_status(args: argparse.Namespace, paths: Paths) -> int:
    print(json.dumps(load_state(paths), indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("MOTIS_ROOT", "/srv/motis")))
    parser.add_argument("--caddy-snippet", type=Path, default=Path("/etc/caddy/motis-upstream.caddy"))
    parser.add_argument("--strategy", choices=["auto", "bluegreen", "stop-first"], default="auto")
    sub = parser.add_subparsers(dest="command", required=True)
    p_update = sub.add_parser("update")
    p_update.add_argument("--channel", choices=sorted(CHANNEL_TAGS), default=os.environ.get("MOTIS_CHANNEL", "production"))
    p_update.add_argument("--force", action="store_true")
    p_update.set_defaults(func=cmd_update)
    sub.add_parser("rollback").set_defaults(func=cmd_rollback)
    sub.add_parser("status").set_defaults(func=cmd_status)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return args.func(args, Paths(args.root))
    except Exception as error:  # noqa: BLE001 - the timer logs and retries
        log(f"error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
