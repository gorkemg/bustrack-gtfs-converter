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


class StatusReportTests(unittest.TestCase):
    NOW = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)

    def test_meminfo_fields(self) -> None:
        text = "MemTotal: 1000 kB\nMemFree: 1 kB\nMemAvailable: 400 kB\nSwapTotal: 512 kB\nSwapFree: 500 kB\n"
        self.assertEqual(
            motis_update.parse_meminfo_all(text),
            {"total_kb": 1000, "available_kb": 400, "swap_total_kb": 512, "swap_free_kb": 500},
        )

    def test_apt_check(self) -> None:
        self.assertEqual(motis_update.parse_apt_check("12;3"), (12, 3))
        self.assertIsNone(motis_update.parse_apt_check("E: something"))

    def test_systemctl_show(self) -> None:
        unit = motis_update.parse_systemctl_show("ActiveState=active\nNRestarts=2\nResult=oom-kill\nMemoryCurrent=[not set]\n")
        self.assertEqual(unit, {"active_state": "active", "result": "oom-kill", "n_restarts": 2, "memory_bytes": None})

    def test_record_run_counts_failures_and_resets(self) -> None:
        import urllib.error

        state = motis_update.record_run({}, self.NOW, urllib.error.URLError("github down"))
        state = motis_update.record_run(state, self.NOW, RuntimeError("smoke test failed"))
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertEqual(state["last_error_kind"], "node")
        self.assertNotIn("last_success_at", state)
        state = motis_update.record_run(state, self.NOW, None)
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(state["last_success_at"], "2026-11-13T10:00:00Z")
        self.assertNotIn("last_error_kind", state)

    def test_classify_error(self) -> None:
        import urllib.error

        self.assertEqual(motis_update.classify_error(urllib.error.URLError("x")), "upstream")
        self.assertEqual(motis_update.classify_error(TimeoutError()), "upstream")
        self.assertEqual(motis_update.classify_error(RuntimeError("dataset checksum mismatch")), "node")

    def test_build_status_shape(self) -> None:
        state = {"active_release": "abc", "active_colour": "blue", "activated_by": "rollback", "consecutive_failures": 1, "last_error_kind": "upstream"}
        snapshot = motis_update.build_status(
            now=self.NOW, state=state, channel="beta", boot_id="b-1", uptime_seconds=3600.4,
            meminfo={"total_kb": 1000, "available_kb": 400}, disk=(100, 40), load1=0.5,
            reboot_required_since=datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc),
            updates={"pending": 3, "security": 1}, unit={"active_state": "active"},
        )
        self.assertEqual(snapshot["v"], 1)
        self.assertEqual(snapshot["host"]["booted_at"], "2026-11-13T09:00:00Z")
        self.assertEqual(snapshot["host"]["uptime_s"], 3600)
        self.assertTrue(snapshot["host"]["reboot_required"])
        self.assertEqual(snapshot["host"]["reboot_required_since"], "2026-11-12T06:00:00Z")
        self.assertEqual(snapshot["host"]["disk"], {"total_bytes": 100, "free_bytes": 40})
        self.assertEqual(snapshot["motis"]["activated_by"], "rollback")
        self.assertEqual(snapshot["updater"], {"last_check_at": None, "last_success_at": None, "consecutive_failures": 1, "last_error_kind": "upstream"})
        self.assertNotIn("MOTIS_TOKEN", str(snapshot))

    def test_build_status_tolerates_missing_probes(self) -> None:
        snapshot = motis_update.build_status(
            now=self.NOW, state={}, channel="production", boot_id=None, uptime_seconds=None,
            meminfo={}, disk=None, load1=None, reboot_required_since=None, updates=None, unit=None,
        )
        self.assertIsNone(snapshot["host"]["booted_at"])
        self.assertFalse(snapshot["host"]["reboot_required"])
        self.assertIsNone(snapshot["host"]["memory"])
        self.assertEqual(snapshot["updater"]["consecutive_failures"], 0)


class ReportCommandTests(unittest.TestCase):
    def test_report_writes_world_readable_snapshot(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            paths = motis_update.Paths(Path(root))
            paths.state.write_text(json.dumps({"active_release": "r1"}), encoding="utf-8")
            args = motis_update.parse_args(["--root", root, "report", "--channel", "beta"])
            self.assertEqual(args.func(args, paths), 0)
            data = json.loads(paths.status_file.read_text(encoding="utf-8"))
            self.assertEqual(data["motis"]["active_release"], "r1")
            self.assertEqual(data["motis"]["channel"], "beta")
            self.assertEqual(paths.status_file.stat().st_mode & 0o777, 0o644)
