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

    def test_pvta_route_details_cache_path_is_project_relative(self) -> None:
        cache_path = convert.get_pvta_route_details_cache_path()

        self.assertEqual(
            cache_path,
            convert.get_repo_root() / "internal" / "assets" / "cache" / "pvta_routedetails.xml",
        )

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

    def test_enrich_route_realtime_ids_uses_normalized_short_name_matching_priority(self) -> None:
        xml_payload = (
            b"<ArrayOfRoute>"
            b"<Route><RouteAbbreviation>B43</RouteAbbreviation><ShortName> B 43 </ShortName><RouteId>10043</RouteId></Route>"
            b"<Route><RouteAbbreviation>T1</RouteAbbreviation><ShortName>801</ShortName><RouteId>20801</RouteId></Route>"
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
                    [(" b43 ", "unused"), ("801", "T1"), ("LOCAL801", " 8 0 1 "), ("T1", "misc")],
                )

                with patch("scripts.convert.urlopen", return_value=BytesIO(xml_payload)):
                    convert.enrich_route_realtime_ids(connection, "pvta", cache_path)

                rows = connection.execute(
                    "SELECT route_id, route_short_name, route_rt_id FROM routes ORDER BY route_id"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(
            rows,
            [
                (" b43 ", "unused", "10043"),
                ("801", "T1", "20801"),
                ("LOCAL801", " 8 0 1 ", "20801"),
                ("T1", "misc", "20801"),
            ],
        )

    def test_parse_pvta_route_details_mapping_accepts_json_payload(self) -> None:
        payload = b'[{"RouteAbbreviation":"B43","RouteId":10043}]'

        mapping = convert.parse_pvta_route_details_mapping(payload)

        self.assertEqual(mapping, {"B43": "10043"})

    def test_parse_pvta_route_details_records_reads_short_name(self) -> None:
        payload = (
            b"<ArrayOfRoute>"
            b"<Route><RouteAbbreviation>B43</RouteAbbreviation><ShortName>943</ShortName><RouteId>10043</RouteId></Route>"
            b"</ArrayOfRoute>"
        )

        records = convert.parse_pvta_route_details_records(payload)

        self.assertEqual(
            records,
            [
                {
                    "route_rt_id": "10043",
                    "route_abbreviation": "B43",
                    "short_name": "943",
                }
            ],
        )

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
                convert.create_canonical_route_tables(connection)
                convert.create_canonical_stop_counterpart_table(connection)
                connection.execute(
                    "INSERT INTO canonical_routes VALUES ('R1', 0, 'Label', '1', '2', 100.0)"
                )
                connection.commit()
            finally:
                connection.close()

            validated_tables, total_rows = convert.validate_database(sqlite_path, csv_dir)

            # Derived tables are sanity-checked but not counted; an empty
            # counterpart table is legitimate (loop-only feeds).
            self.assertEqual(validated_tables, 3)
            self.assertEqual(total_rows, 3)

    def test_validate_database_raises_when_canonical_routes_missing_or_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_dir = Path(temp_dir) / "csv"
            csv_dir.mkdir()
            (csv_dir / "stops.txt").write_text(
                "stop_id,stop_name\n1,Stop A\n",
                encoding="utf-8",
            )

            sqlite_path = Path(temp_dir) / "test.sqlite"
            connection = sqlite3.connect(sqlite_path)
            try:
                connection.execute("CREATE TABLE stops (stop_id TEXT, stop_name TEXT)")
                connection.execute("INSERT INTO stops VALUES ('1', 'Stop A')")
                connection.commit()
            finally:
                connection.close()

            # Missing canonical tables must fail validation.
            with self.assertRaises(SystemExit):
                convert.validate_database(sqlite_path, csv_dir)

            connection = sqlite3.connect(sqlite_path)
            try:
                convert.create_canonical_route_tables(connection)
                convert.create_canonical_stop_counterpart_table(connection)
                connection.commit()
            finally:
                connection.close()

            # Present but empty canonical_routes must fail as well.
            with self.assertRaises(SystemExit):
                convert.validate_database(sqlite_path, csv_dir)

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
                "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n2,Stop B,1.1,2.0\n",
                encoding="utf-8",
            )
            (csv_dir / "trips.txt").write_text(
                "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                encoding="utf-8",
            )
            (csv_dir / "stop_times.txt").write_text(
                "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\nT1,08:05:00,2,2\n",
                encoding="utf-8",
            )

            output_path = temp_root / "pvta.sqlite"

            with patch("sys.argv", ["convert.py", str(csv_dir), "--output", str(output_path)]):
                exit_code = convert.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.with_suffix(".sqlite.zip").exists())

            connection = sqlite3.connect(output_path)
            try:
                canonical_route_count = connection.execute(
                    "SELECT COUNT(*) FROM canonical_routes"
                ).fetchone()[0]
                schema_version = connection.execute(
                    "SELECT value FROM app_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                counterpart_table_exists = convert.table_exists(
                    connection, "canonical_stop_counterparts"
                )
            finally:
                connection.close()

            self.assertEqual(canonical_route_count, 1)
            self.assertEqual(schema_version, "1.2")
            self.assertTrue(counterpart_table_exists)

    def test_main_uses_agency_for_local_folder_without_remote_update_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            csv_dir = temp_root / "google_transit_2"
            csv_dir.mkdir()
            (csv_dir / "routes.txt").write_text(
                "route_id,route_short_name\nB43,B43\nR2,2\n",
                encoding="utf-8",
            )
            (csv_dir / "stops.txt").write_text(
                "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n2,Stop B,1.1,2.0\n",
                encoding="utf-8",
            )
            (csv_dir / "trips.txt").write_text(
                "route_id,service_id,trip_id,trip_headsign,shape_id\nB43,S1,T1,Headsign,SH1\n",
                encoding="utf-8",
            )
            (csv_dir / "stop_times.txt").write_text(
                "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\nT1,08:05:00,2,2\n",
                encoding="utf-8",
            )
            output_path = temp_root / "pvta.sqlite"
            xml_payload = (
                b"<ArrayOfRoute>"
                b"<Route><RouteAbbreviation>B43</RouteAbbreviation><RouteId>10043</RouteId></Route>"
                b"</ArrayOfRoute>"
            )

            with patch(
                "scripts.convert.load_agencies_config",
                return_value=[{"id": "pvta", "url": "https://example.test/pvta.zip"}],
            ):
                with patch(
                    "scripts.convert.needs_update",
                    side_effect=AssertionError("local input_path must not run remote update check"),
                ):
                    with patch(
                        "scripts.convert.get_pvta_route_details_cache_path",
                        return_value=temp_root / "cache" / "pvta_routedetails.xml",
                    ):
                        with patch("scripts.convert.urlopen", return_value=BytesIO(xml_payload)):
                            with patch(
                                "sys.argv",
                                [
                                    "convert.py",
                                    str(csv_dir),
                                    "--agency",
                                    "pvta",
                                    "--output",
                                    str(output_path),
                                ],
                            ):
                                exit_code = convert.main()

            connection = sqlite3.connect(output_path)
            try:
                rows = connection.execute(
                    "SELECT route_id, route_rt_id FROM routes ORDER BY route_id"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows, [("B43", "10043"), ("R2", "R2")])

    def test_main_forces_download_when_release_check_fails_and_folder_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            input_dir = temp_root / "uta"
            output_path = temp_root / "uta.sqlite"

            def fake_download(url: str, target_dir: Path) -> None:
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "stops.txt").write_text(
                    "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n2,Stop B,1.1,2.0\n",
                    encoding="utf-8",
                )
                (target_dir / "trips.txt").write_text(
                    "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                    encoding="utf-8",
                )
                (target_dir / "stop_times.txt").write_text(
                    "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\nT1,08:05:00,2,2\n",
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
                    "stop_id,stop_name,stop_lat,stop_lon\n1,Stop A,1.0,2.0\n2,Stop B,1.1,2.0\n",
                    encoding="utf-8",
                )
                (target_dir / "trips.txt").write_text(
                    "route_id,service_id,trip_id,trip_headsign,shape_id\nR1,S1,T1,Headsign,SH1\n",
                    encoding="utf-8",
                )
                (target_dir / "stop_times.txt").write_text(
                    "trip_id,arrival_time,stop_id,stop_sequence\nT1,08:00:00,1,1\nT1,08:05:00,2,2\n",
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

    def test_haversine_distance_meters_known_distance(self) -> None:
        # One degree of longitude at the equator is roughly 111.2 km.
        distance = convert.haversine_distance_meters(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(distance, 111195.0, delta=100.0)

        self.assertEqual(convert.haversine_distance_meters(45.0, 9.0, 45.0, 9.0), 0.0)

    def test_build_stop_graph_counts_edge_frequencies_and_skips_self_loops(self) -> None:
        adjacency, reverse_adjacency = convert.build_stop_graph(
            [
                ["A", "B", "C"],
                ["A", "B"],
                ["A", "A", "B"],
            ]
        )

        self.assertEqual(adjacency["A"], {"B": 3})
        self.assertEqual(adjacency["B"], {"C": 1})
        self.assertEqual(adjacency["C"], {})
        self.assertEqual(reverse_adjacency["B"], {"A": 3})
        self.assertEqual(reverse_adjacency["C"], {"B": 1})
        self.assertEqual(reverse_adjacency["A"], {})

    def test_remove_cycle_edges_drops_lowest_frequency_edge(self) -> None:
        adjacency, reverse_adjacency = convert.build_stop_graph(
            [
                ["A", "B", "C"],
                ["A", "B", "C"],
                ["A", "B", "C"],
                ["C", "A"],
            ]
        )

        removed_edges = convert.remove_cycle_edges(adjacency, reverse_adjacency)

        self.assertEqual(removed_edges, [("C", "A", 1)])
        self.assertNotIn("A", adjacency["C"])
        self.assertNotIn("C", reverse_adjacency["A"])

    def test_remove_cycle_edges_keeps_acyclic_graph_untouched(self) -> None:
        adjacency, reverse_adjacency = convert.build_stop_graph(
            [["A", "B", "C", "D"]]
        )

        removed_edges = convert.remove_cycle_edges(adjacency, reverse_adjacency)

        self.assertEqual(removed_edges, [])
        self.assertEqual(adjacency["A"], {"B": 1})

    def test_topological_superset_order_merges_short_turn_into_full_pattern(self) -> None:
        adjacency, reverse_adjacency = convert.build_stop_graph(
            [
                ["A", "B", "C", "D", "E"],
                ["B", "C", "D"],
            ]
        )

        superset = convert.topological_superset_order(adjacency, reverse_adjacency)

        self.assertEqual(superset, ["A", "B", "C", "D", "E"])

    def test_topological_superset_order_tie_breaks_deterministically(self) -> None:
        # Branch fixture: after A, both C (incoming weight 2) and D (weight 1)
        # are ready — incoming frequency must pick C first.
        frequency_trips = [
            ["A", "C", "E"],
            ["A", "C", "E"],
            ["A", "D", "E"],
        ]
        adjacency, reverse_adjacency = convert.build_stop_graph(frequency_trips)
        superset = convert.topological_superset_order(adjacency, reverse_adjacency)
        self.assertEqual(superset, ["A", "C", "D", "E"])

        # Equal incoming weights: the longer downstream path (C -> D -> Z)
        # wins over the short branch (B -> Z); the remaining tie between B and
        # D resolves lexicographically.
        path_trips = [
            ["A", "B", "Z"],
            ["A", "C", "D", "Z"],
        ]
        adjacency, reverse_adjacency = convert.build_stop_graph(path_trips)
        superset = convert.topological_superset_order(adjacency, reverse_adjacency)
        self.assertEqual(superset, ["A", "C", "B", "D", "Z"])

        # Reproducibility: permuting the input trip order must not change the
        # resulting superset.
        adjacency, reverse_adjacency = convert.build_stop_graph(path_trips[::-1])
        self.assertEqual(
            convert.topological_superset_order(adjacency, reverse_adjacency), superset
        )

    def test_compute_progress_ratios_endpoints_and_monotonicity(self) -> None:
        stop_coordinates = {
            "A": (0.0, 0.0),
            "B": (0.0, 1.0),
            "C": (0.0, 2.0),
        }

        total_distance, ratios = convert.compute_progress_ratios(
            ["A", "B", "C"], stop_coordinates
        )

        self.assertGreater(total_distance, 0.0)
        self.assertEqual(ratios[0], 0.0)
        self.assertEqual(ratios[-1], 1.0)
        self.assertAlmostEqual(ratios[1], 0.5, places=3)
        self.assertEqual(ratios, sorted(ratios))

        # A stop without coordinates contributes a zero-length segment.
        total_distance, ratios = convert.compute_progress_ratios(
            ["A", "missing", "C"], stop_coordinates
        )
        self.assertGreater(total_distance, 0.0)
        self.assertEqual(ratios, [0.0, 0.0, 1.0])

        # Without any usable coordinates the ratios fall back to uniform spacing.
        total_distance, ratios = convert.compute_progress_ratios(
            ["X", "Y", "Z"], stop_coordinates
        )
        self.assertEqual(total_distance, 0.0)
        self.assertEqual(ratios, [0.0, 0.5, 1.0])

    def test_select_direction_label_prefers_most_frequent_then_longest_trip(self) -> None:
        self.assertEqual(
            convert.select_direction_label(
                [
                    ("T1", "Downtown", ["A", "B"]),
                    ("T2", "Downtown", ["A", "B"]),
                    ("T3", "Airport", ["A", "B", "C"]),
                ]
            ),
            "Downtown",
        )

        # Frequency tie: the headsign of the longest trip (most stops) wins.
        self.assertEqual(
            convert.select_direction_label(
                [
                    ("T1", "Downtown", ["A", "B", "C"]),
                    ("T2", "Airport", ["A", "B", "C", "D", "E"]),
                ]
            ),
            "Airport",
        )

        self.assertEqual(
            convert.select_direction_label([("T1", "", ["A", "B"])]), ""
        )

    def _create_canonical_fixture_tables(
        self, connection: sqlite3.Connection, include_direction_id: bool = True
    ) -> None:
        direction_column = ", direction_id INTEGER" if include_direction_id else ""
        connection.execute(
            "CREATE TABLE trips (route_id TEXT, service_id TEXT, trip_id TEXT, "
            f"trip_headsign TEXT{direction_column})"
        )
        connection.execute(
            "CREATE TABLE stop_times (trip_id TEXT, arrival_time TEXT, "
            "stop_id TEXT, stop_sequence INTEGER)"
        )
        connection.execute(
            "CREATE TABLE stops (stop_id TEXT, stop_name TEXT, "
            "stop_lat REAL, stop_lon REAL)"
        )
        connection.executemany(
            "INSERT INTO stops VALUES (?, ?, ?, ?)",
            [
                ("A", "Stop A", 0.0, 0.0),
                ("B", "Stop B", 0.0, 1.0),
                ("C", "Stop C", 0.0, 2.0),
            ],
        )

    def test_build_canonical_routes_populates_tables_end_to_end(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            self._create_canonical_fixture_tables(connection)
            connection.executemany(
                "INSERT INTO trips VALUES (?, ?, ?, ?, ?)",
                [
                    ("R1", "S1", "T1", "Outbound", 0),
                    ("R1", "S1", "T2", "Outbound", 0),  # short-turn variant
                    ("R1", "S1", "T3", "Inbound", 1),
                ],
            )
            connection.executemany(
                "INSERT INTO stop_times VALUES (?, ?, ?, ?)",
                [
                    ("T1", "08:00:00", "A", 1),
                    ("T1", "08:05:00", "B", 2),
                    ("T1", "08:10:00", "C", 3),
                    ("T2", "09:00:00", "B", 1),
                    ("T2", "09:05:00", "C", 2),
                    ("T3", "10:00:00", "C", 1),
                    ("T3", "10:05:00", "B", 2),
                    ("T3", "10:10:00", "A", 3),
                ],
            )

            convert.build_canonical_routes(connection)

            route_rows = connection.execute(
                "SELECT route_id, direction_id, direction_label, start_stop_id, "
                "end_stop_id, total_distance FROM canonical_routes "
                "ORDER BY direction_id"
            ).fetchall()
            self.assertEqual(len(route_rows), 2)
            self.assertEqual(route_rows[0][:5], ("R1", 0, "Outbound", "A", "C"))
            self.assertEqual(route_rows[1][:5], ("R1", 1, "Inbound", "C", "A"))
            self.assertGreater(route_rows[0][5], 0.0)

            stop_rows = connection.execute(
                "SELECT stop_id, superset_sequence, progress_ratio "
                "FROM canonical_route_stops WHERE direction_id = 0 "
                "ORDER BY superset_sequence"
            ).fetchall()
            self.assertEqual([row[0] for row in stop_rows], ["A", "B", "C"])
            self.assertEqual([row[1] for row in stop_rows], [0, 1, 2])
            self.assertEqual(stop_rows[0][2], 0.0)
            self.assertEqual(stop_rows[-1][2], 1.0)

            index_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            self.assertIn(
                "idx_canonical_route_stops_route_id_direction_id_superset_sequence",
                index_names,
            )
            self.assertIn("idx_canonical_route_stops_stop_id", index_names)
        finally:
            connection.close()

    def test_build_canonical_routes_defaults_missing_direction_id_to_zero(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            self._create_canonical_fixture_tables(connection, include_direction_id=False)
            connection.execute(
                "INSERT INTO trips VALUES ('R1', 'S1', 'T1', 'Somewhere')"
            )
            connection.executemany(
                "INSERT INTO stop_times VALUES (?, ?, ?, ?)",
                [
                    ("T1", "08:00:00", "A", 1),
                    ("T1", "08:05:00", "B", 2),
                ],
            )

            convert.build_canonical_routes(connection)

            route_rows = connection.execute(
                "SELECT route_id, direction_id FROM canonical_routes"
            ).fetchall()
            self.assertEqual(route_rows, [("R1", 0)])
        finally:
            connection.close()

    def test_match_stop_counterparts_returns_same_stop_and_nearest_pair(self) -> None:
        # B serves both directions; A1/A2 are a street pair ~44m apart.
        matches = convert.match_stop_counterparts(
            {"A1": 0.0, "B": 0.5},
            {"A2": 1.0, "B": 0.5},
            {"A1": (0.0, 0.0), "A2": (0.0004, 0.0), "B": (0.0, 0.001)},
            {},
        )

        self.assertEqual(len(matches), 2)
        by_stop = {match[0]: match for match in matches}
        self.assertEqual(by_stop["B"], ("B", "B", 0.0, "same_stop"))
        stop_id, counterpart_id, distance, match_type = by_stop["A1"]
        self.assertEqual(counterpart_id, "A2")
        self.assertEqual(match_type, "paired")
        self.assertAlmostEqual(distance, 44.5, delta=2.0)

    def test_match_stop_counterparts_mirror_filter_rejects_wrong_passage(self) -> None:
        # N is nearest but sits at the same linear position (another passage
        # of the route through the area); F mirrors the progress correctly.
        matches = convert.match_stop_counterparts(
            {"S": 0.1},
            {"N": 0.1, "F": 0.88},
            {"S": (0.0, 0.0), "N": (0.00027, 0.0), "F": (0.0007, 0.0)},
            {},
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], "F")
        self.assertEqual(matches[0][3], "paired")

    def test_match_stop_counterparts_prefers_shared_parent_station(self) -> None:
        # H is nearer and mirror-perfect, but G shares the GTFS parent_station.
        matches = convert.match_stop_counterparts(
            {"S": 0.0},
            {"G": 0.2, "H": 1.0},
            {"S": (0.0, 0.0), "G": (0.0006, 0.0), "H": (0.0002, 0.0)},
            {"S": "P1", "G": "P1"},
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], "G")
        self.assertEqual(matches[0][3], "parent_station")

    def test_match_stop_counterparts_returns_no_match_outside_radius(self) -> None:
        matches = convert.match_stop_counterparts(
            {"S": 0.0},
            {"T": 1.0},
            {"S": (0.0, 0.0), "T": (0.002, 0.0)},
            {},
        )

        self.assertEqual(matches, [])

    def test_build_canonical_stop_counterparts_populates_table(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE trips (route_id TEXT, service_id TEXT, trip_id TEXT, "
                "trip_headsign TEXT, direction_id INTEGER)"
            )
            connection.execute(
                "CREATE TABLE stop_times (trip_id TEXT, arrival_time TEXT, "
                "stop_id TEXT, stop_sequence INTEGER)"
            )
            connection.execute(
                "CREATE TABLE stops (stop_id TEXT, stop_name TEXT, "
                "stop_lat REAL, stop_lon REAL)"
            )
            connection.executemany(
                "INSERT INTO stops VALUES (?, ?, ?, ?)",
                [
                    ("A1", "Stop A east", 0.0, 0.0),
                    ("B1", "Stop B east", 0.0, 0.001),
                    ("C1", "Stop C east", 0.0, 0.002),
                    ("A2", "Stop A west", 0.0004, 0.0),
                    ("B2", "Stop B west", 0.0004, 0.001),
                    ("C2", "Stop C west", 0.0004, 0.002),
                ],
            )
            connection.executemany(
                "INSERT INTO trips VALUES (?, ?, ?, ?, ?)",
                [
                    ("R1", "S1", "T1", "Eastbound", 0),
                    ("R1", "S1", "T2", "Westbound", 1),
                ],
            )
            connection.executemany(
                "INSERT INTO stop_times VALUES (?, ?, ?, ?)",
                [
                    ("T1", "08:00:00", "A1", 1),
                    ("T1", "08:05:00", "B1", 2),
                    ("T1", "08:10:00", "C1", 3),
                    ("T2", "09:00:00", "C2", 1),
                    ("T2", "09:05:00", "B2", 2),
                    ("T2", "09:10:00", "A2", 3),
                ],
            )

            convert.build_canonical_routes(connection)
            convert.build_canonical_stop_counterparts(connection)

            rows = connection.execute(
                "SELECT route_id, direction_id, stop_id, counterpart_stop_id, "
                "counterpart_direction_id, match_type "
                "FROM canonical_stop_counterparts ORDER BY direction_id, stop_id"
            ).fetchall()
            self.assertEqual(len(rows), 6)
            self.assertIn(("R1", 0, "A1", "A2", 1, "paired"), rows)
            self.assertIn(("R1", 0, "B1", "B2", 1, "paired"), rows)
            self.assertIn(("R1", 1, "C2", "C1", 0, "paired"), rows)

            distances = [
                row[0]
                for row in connection.execute(
                    "SELECT counterpart_distance FROM canonical_stop_counterparts"
                ).fetchall()
            ]
            for distance in distances:
                self.assertAlmostEqual(distance, 44.5, delta=2.0)

            index_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            self.assertIn("idx_canonical_stop_counterparts_stop_id", index_names)
        finally:
            connection.close()

    def test_build_canonical_stop_counterparts_skips_without_canonical_tables(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertLogs("gtfs_converter", level="WARNING"):
                convert.build_canonical_stop_counterparts(connection)

            self.assertFalse(
                convert.table_exists(connection, "canonical_stop_counterparts")
            )
        finally:
            connection.close()

    def test_build_canonical_routes_skips_when_stop_times_missing(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE trips (route_id TEXT, trip_id TEXT)")
            connection.execute("CREATE TABLE stops (stop_id TEXT)")

            with self.assertLogs("gtfs_converter", level="WARNING"):
                convert.build_canonical_routes(connection)

            self.assertFalse(convert.table_exists(connection, "canonical_routes"))
            self.assertFalse(
                convert.table_exists(connection, "canonical_route_stops")
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
