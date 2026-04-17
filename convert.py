from __future__ import annotations

import argparse
import logging
from pathlib import Path


LOGGER = logging.getLogger("gtfs_converter")


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

    LOGGER.info("CLI scaffold initialized successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())