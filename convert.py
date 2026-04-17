from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from pathlib import Path


LOGGER = logging.getLogger("gtfs_converter")

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

    connection.close()
    LOGGER.info("SQLite schema initialized successfully: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())