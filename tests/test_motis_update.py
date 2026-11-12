from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import motis_build

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("motis_update", REPO_ROOT / "deploy" / "routing" / "motis_update.py")
motis_update = importlib.util.module_from_spec(_spec)
sys.modules["motis_update"] = motis_update
_spec.loader.exec_module(motis_update)


def _tar(entries: list[tarfile.TarInfo]) -> tarfile.TarFile:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for entry in entries:
            archive.addfile(entry, io.BytesIO(b"x" * entry.size) if entry.isfile() else None)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer)


def _file(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 1
    return info


class MotisUpdateTests(unittest.TestCase):
    def test_port_rewrite_matches_rendered_config(self) -> None:
        rendered = motis_build.render_motis_config(motis_build.load_routing_config())
        rewritten = motis_update.set_server_port(rendered, 8082)
        self.assertIn("server:\n  host: 127.0.0.1\n  port: 8082\n", rewritten)
        self.assertEqual(rewritten.count("port:"), 1)
        # Everything outside the server block is untouched.
        self.assertEqual(rendered.split("osm:", 1)[1], rewritten.split("osm:", 1)[1])

    def test_port_rewrite_requires_server_port(self) -> None:
        with self.assertRaises(ValueError):
            motis_update.set_server_port("osm: x\n", 8081)

    def test_colours_alternate(self) -> None:
        self.assertEqual(motis_update.other_colour(None), "blue")
        self.assertEqual(motis_update.other_colour("blue"), "green")
        self.assertEqual(motis_update.other_colour("green"), "blue")

    def test_sha256_file(self) -> None:
        digest = "a" * 64
        self.assertEqual(motis_update.parse_sha256_file(f"{digest}  motis-data.tar.zst\n"), digest)
        self.assertEqual(motis_update.release_id(digest), "a" * 16)
        for bad in ("", "abc  x", "z" * 64):
            with self.assertRaises(ValueError):
                motis_update.parse_sha256_file(bad)

    def test_strategy(self) -> None:
        low, high = 400 * 1024, 900 * 1024
        self.assertEqual(motis_update.choose_strategy("auto", low, has_active=True), "stop-first")
        self.assertEqual(motis_update.choose_strategy("auto", high, has_active=True), "bluegreen")
        self.assertEqual(motis_update.choose_strategy("auto", None, has_active=True), "stop-first")
        self.assertEqual(motis_update.choose_strategy("stop-first", low, has_active=False), "bluegreen")
        self.assertEqual(motis_update.choose_strategy("bluegreen", low, has_active=True), "bluegreen")
        self.assertEqual(motis_update.parse_meminfo("MemTotal: 1 kB\nMemAvailable:  512000 kB\n"), 512000)

    def test_prune_keeps_active_and_previous(self) -> None:
        self.assertEqual(motis_update.releases_to_prune(["a", "b", "c"], ["b", None]), ["a", "c"])
        self.assertEqual(motis_update.releases_to_prune(["a", "b"], ["a", "b"]), [])

    def test_smoke_matches_ci(self) -> None:
        plan = {"itineraries": [{"legs": [{"mode": "WALK"}, {"mode": "BUS", "tripId": "20261113_08:00_pvta_123"}]}]}
        self.assertIsNone(motis_update.smoke_error(plan, "pvta"))
        self.assertIsNotNone(motis_update.smoke_error(plan, "uta"))
        self.assertIsNotNone(motis_update.smoke_error({"itineraries": []}, "pvta"))
        now = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)  # Friday
        self.assertEqual(
            motis_update.smoke_time("America/New_York", now),
            motis_build.next_weekday_morning("America/New_York", now),
        )

    def test_build_info_carries_smoke_trips(self) -> None:
        config = motis_build.load_routing_config()
        info = motis_build.build_info(config, {}, "production", datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual(sorted(info["smoke_trips"]), sorted(a["id"] for a in config["agencies"]))
        url = motis_update.plan_url("http://127.0.0.1:8081", [1.0, 2.0], [3.0, 4.0], datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertTrue(url.startswith("http://127.0.0.1:8081/api/v6/plan?fromPlace=1.0%2C2.0"))

    def test_archive_members_are_restricted_to_data(self) -> None:
        directory = tarfile.TarInfo("data")
        directory.type = tarfile.DIRTYPE
        good = _tar([directory, _file("data/config.yml")])
        self.assertEqual(len(motis_update.safe_members(good)), 2)
        for bad in (_file("../evil"), _file("etc/passwd")):
            with self.assertRaises(ValueError):
                motis_update.safe_members(_tar([bad]))
        link = tarfile.TarInfo("data/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/shadow"
        with self.assertRaises(ValueError):
            motis_update.safe_members(_tar([link]))

    def test_caddy_upstream(self) -> None:
        upstream = motis_update.caddy_upstream(8081)
        self.assertTrue(upstream.startswith("reverse_proxy 127.0.0.1:8081 {"))
        self.assertIn("header_up -Authorization", upstream)


if __name__ == "__main__":
    unittest.main()
