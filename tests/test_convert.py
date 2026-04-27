from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import convert


class ConvertTests(unittest.TestCase):
    def test_parse_release_metadata_from_body_reads_hidden_markers(self) -> None:
        body = "\n".join(
            [
                "Automated GTFS release for pvta.",
                "<!-- source_last_modified: Thu, 17 Apr 2026 10:00:00 GMT -->",
                "<!-- source_etag: etag-123 -->",
            ]
        )

        metadata = convert.parse_release_metadata_from_body(body)

        self.assertEqual(metadata["source_last_modified"], "Thu, 17 Apr 2026 10:00:00 GMT")
        self.assertEqual(metadata["source_etag"], "etag-123")

    def test_write_release_notes_persists_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            release_notes_path = Path(temp_dir) / "pvta.release-notes.md"

            with patch.dict(
                "os.environ",
                {
                    "GITHUB_REPOSITORY": "example/repo",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_SHA": "abc123",
                    "GITHUB_SERVER_URL": "https://github.com",
                },
                clear=False,
            ):
                convert.write_release_notes(
                    release_notes_path,
                    "pvta",
                    {
                        "source_last_modified": "Thu, 17 Apr 2026 10:00:00 GMT",
                        "source_etag": "etag-123",
                    },
                    16,
                    653005,
                )

            content = release_notes_path.read_text(encoding="utf-8")
            self.assertIn("Validated tables: 16", content)
            self.assertIn("Validated rows: 653005", content)
            self.assertIn("<!-- source_last_modified: Thu, 17 Apr 2026 10:00:00 GMT -->", content)
            self.assertIn("<!-- source_etag: etag-123 -->", content)

    def test_extract_feed_date_range_prefers_feed_info(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = Path(temp_dir)
            (input_dir / "feed_info.txt").write_text(
                "feed_publisher_name,feed_start_date,feed_end_date\nAgency,20260101,20261231\n",
                encoding="utf-8",
            )
            (input_dir / "calendar.txt").write_text(
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20250101,20251231\n",
                encoding="utf-8",
            )

            feed_start_date, feed_end_date = convert.extract_feed_date_range(input_dir)

            self.assertEqual(feed_start_date, "20260101")
            self.assertEqual(feed_end_date, "20261231")

    def test_extract_feed_date_range_falls_back_to_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = Path(temp_dir)
            (input_dir / "calendar.txt").write_text(
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20250101,20251231\n",
                encoding="utf-8",
            )

            feed_start_date, feed_end_date = convert.extract_feed_date_range(input_dir)

            self.assertEqual(feed_start_date, "20250101")
            self.assertEqual(feed_end_date, "20251231")

    def test_create_app_metadata_includes_validation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = Path(temp_dir) / "pvta"
            input_dir.mkdir()
            (input_dir / "calendar.txt").write_text(
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20250101,20251231\n",
                encoding="utf-8",
            )

            connection = sqlite3.connect(":memory:")
            try:
                with patch.dict(
                    "os.environ",
                    {"GITHUB_SHA": "abc123", "GITHUB_RUN_ID": "987654"},
                    clear=False,
                ):
                    convert.create_app_metadata(connection, input_dir, "pvta")

                rows = dict(connection.execute("SELECT key, value FROM app_metadata").fetchall())
            finally:
                connection.close()

            self.assertEqual(rows["agency_id"], "pvta")
            self.assertEqual(rows["git_commit_sha"], "abc123")
            self.assertEqual(rows["workflow_run_id"], "987654")
            self.assertEqual(rows["feed_start_date"], "20250101")
            self.assertEqual(rows["feed_end_date"], "20251231")
            self.assertTrue(rows["build_id"])
            self.assertEqual(str(uuid.UUID(rows["build_id"])), rows["build_id"])

    def test_create_recommended_indexes_adds_requested_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gtfs_dir = Path(temp_dir)
            (gtfs_dir / "trips.txt").write_text(
                "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                encoding="utf-8",
            )
            (gtfs_dir / "routes.txt").write_text(
                "route_id,route_short_name\nR1,1\n",
                encoding="utf-8",
            )
            (gtfs_dir / "stops.txt").write_text(
                "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n",
                encoding="utf-8",
            )
            (gtfs_dir / "stop_times.txt").write_text(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:01:00,1,1\n",
                encoding="utf-8",
            )
            (gtfs_dir / "calendar_dates.txt").write_text(
                "service_id,date,exception_type\nS1,20250101,1\n",
                encoding="utf-8",
            )
            (gtfs_dir / "calendar.txt").write_text(
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20250101,20251231\n",
                encoding="utf-8",
            )

            gtfs_files = [
                gtfs_dir / "calendar.txt",
                gtfs_dir / "calendar_dates.txt",
                gtfs_dir / "routes.txt",
                gtfs_dir / "stop_times.txt",
                gtfs_dir / "stops.txt",
                gtfs_dir / "trips.txt",
            ]

            connection = sqlite3.connect(":memory:")
            try:
                convert.create_gtfs_tables(connection, gtfs_files)
                convert.create_recommended_indexes(connection, gtfs_files)

                index_names = {
                    row[1]
                    for row in connection.execute(
                        "SELECT type, name FROM sqlite_master WHERE type = 'index'"
                    ).fetchall()
                }
            finally:
                connection.close()

            self.assertIn("idx_trips_trip_id", index_names)
            self.assertIn("idx_routes_route_id", index_names)
            self.assertIn("idx_stops_stop_id", index_names)
            self.assertIn("idx_stop_times_stop_id_departure_time", index_names)
            self.assertIn("idx_stop_times_stop_id_arrival_time", index_names)
            self.assertIn("idx_calendar_dates_date_service_id", index_names)
            self.assertIn("idx_calendar_start_date_end_date", index_names)

    def test_enrich_route_realtime_ids_defaults_to_route_id_and_indexes_column(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE routes (route_id TEXT, route_short_name TEXT)"
            )
            connection.executemany(
                "INSERT INTO routes (route_id, route_short_name) VALUES (?, ?)",
                [("R1", "1"), ("R2", "2")],
            )

            convert.enrich_route_realtime_ids(connection, "uta")

            rows = connection.execute(
                "SELECT route_id, route_rt_id FROM routes ORDER BY route_id"
            ).fetchall()
            index_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        finally:
            connection.close()

        self.assertEqual(rows, [("R1", "R1"), ("R2", "R2")])
        self.assertIn("idx_routes_route_rt_id", index_names)

    def test_enrich_route_realtime_ids_applies_pvta_route_details_mapping(self) -> None:
        xml_payload = (
            b"<ArrayOfRoute>"
            b"<Route><RouteAbbreviation>B43</RouteAbbreviation><RouteId>10043</RouteId></Route>"
            b"</ArrayOfRoute>"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "pvta_routedetails.xml"
            connection = sqlite3.connect(":memory:")
            try:
                connection.execute(
                    "CREATE TABLE routes (route_id TEXT, route_short_name TEXT)"
                )
                connection.executemany(
                    "INSERT INTO routes (route_id, route_short_name) VALUES (?, ?)",
                    [("B43", "B43"), ("R2", "2")],
                )

                with patch("scripts.convert.urlopen", return_value=BytesIO(xml_payload)):
                    convert.enrich_route_realtime_ids(connection, "pvta", cache_path)

                rows = connection.execute(
                    "SELECT route_id, route_rt_id FROM routes ORDER BY route_id"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(rows, [("B43", "10043"), ("R2", "R2")])
            self.assertEqual(cache_path.read_bytes(), xml_payload)

    def test_pvta_route_details_uses_cache_when_download_fails(self) -> None:
        xml_payload = (
            b"<ArrayOfRoute>"
            b"<Route><RouteAbbreviation>B43</RouteAbbreviation><RouteId>10043</RouteId></Route>"
            b"</ArrayOfRoute>"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "pvta_routedetails.xml"
            cache_path.write_bytes(xml_payload)

            with patch("scripts.convert.urlopen", side_effect=URLError("offline")):
                loaded_payload = convert.load_pvta_route_details_xml(cache_path)

        self.assertEqual(loaded_payload, xml_payload)

    def test_build_http_request_uses_browser_user_agent(self) -> None:
        request = convert.build_http_request("https://example.test/feed.zip")

        self.assertEqual(request.get_header("User-agent"), convert.USER_AGENT)

    def test_fetch_release_source_metadata_from_github_returns_none_for_404(self) -> None:
        not_found_error = HTTPError(
            url="https://api.github.com/repos/example/repo/releases/tags/uta",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "example/repo"}, clear=False):
            with patch("scripts.convert.urlopen", side_effect=not_found_error):
                release_metadata = convert.fetch_release_source_metadata_from_github("uta")

        self.assertIsNone(release_metadata)

    def test_download_and_extract_zip_populates_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "agency"
            archive_bytes = BytesIO()
            with zipfile.ZipFile(archive_bytes, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("stops.txt", "stop_id,stop_name\n1,Stop A\n")

            archive_bytes.seek(0)

            with patch("scripts.convert.urlopen", return_value=archive_bytes):
                convert.download_and_extract_zip("https://example.test/feed.zip", target_dir)

            self.assertTrue((target_dir / "stops.txt").exists())

    def test_download_and_extract_zip_flattens_nested_txt_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "agency"
            archive_bytes = BytesIO()
            with zipfile.ZipFile(archive_bytes, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("nested/stops.txt", "stop_id,stop_name\n1,Stop A\n")
                archive.writestr("nested/trips.txt", "route_id,service_id,trip_id\nR1,S1,T1\n")

            archive_bytes.seek(0)

            with patch("scripts.convert.urlopen", return_value=archive_bytes):
                convert.download_and_extract_zip("https://example.test/feed.zip", target_dir)

            self.assertTrue((target_dir / "stops.txt").exists())
            self.assertTrue((target_dir / "trips.txt").exists())
            self.assertFalse((target_dir / "nested").joinpath("stops.txt").exists())

    def test_zip_sqlite_file_creates_expected_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "sample.sqlite"
            sqlite_path.write_bytes(b"sqlite-bytes")

            zip_path = convert.zip_sqlite_file(sqlite_path)

            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(archive.namelist(), ["sample.sqlite"])

    def test_cleanup_extracted_folder_removes_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "agency"
            target_dir.mkdir()
            (target_dir / "file.txt").write_text("data", encoding="utf-8")

            convert.cleanup_extracted_folder(target_dir)

            self.assertFalse(target_dir.exists())

    def test_validate_database_counts_rows_and_returns_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_dir = Path(temp_dir) / "csv"
            csv_dir.mkdir()
            (csv_dir / "stops.txt").write_text(
                "stop_id,stop_name\n1,Stop A\n",
                encoding="utf-8",
            )
            (csv_dir / "trips.txt").write_text(
                "trip_id,route_id\n10,R1\n",
                encoding="utf-8",
            )
            (csv_dir / "stop_times.txt").write_text(
                "trip_id,arrival_time,stop_id\n10,08:00:00,1\n",
                encoding="utf-8",
            )

            sqlite_path = Path(temp_dir) / "test.sqlite"
            connection = sqlite3.connect(sqlite_path)
            try:
                connection.execute("CREATE TABLE stops (stop_id TEXT, stop_name TEXT)")
                connection.execute("INSERT INTO stops VALUES ('1', 'Stop A')")
                connection.execute("CREATE TABLE trips (trip_id TEXT, route_id TEXT)")
                connection.execute("INSERT INTO trips VALUES ('10', 'R1')")
                connection.execute(
                    "CREATE TABLE stop_times (trip_id TEXT, arrival_time TEXT, stop_id TEXT)"
                )
                connection.execute("INSERT INTO stop_times VALUES ('10', '08:00:00', '1')")
                connection.commit()
            finally:
                connection.close()

            validated_tables, total_rows = convert.validate_database(sqlite_path, csv_dir)

            self.assertEqual(validated_tables, 3)
            self.assertEqual(total_rows, 3)

    def test_validate_database_raises_on_row_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_dir = Path(temp_dir) / "csv"
            csv_dir.mkdir()
            (csv_dir / "stops.txt").write_text(
                "stop_id,stop_name\n1,Stop A\n2,Stop B\n",
                encoding="utf-8",
            )
            (csv_dir / "trips.txt").write_text(
                "trip_id,route_id\n10,R1\n",
                encoding="utf-8",
            )
            (csv_dir / "stop_times.txt").write_text(
                "trip_id,arrival_time,stop_id\n10,08:00:00,1\n",
                encoding="utf-8",
            )

            sqlite_path = Path(temp_dir) / "test.sqlite"
            connection = sqlite3.connect(sqlite_path)
            try:
                connection.execute("CREATE TABLE stops (stop_id TEXT, stop_name TEXT)")
                connection.execute("INSERT INTO stops VALUES ('1', 'Stop A')")
                connection.execute("CREATE TABLE trips (trip_id TEXT, route_id TEXT)")
                connection.execute("INSERT INTO trips VALUES ('10', 'R1')")
                connection.execute(
                    "CREATE TABLE stop_times (trip_id TEXT, arrival_time TEXT, stop_id TEXT)"
                )
                connection.execute("INSERT INTO stop_times VALUES ('10', '08:00:00', '1')")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(SystemExit):
                convert.validate_database(sqlite_path, csv_dir)

    def test_needs_update_returns_false_when_source_metadata_is_unchanged(self) -> None:
        upstream_metadata = {
            "source_last_modified": "Thu, 17 Apr 2026 10:00:00 GMT",
            "source_etag": "etag-123",
        }
        release_metadata = {
            "source_last_modified": "2026-04-17T10:00:00+00:00",
            "source_etag": "etag-123",
            "released_at": "2026-04-17T11:00:00+00:00",
        }

        with patch("scripts.convert.fetch_source_metadata", return_value=upstream_metadata):
            with patch(
                "scripts.convert.get_last_successful_release_metadata",
                return_value=release_metadata,
            ):
                should_update, actual_upstream_metadata, actual_release_metadata, decision_reason = convert.needs_update(
                    "pvta", "https://example.test/pvta.zip"
                )

        self.assertFalse(should_update)
        self.assertEqual(actual_upstream_metadata, upstream_metadata)
        self.assertEqual(actual_release_metadata, release_metadata)
        self.assertEqual(decision_reason, "source_etag_unchanged")

    def test_needs_update_returns_true_when_source_last_modified_is_newer(self) -> None:
        upstream_metadata = {
            "source_last_modified": "Fri, 18 Apr 2026 10:00:00 GMT",
            "source_etag": "",
        }
        release_metadata = {
            "source_last_modified": "2026-04-17T10:00:00+00:00",
            "source_etag": "",
            "released_at": "2026-04-17T11:00:00+00:00",
        }

        with patch("scripts.convert.fetch_source_metadata", return_value=upstream_metadata):
            with patch(
                "scripts.convert.get_last_successful_release_metadata",
                return_value=release_metadata,
            ):
                should_update, _, _, decision_reason = convert.needs_update(
                    "pvta", "https://example.test/pvta.zip"
                )

        self.assertTrue(should_update)
        self.assertEqual(decision_reason, "source_last_modified_newer")

    def test_update_release_cache_writes_expected_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "release_cache.json"
            upstream_metadata = {
                "source_last_modified": "Thu, 17 Apr 2026 10:00:00 GMT",
                "source_etag": "etag-123",
            }

            convert.update_release_cache("pvta", upstream_metadata, cache_path=cache_path)

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("pvta", payload)
            self.assertEqual(payload["pvta"]["source_last_modified"], "Thu, 17 Apr 2026 10:00:00 GMT")
            self.assertEqual(payload["pvta"]["source_etag"], "etag-123")

    def test_main_runs_end_to_end_for_local_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            csv_dir = temp_root / "pvta"
            csv_dir.mkdir()
            (csv_dir / "stops.txt").write_text(
                "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n",
                encoding="utf-8",
            )
            (csv_dir / "trips.txt").write_text(
                "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                encoding="utf-8",
            )
            (csv_dir / "stop_times.txt").write_text(
                "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\n",
                encoding="utf-8",
            )

            output_path = temp_root / "pvta.sqlite"

            with patch("sys.argv", ["convert.py", str(csv_dir), "--output", str(output_path)]):
                exit_code = convert.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.with_suffix(".sqlite.zip").exists())

    def test_main_forces_download_when_release_check_fails_and_folder_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            input_dir = temp_root / "uta"
            output_path = temp_root / "uta.sqlite"

            def fake_download(url: str, target_dir: Path) -> None:
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "stops.txt").write_text(
                    "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n",
                    encoding="utf-8",
                )
                (target_dir / "trips.txt").write_text(
                    "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                    encoding="utf-8",
                )
                (target_dir / "stop_times.txt").write_text(
                    "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\n",
                    encoding="utf-8",
                )

            with patch(
                "scripts.convert.load_agencies_config",
                return_value=[{"id": "uta", "url": "https://example.test/uta.zip"}],
            ):
                with patch(
                    "scripts.convert.needs_update",
                    side_effect=HTTPError(
                        url="https://example.test/uta.zip",
                        code=406,
                        msg="Not Acceptable",
                        hdrs=None,
                        fp=None,
                    ),
                ):
                    with patch("scripts.convert.download_and_extract_zip", side_effect=fake_download):
                        with patch(
                            "sys.argv",
                            [
                                "convert.py",
                                "--agency",
                                "uta",
                                "--output",
                                str(output_path),
                            ],
                        ):
                            exit_code = convert.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.with_suffix(".sqlite.zip").exists())

    def test_main_force_update_bypasses_release_check_and_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            output_path = temp_root / "uta.sqlite"

            def fake_download(url: str, target_dir: Path) -> None:
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "stops.txt").write_text(
                    "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n",
                    encoding="utf-8",
                )
                (target_dir / "trips.txt").write_text(
                    "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                    encoding="utf-8",
                )
                (target_dir / "stop_times.txt").write_text(
                    "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\n",
                    encoding="utf-8",
                )

            with patch(
                "scripts.convert.load_agencies_config",
                return_value=[{"id": "uta", "url": "https://example.test/uta.zip"}],
            ):
                with patch(
                    "scripts.convert.needs_update",
                    side_effect=AssertionError("needs_update must not be called when --force-update is set"),
                ):
                    with patch("scripts.convert.download_and_extract_zip", side_effect=fake_download):
                        with patch(
                            "sys.argv",
                            [
                                "convert.py",
                                "--agency",
                                "uta",
                                "--force-update",
                                "--output",
                                str(output_path),
                            ],
                        ):
                            exit_code = convert.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.with_suffix(".sqlite.zip").exists())


if __name__ == "__main__":
    unittest.main()
