from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path


LOGGER = logging.getLogger("gtfs_converter")


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

    connection.close()
    LOGGER.info("SQLite connection initialized successfully: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())