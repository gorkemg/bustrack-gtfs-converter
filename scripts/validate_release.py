"""Validate the generated release asset before any release is modified."""
import argparse
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
                if metadata.get("agency_id") != agency or metadata.get("schema_version") != "1.0":
                    raise ValueError("SQLite agency or schema version does not match")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"stops", "trips", "stop_times"}.issubset(tables):
                    raise ValueError("SQLite is missing required GTFS tables")
                for table in ("stops", "trips", "stop_times"):
                    if connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is None:
                        raise ValueError(f"SQLite table {table} is empty")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agency")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    validate_release(args.archive, args.agency)


if __name__ == "__main__":
    main()
