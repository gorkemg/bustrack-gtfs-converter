#!/usr/bin/env python3
"""Measure how many plans per second the routing node serves.

Runs on the node itself against the active MOTIS colour on localhost, so it
loads only the routing node: no Caddy, no backend, no rate limits. Each step
keeps N concurrent clients busy for a fixed time with random trips between
well-known places of both agencies and reports throughput, latency and the
MOTIS memory afterwards.

    sudo python3 loadtest.py                 # steps 1 2 4 8 16, 20 s each
    sudo python3 loadtest.py --steps 1 4 --seconds 10

Stop early with Ctrl-C. A step with errors or a p95 above two seconds means the
node is past its comfortable capacity.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

COLOURS = {"blue": 8081, "green": 8082}
STATE = Path("/srv/motis/state.json")

PLACES = {
    "pvta": [
        (42.3756, -72.5194),  # Amherst Center
        (42.3868, -72.5300),  # Haigis Mall, UMass
        (42.3555, -72.5476),  # Hampshire Mall
        (42.3188, -72.6304),  # Northampton
        (42.2043, -72.6162),  # Holyoke
        (42.1062, -72.5935),  # Springfield Union Station
        (42.1497, -72.6075),  # Chicopee
        (42.3399, -72.5023),  # Belchertown Rd
    ],
    "uta": [
        (40.7608, -111.8910),  # Downtown Salt Lake City
        (40.7649, -111.8421),  # University of Utah
        (40.2338, -111.6585),  # Provo
        (41.2230, -111.9738),  # Ogden
        (40.5649, -111.8389),  # Sandy
        (40.7899, -111.9791),  # Airport
        (40.6669, -111.8880),  # Murray
        (40.5246, -111.8638),  # Draper
    ],
}


def active_port() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    return COLOURS[state["active_colour"]]


def plan_url(port: int, rng: random.Random) -> str:
    places = PLACES[rng.choice(sorted(PLACES))]
    origin, destination = rng.sample(places, 2)
    query = urllib.parse.urlencode({
        "fromPlace": f"{origin[0]},{origin[1]}",
        "toPlace": f"{destination[0]},{destination[1]}",
        "numItineraries": "5",
    })
    return f"http://127.0.0.1:{port}/api/v6/plan?{query}"


def run_step(port: int, clients: int, seconds: float) -> dict:
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def worker(seed: int) -> None:
        nonlocal errors
        rng = random.Random(seed)
        while time.monotonic() < deadline:
            url = plan_url(port, rng)
            start = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=15) as response:
                    response.read()
                    ok = response.status == 200
            except Exception:
                ok = False
            elapsed = time.monotonic() - start
            with lock:
                if ok:
                    latencies.append(elapsed)
                else:
                    errors += 1

    threads = [threading.Thread(target=worker, args=(clients * 1000 + i,)) for i in range(clients)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration = time.monotonic() - started

    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return float("nan")
        return latencies[min(len(latencies) - 1, int(p * len(latencies)))] * 1000

    return {
        "clients": clients,
        "plans_per_s": len(latencies) / duration,
        "p50_ms": statistics.median(latencies) * 1000 if latencies else float("nan"),
        "p95_ms": pct(0.95),
        "max_ms": latencies[-1] * 1000 if latencies else float("nan"),
        "errors": errors,
    }


def motis_rss_mb() -> str:
    try:
        output = subprocess.run(["ps", "-C", "motis", "-o", "rss="], capture_output=True, text=True, check=False).stdout
        values = [int(v) for v in output.split()]
        return f"{sum(values) / 1024:.0f} MB" if values else "n/a"
    except OSError:
        return "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", type=int, nargs="+", default=[1, 2, 4, 8, 16])
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--port", type=int, default=None, help="default: active colour from state.json")
    args = parser.parse_args()

    port = args.port or active_port()
    print(f"MOTIS on 127.0.0.1:{port}, RSS before: {motis_rss_mb()}")
    print(f"{'clients':>7} {'plans/s':>8} {'p50 ms':>7} {'p95 ms':>7} {'max ms':>7} {'errors':>6}  RSS")
    for clients in args.steps:
        result = run_step(port, clients, args.seconds)
        print(f"{result['clients']:>7} {result['plans_per_s']:>8.1f} {result['p50_ms']:>7.0f} "
              f"{result['p95_ms']:>7.0f} {result['max_ms']:>7.0f} {result['errors']:>6}  {motis_rss_mb()}")
        time.sleep(2)


if __name__ == "__main__":
    main()
