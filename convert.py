from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path


LOGGER = logging.getLogger("gtfs_converter")
IMPORT_CHUNK_SIZE = 5000

INTEGER_COLUMN_NAMES = {
    "bikes_allowed",
    "continuous_drop_off",
    "continuous_pickup",
    "direction_id",
    "drop_off_type",
    "duration_limit",
    "duration_limit_type",
    "exact_times",
    "exception_type",
    "fare_media_type",
    "fare_transfer_type",
    "location_type",
    "min_transfer_time",
    "payment_method",
    "pickup_type",
    "route_sort_order",
    "route_type",
    "shape_dist_traveled",
    "stop_sequence",
    "timepoint",
    "transfer_count",
    "transfer_type",
    "wheelchair_accessible",
    "wheelchair_boarding",
}

REAL_COLUMN_NAMES = {
    "amount",
    "shape_dist_traveled",
    "shape_pt_lat",
    "shape_pt_lon",
    "stop_lat",
    "stop_lon",
}


def discover_gtfs_files(input_path: Path) -> list[Path]:
    gtfs_files = sorted(path for path in input_path.iterdir() if path.suffix == ".txt")

    if not gtfs_files:
        raise FileNotFoundError(f"No GTFS .txt files found in {input_path}")

    return gtfs_files


def build_output_path(input_path: Path, output_name: str) -> Path:
    output_path = Path(output_name)
    if output_path.is_absolute():
        return output_path

    return input_path.parent / output_path


def connect_sqlite(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    return connection


def quote_identifier(identifier: str) -> str:
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


def infer_sqlite_type(column_name: str) -> str:
    normalized_name = column_name.strip().lower()

    if normalized_name in REAL_COLUMN_NAMES:
        return "REAL"

    if normalized_name.endswith(("_lat", "_lon")):
        return "REAL"

    if normalized_name.endswith(("_sequence", "_sort_order", "_count", "_type")):
        return "INTEGER"

    if normalized_name in INTEGER_COLUMN_NAMES:
        return "INTEGER"

    return "TEXT"


def read_gtfs_header(file_path: Path) -> list[str]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as error:
            raise ValueError(f"GTFS file is empty: {file_path}") from error

    if not header:
        raise ValueError(f"GTFS header is empty: {file_path}")

    return header


def build_create_table_sql(table_name: str, columns: list[str]) -> str:
    column_definitions = [
        f"{quote_identifier(column_name)} {infer_sqlite_type(column_name)}"
        for column_name in columns
    ]
    joined_columns = ", ".join(column_definitions)
    return (
        f"CREATE TABLE {quote_identifier(table_name)} ({joined_columns})"
    )


def build_insert_sql(table_name: str, columns: list[str]) -> str:
    quoted_columns = ", ".join(quote_identifier(column_name) for column_name in columns)
    placeholders = ", ".join("?" for _ in columns)
    return (
        f"INSERT INTO {quote_identifier(table_name)} ({quoted_columns}) "
        f"VALUES ({placeholders})"
    )


def convert_value(raw_value: str, column_name: str) -> int | float | str | None:
    value = raw_value.strip()
    if value == "":
        return None

    sqlite_type = infer_sqlite_type(column_name)
    if sqlite_type == "INTEGER":
        return int(value)

    if sqlite_type == "REAL":
        return float(value)

    return value


def iter_gtfs_chunks(
    file_path: Path, columns: list[str], chunk_size: int = IMPORT_CHUNK_SIZE
) -> Iterator[list[tuple[int | float | str | None, ...]]]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        chunk: list[tuple[int | float | str | None, ...]] = []

        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(columns):
                raise ValueError(
                    f"Row {row_number} in {file_path} has {len(row)} values; expected {len(columns)}"
                )

            converted_row = tuple(
                convert_value(raw_value, column_name)
                for column_name, raw_value in zip(columns, row)
            )
            chunk.append(converted_row)

            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []

        if chunk:
            yield chunk


def create_gtfs_tables(connection: sqlite3.Connection, gtfs_files: list[Path]) -> None:
    with connection:
        for gtfs_file in gtfs_files:
            table_name = gtfs_file.stem
            columns = read_gtfs_header(gtfs_file)
            connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(table_name)}")
            connection.execute(build_create_table_sql(table_name, columns))
            LOGGER.info(
                "Created table %s with %d columns", table_name, len(columns)
            )


def import_gtfs_data(connection: sqlite3.Connection, gtfs_files: list[Path]) -> None:
    for gtfs_file in gtfs_files:
        table_name = gtfs_file.stem
        columns = read_gtfs_header(gtfs_file)
        insert_sql = build_insert_sql(table_name, columns)
        imported_rows = 0

        with connection:
            for chunk in iter_gtfs_chunks(gtfs_file, columns):
                connection.executemany(insert_sql, chunk)
                imported_rows += len(chunk)

        LOGGER.info("Imported %d rows into %s", imported_rows, table_name)


def create_app_metadata(
    connection: sqlite3.Connection, input_path: Path, schema_version: str = "1.0"
) -> None:
    metadata_rows = [
        (
            "build_timestamp",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        ("gtfs_source_filename", input_path.name),
        ("schema_version", schema_version),
    ]

    with connection:
        connection.execute("DROP TABLE IF EXISTS app_metadata")
        connection.execute(
            "CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.executemany(
            "INSERT INTO app_metadata (key, value) VALUES (?, ?)", metadata_rows
        )

    LOGGER.info("Created app_metadata with %d entries", len(metadata_rows))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a GTFS directory into an optimized SQLite database."
    )
    parser.add_argument(
        "input_path",
        type=Path,
        help="Path to the GTFS directory containing .txt files.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="gtfs.sqlite",
        help="Output SQLite database filename.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)

    LOGGER.info("Starting GTFS conversion pipeline")
    LOGGER.info("Input path: %s", args.input_path)
    LOGGER.info("Output database: %s", args.output)

    if not args.input_path.exists():
        LOGGER.error("Input path does not exist: %s", args.input_path)
        return 1

    if not args.input_path.is_dir():
        LOGGER.error("Input path is not a directory: %s", args.input_path)
        return 1

    try:
        gtfs_files = discover_gtfs_files(args.input_path)
    except FileNotFoundError as error:
        LOGGER.error("%s", error)
        return 1

    output_path = build_output_path(args.input_path, args.output)
    LOGGER.info("Discovered %d GTFS files", len(gtfs_files))
    LOGGER.debug("GTFS files: %s", [path.name for path in gtfs_files])

    try:
        connection = connect_sqlite(output_path)
    except sqlite3.Error as error:
        LOGGER.error("Failed to connect to SQLite database %s: %s", output_path, error)
        return 1

    try:
        create_gtfs_tables(connection, gtfs_files)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOGGER.error("Failed to create GTFS tables: %s", error)
        connection.close()
        return 1

    try:
        import_gtfs_data(connection, gtfs_files)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOGGER.error("Failed to import GTFS data: %s", error)
        connection.close()
        return 1

    try:
        create_app_metadata(connection, args.input_path)
    except sqlite3.Error as error:
        LOGGER.error("Failed to create app_metadata: %s", error)
        connection.close()
        return 1

    connection.close()
    LOGGER.info(
        "SQLite schema, data, and metadata initialized successfully: %s", output_path
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())