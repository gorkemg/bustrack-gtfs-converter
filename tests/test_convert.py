from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import convert


class ConvertTests(unittest.TestCase):
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

    def test_needs_update_uses_github_release_timestamp(self) -> None:
        upstream_time = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)
        release_time = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        with patch("scripts.convert.fetch_last_modified", return_value=upstream_time):
            with patch(
                "scripts.convert.fetch_latest_release_timestamp_from_github",
                return_value=release_time,
            ):
                should_update, actual_upstream_time = convert.needs_update(
                    "pvta", "https://example.test/pvta.zip"
                )

        self.assertTrue(should_update)
        self.assertEqual(actual_upstream_time, upstream_time)

    def test_update_release_cache_writes_expected_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "release_cache.json"
            upstream_time = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)

            convert.update_release_cache("pvta", upstream_time, cache_path=cache_path)

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("pvta", payload)
            self.assertEqual(
                payload["pvta"]["source_last_modified"],
                upstream_time.isoformat(timespec="seconds"),
            )

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


if __name__ == "__main__":
    unittest.main()
