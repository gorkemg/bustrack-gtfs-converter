"""Validate the generated release asset before any release is modified."""
import argparse
from datetime import datetime
import uuid
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path

MAX_DATABASE_SIZE = 2 * 1024 * 1024 * 1024


def copy_bounded(source, destination, expected_size: int) -> None:
    copied = 0
    while True:
        chunk = source.read(min(65536, MAX_DATABASE_SIZE - copied + 1))
        if not chunk:
            break
        copied += len(chunk)
        if copied > MAX_DATABASE_SIZE or copied > expected_size:
            raise ValueError("Decompressed database exceeds its size limit")
        destination.write(chunk)
    if copied != expected_size:
        raise ValueError("Decompressed database size does not match archive metadata")


def validate_release(archive_path: Path, agency: str) -> None:
    if archive_path.name != f"{agency}.sqlite.zip" or archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("Unexpected archive name or file type")
    if not 0 < archive_path.stat().st_size <= MAX_DATABASE_SIZE:
        raise ValueError("Invalid archive size")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != f"{agency}.sqlite":
            raise ValueError("Archive must contain exactly the expected SQLite file")
        entry = entries[0]
        mode = entry.external_attr >> 16
        if entry.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
            raise ValueError("Archive entry must be a regular file")
        if not 0 < entry.file_size <= MAX_DATABASE_SIZE or not 0 < entry.compress_size <= MAX_DATABASE_SIZE:
            raise ValueError("Invalid archive entry size")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "release.sqlite"
            with archive.open(entry) as source, database.open("wb") as dest:
                copy_bounded(source, dest, entry.file_size)
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite integrity check failed")
                metadata = dict(connection.execute("SELECT key, value FROM app_metadata"))
                if metadata.get("agency_id") != agency or metadata.get("schema_version") != "1.2":
                    raise ValueError("SQLite agency or schema version does not match")
                required_metadata = {"build_timestamp", "build_id", "agency_id", "git_commit_sha", "workflow_run_id", "feed_start_date", "feed_end_date", "gtfs_source_filename", "schema_version"}
                if not required_metadata.issubset(metadata) or not metadata["gtfs_source_filename"]:
                    raise ValueError("Missing required release metadata")
                try:
                    timestamp = datetime.fromisoformat(metadata["build_timestamp"])
                    uuid.UUID(metadata["build_id"])
                    if timestamp.tzinfo is None:
                        raise ValueError("Build timestamp must include a timezone")
                    for key in ("feed_start_date", "feed_end_date"):
                        if metadata[key]:
                            datetime.strptime(metadata[key], "%Y%m%d")
                    if metadata["feed_start_date"] and metadata["feed_end_date"] and metadata["feed_start_date"] > metadata["feed_end_date"]:
                        raise ValueError("Invalid feed date range")
                except (ValueError, TypeError) as error:
                    raise ValueError("Invalid release metadata") from error
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"stops", "trips", "stop_times"}.issubset(tables):
                    raise ValueError("SQLite is missing required GTFS tables")
                for table in ("stops", "trips", "stop_times"):
                    if connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is None:
                        raise ValueError(f"SQLite table {table} is empty")

                validate_canonical(connection)


def validate_canonical(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"canonical_routes", "canonical_route_stops", "canonical_stop_counterparts"}
    if not required.issubset(tables):
        raise ValueError("Missing canonical tables")
    routes = connection.execute("SELECT route_id, direction_id, start_stop_id, end_stop_id FROM canonical_routes").fetchall()
    if not routes:
        raise ValueError("No canonical routes")
    groups = {}
    for route, direction, start, end in routes:
        rows = connection.execute("SELECT stop_id, superset_sequence, progress_ratio FROM canonical_route_stops WHERE route_id=? AND direction_id=? ORDER BY superset_sequence", (route, direction)).fetchall()
        if len(rows) < 2 or rows[0][0] != start or rows[-1][0] != end or [r[1] for r in rows] != list(range(len(rows))):
            raise ValueError("Invalid canonical stop ordering")
        ratios = [r[2] for r in rows]
        if any(not isinstance(r, (int, float)) or not 0 <= r <= 1 for r in ratios) or ratios[0] != 0 or ratios[-1] != 1 or ratios != sorted(ratios):
            raise ValueError("Invalid canonical progress ratios")
        groups[(route, direction)] = {r[0] for r in rows}
    if connection.execute("SELECT COUNT(*) FROM canonical_route_stops").fetchone()[0] != sum(map(len, groups.values())):
        raise ValueError("Orphan or duplicate canonical stops")
    for route, direction, stop, counterpart, opposite, distance, match in connection.execute("SELECT route_id, direction_id, stop_id, counterpart_stop_id, counterpart_direction_id, counterpart_distance, match_type FROM canonical_stop_counterparts"):
        if direction not in (0, 1) or opposite != 1 - direction or stop not in groups.get((route, direction), set()) or counterpart not in groups.get((route, opposite), set()) or match not in ("same_stop", "parent_station", "paired") or (distance is not None and (not isinstance(distance, (int, float)) or distance < 0)) or (match == "same_stop" and (stop != counterpart or distance != 0)):
            raise ValueError("Invalid canonical counterpart")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agency")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    validate_release(args.archive, args.agency)


if __name__ == "__main__":
    main()
