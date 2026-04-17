from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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

INDEXED_COLUMN_NAMES = {
    "route_id",
    "route_long_name",
    "route_short_name",
    "stop_id",
    "stop_name",
    "trip_headsign",
    "trip_id",
}


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_agencies_config_path() -> Path:
    return get_repo_root() / "config" / "agencies.json"


def get_release_cache_path() -> Path:
    return get_repo_root() / "data" / "release_cache.json"


def load_agencies_config(config_path: Path) -> list[dict[str, str]]:
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    if not isinstance(config, list):
        raise ValueError(f"Agency config must be a JSON list: {config_path}")

    return config


def load_release_cache(cache_path: Path) -> dict[str, dict[str, str]]:
    if not cache_path.exists():
        return {}

    with cache_path.open("r", encoding="utf-8") as handle:
        cache = json.load(handle)

    if not isinstance(cache, dict):
        raise ValueError(f"Release cache must be a JSON object: {cache_path}")

    return cache


def save_release_cache(cache_path: Path, cache: dict[str, dict[str, str]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2, sort_keys=True)
        handle.write("\n")


def parse_http_datetime(value: str) -> datetime:
    parsed_value = parsedate_to_datetime(value)
    if parsed_value.tzinfo is None:
        return parsed_value.replace(tzinfo=timezone.utc)
    return parsed_value.astimezone(timezone.utc)


def parse_iso8601_datetime(value: str) -> datetime:
    parsed_value = datetime.fromisoformat(value)
    if parsed_value.tzinfo is None:
        return parsed_value.replace(tzinfo=timezone.utc)
    return parsed_value.astimezone(timezone.utc)


def fetch_last_modified(gtfs_url: str) -> datetime:
    request = Request(gtfs_url, method="HEAD")
    with urlopen(request, timeout=30) as response:
        last_modified = response.headers.get("Last-Modified")

    if not last_modified:
        raise ValueError(f"No Last-Modified header returned for {gtfs_url}")

    return parse_http_datetime(last_modified)


def needs_update(
    agency_id: str, gtfs_url: str, cache_path: Path | None = None
) -> tuple[bool, datetime]:
    effective_cache_path = cache_path or get_release_cache_path()
    upstream_last_modified = fetch_last_modified(gtfs_url)
    release_cache = load_release_cache(effective_cache_path)
    cached_release = release_cache.get(agency_id, {})
    cached_release_date = cached_release.get("released_at")

    if not cached_release_date:
        return True, upstream_last_modified

    last_release_date = parse_iso8601_datetime(cached_release_date)
    return upstream_last_modified > last_release_date, upstream_last_modified


def update_release_cache(
    agency_id: str,
    upstream_last_modified: datetime,
    cache_path: Path | None = None,
) -> None:
    effective_cache_path = cache_path or get_release_cache_path()
    release_cache = load_release_cache(effective_cache_path)
    release_cache[agency_id] = {
        "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_last_modified": upstream_last_modified.isoformat(timespec="seconds"),
    }
    save_release_cache(effective_cache_path, release_cache)


def resolve_agency_config(
    agencies_config: list[dict[str, str]], agency_id: str
) -> dict[str, str]:
    for agency_config in agencies_config:
        if agency_config.get("id") == agency_id:
            return agency_config

    raise ValueError(f"Agency '{agency_id}' not found in agencies config")


def resolve_input_path(input_path: Path | None, agency_id: str | None) -> Path:
    if input_path is not None:
        return input_path

    if agency_id is None:
        raise ValueError("Either input_path or --agency must be provided")

    repo_root = get_repo_root()
    candidate_paths = [repo_root / "data" / agency_id, repo_root / agency_id]
    for candidate_path in candidate_paths:
        if candidate_path.exists() and candidate_path.is_dir():
            return candidate_path

    raise FileNotFoundError(
        f"No GTFS directory found for agency '{agency_id}' in data/ or repository root"
    )


def resolve_output_path(
    input_path: Path, agency_id: str | None, output_name: str | None
) -> Path:
    repo_root = get_repo_root()

    if output_name:
        output_path = Path(output_name)
        if output_path.is_absolute():
            return output_path
        return repo_root / output_path

    output_stem = agency_id or input_path.name
    return repo_root / "data" / f"{output_stem}.sqlite"


def discover_gtfs_files(input_path: Path) -> list[Path]:
    gtfs_files = sorted(path for path in input_path.iterdir() if path.suffix == ".txt")

    if not gtfs_files:
        raise FileNotFoundError(f"No GTFS .txt files found in {input_path}")

    return gtfs_files


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
    return f"CREATE TABLE {quote_identifier(table_name)} ({joined_columns})"


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
            LOGGER.info("Created table %s with %d columns", table_name, len(columns))


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


def build_index_name(table_name: str, column_name: str) -> str:
    return f"idx_{table_name}_{column_name}"


def create_recommended_indexes(
    connection: sqlite3.Connection, gtfs_files: list[Path]
) -> None:
    created_indexes = 0

    with connection:
        for gtfs_file in gtfs_files:
            table_name = gtfs_file.stem
            columns = read_gtfs_header(gtfs_file)

            for column_name in columns:
                if column_name not in INDEXED_COLUMN_NAMES:
                    continue

                index_name = build_index_name(table_name, column_name)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    f"{quote_identifier(index_name)} ON "
                    f"{quote_identifier(table_name)} ({quote_identifier(column_name)})"
                )
                created_indexes += 1

    LOGGER.info("Created %d recommended indexes", created_indexes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a GTFS directory into an optimized SQLite database."
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        type=Path,
        help="Optional path to the GTFS directory containing .txt files.",
    )
    parser.add_argument(
        "--agency",
        help="Agency ID from config/agencies.json, for example pvta or uta.",
    )
    parser.add_argument(
        "--url",
        help="Override GTFS download URL for the selected agency.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
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

    try:
        agencies_config = load_agencies_config(get_agencies_config_path())
    except (OSError, json.JSONDecodeError, ValueError) as error:
        LOGGER.error("Failed to load agencies config: %s", error)
        return 1

    agency_config: dict[str, str] | None = None
    if args.agency:
        try:
            agency_config = resolve_agency_config(agencies_config, args.agency)
        except ValueError as error:
            LOGGER.error("%s", error)
            return 1

    try:
        input_path = resolve_input_path(args.input_path, args.agency)
    except (ValueError, FileNotFoundError) as error:
        LOGGER.error("%s", error)
        return 1

    gtfs_url = args.url or (agency_config or {}).get("url")
    output_path = resolve_output_path(input_path, args.agency, args.output)

    LOGGER.info("Starting GTFS conversion pipeline")
    LOGGER.info("Agency: %s", args.agency or input_path.name)
    LOGGER.info("Input path: %s", input_path)
    LOGGER.info("Output database: %s", output_path)
    if gtfs_url:
        LOGGER.info("GTFS URL: %s", gtfs_url)

    upstream_last_modified: datetime | None = None
    if args.agency and gtfs_url:
        try:
            should_update, upstream_last_modified = needs_update(args.agency, gtfs_url)
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            HTTPError,
            URLError,
        ) as error:
            LOGGER.warning("Release check failed, continuing with conversion: %s", error)
        else:
            if not should_update:
                LOGGER.info("No update needed")
                return 0

    if not input_path.exists():
        LOGGER.error("Input path does not exist: %s", input_path)
        return 1

    if not input_path.is_dir():
        LOGGER.error("Input path is not a directory: %s", input_path)
        return 1

    try:
        gtfs_files = discover_gtfs_files(input_path)
    except FileNotFoundError as error:
        LOGGER.error("%s", error)
        return 1

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
        create_app_metadata(connection, input_path)
    except sqlite3.Error as error:
        LOGGER.error("Failed to create app_metadata: %s", error)
        connection.close()
        return 1

    try:
        create_recommended_indexes(connection, gtfs_files)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOGGER.error("Failed to create recommended indexes: %s", error)
        connection.close()
        return 1

    connection.close()

    if args.agency and upstream_last_modified is not None:
        try:
            update_release_cache(args.agency, upstream_last_modified)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            LOGGER.error("Failed to update release cache: %s", error)
            return 1

    LOGGER.info(
        "SQLite schema, data, metadata, and indexes initialized successfully: %s",
        output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())