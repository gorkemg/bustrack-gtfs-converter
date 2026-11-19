from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import motis_build


class MotisBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = motis_build.load_routing_config()

    def test_repository_routing_config_covers_converter_agencies(self) -> None:
        agencies = json.loads((motis_build.REPO_ROOT / "config" / "agencies.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(entry["id"] for entry in agencies),
            sorted(agency["id"] for agency in self.config["agencies"]),
        )

    def test_validate_rejects_bad_bbox_and_http(self) -> None:
        broken = copy.deepcopy(self.config)
        broken["agencies"][0]["bbox"] = [1, 2, 0, 3]
        with self.assertRaises(ValueError):
            motis_build.validate_routing_config(broken)
        broken = copy.deepcopy(self.config)
        broken["agencies"][0]["realtime"] = ["http://insecure.example"]
        with self.assertRaises(ValueError):
            motis_build.validate_routing_config(broken)
        broken = copy.deepcopy(self.config)
        broken["agencies"].append(copy.deepcopy(broken["agencies"][0]))
        with self.assertRaises(ValueError):
            motis_build.validate_routing_config(broken)

    def test_render_config_tags_datasets_by_agency(self) -> None:
        rendered = motis_build.render_motis_config(self.config)
        self.assertIn('osm: "merged.osm.pbf"', rendered)
        self.assertIn('    pvta:\n      path: "pvta.gtfs.zip"\n', rendered)
        self.assertIn('    uta:\n      path: "uta.gtfs.zip"\n      rt:\n        - url: "https://apps.rideuta.com/tms/gtfs/TripUpdate"', rendered)
        self.assertNotIn("pvta.gtfs.zip\"\n      rt:", rendered)
        self.assertIn("host: 127.0.0.1", rendered)
        self.assertIn("geocoding: true", rendered)

    def test_osm_clip_commands_use_bbox(self) -> None:
        commands = motis_build.osm_clip_commands(self.config)
        self.assertEqual(commands[0][:2], ["osmium", "extract"])
        self.assertIn("-72.9822,41.8675,-72.139,42.5247", commands[0])
        self.assertEqual(commands[0][-1], "osm/pvta-clip.osm.pbf")

    def test_next_weekday_morning_skips_weekend(self) -> None:
        friday_evening_utc = datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc)
        when = motis_build.next_weekday_morning("America/New_York", friday_evening_utc)
        self.assertEqual((when.weekday(), when.hour, when.minute), (0, 8, 0))
        self.assertEqual(when.date().isoformat(), "2026-11-16")

    def test_plan_url_uses_utc_time(self) -> None:
        when = datetime(2026, 11, 16, 8, 0, tzinfo=motis_build.ZoneInfo("America/Denver"))
        url = motis_build.plan_url("http://127.0.0.1:8080/", [40.7, -111.8], [40.8, -111.9], when)
        self.assertTrue(url.startswith("http://127.0.0.1:8080/api/v6/plan?"))
        self.assertIn("time=2026-11-16T15%3A00%3A00Z", url)
        self.assertIn("fromPlace=40.7%2C-111.8", url)

    def test_smoke_check_requires_tagged_transit_leg(self) -> None:
        walk_only = {"itineraries": [{"legs": [{"mode": "WALK"}]}]}
        self.assertIsNotNone(motis_build.smoke_check(walk_only, "pvta"))
        wrong_tag = {"itineraries": [{"legs": [{"mode": "BUS", "tripId": "20261116_08:00_uta_123"}]}]}
        self.assertIsNotNone(motis_build.smoke_check(wrong_tag, "pvta"))
        good = {"itineraries": [{"legs": [{"mode": "WALK"}, {"mode": "BUS", "tripId": "20261116_08:00_pvta_t1"}]}]}
        self.assertIsNone(motis_build.smoke_check(good, "pvta"))

    def test_build_info_hashes_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pvta.gtfs.zip"
            path.write_bytes(b"abc")
            info = motis_build.build_info(self.config, {path.name: path}, "beta", datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual(info["inputs"]["pvta.gtfs.zip"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(info["channel"], "beta")
        self.assertEqual(info["built_at"], "2026-11-12T00:00:00Z")
        self.assertEqual(info["motis_version"], self.config["motis"]["version"])
        first = self.config["agencies"][0]
        self.assertEqual(info["smoke_trips"][first["id"]]["from"], first["smoke_trip"]["from"])


if __name__ == "__main__":
    unittest.main()
