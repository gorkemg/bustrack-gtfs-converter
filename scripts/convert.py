from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import sqlite3
import zipfile
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

INDEX_SPECS = {
    "stop_times": [
        ("trip_id",),
        ("stop_id", "arrival_time"),
        ("stop_sequence",),
    ],
    "trips": [
        ("service_id", "route_id"),
        ("trip_headsign",),
        ("shape_id",),
    ],
    "calendar": [
        ("service_id", "start_date", "end_date"),
    ],
    "calendar_dates": [
        ("service_id", "date"),
    ],
    "shapes": [
        ("shape_id",),
    ],
    "stops": [
        ("stop_id", "stop_lat", "stop_lon"),
    ],
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


def parse_github_datetime(value: str) -> datetime:
    normalized_value = value.replace("Z", "+00:00")
    return parse_iso8601_datetime(normalized_value)


def build_http_request(url: str, method: str = "GET") -> Request:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "bustrack-gtfs-converter",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    return Request(url, headers=headers, method=method)


def fetch_last_modified(gtfs_url: str) -> datetime:
    request = build_http_request(gtfs_url, method="HEAD")
    with urlopen(request, timeout=30) as response:
        last_modified = response.headers.get("Last-Modified")

    if not last_modified:
        raise ValueError(f"No Last-Modified header returned for {gtfs_url}")

    return parse_http_datetime(last_modified)


def fetch_latest_release_timestamp_from_github(agency_id: str) -> datetime | None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        return None

    github_api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    releases_url = f"{github_api_url}/repos/{repository}/releases?per_page=100"
    request = build_http_request(releases_url)

    with urlopen(request, timeout=30) as response:
        payload = json.load(response)

    if not isinstance(payload, list):
        raise ValueError("GitHub releases response must be a JSON list")

    matching_timestamps: list[datetime] = []
    for release in payload:
        if not isinstance(release, dict):
            continue

        tag_name = str(release.get("tag_name", ""))
        release_name = str(release.get("name", ""))
        if not (
            tag_name == agency_id
            or tag_name.startswith(f"{agency_id}-")
            or release_name == agency_id
            or release_name.startswith(f"{agency_id}-")
        ):
            continue

        published_at = release.get("published_at") or release.get("created_at")
        if not published_at:
            continue

        matching_timestamps.append(parse_github_datetime(str(published_at)))

    if not matching_timestamps:
        return None

    return max(matching_timestamps)


def get_last_successful_release_timestamp(
    agency_id: str, cache_path: Path | None = None
) -> datetime | None:
    github_release_timestamp = fetch_latest_release_timestamp_from_github(agency_id)
    if github_release_timestamp is not None:
        return github_release_timestamp

    effective_cache_path = cache_path or get_release_cache_path()
    release_cache = load_release_cache(effective_cache_path)
    cached_release = release_cache.get(agency_id, {})
    cached_release_date = cached_release.get("released_at")
    if not cached_release_date:
        return None

    return parse_iso8601_datetime(cached_release_date)


def needs_update(
    agency_id: str, gtfs_url: str, cache_path: Path | None = None
) -> tuple[bool, datetime]:
    upstream_last_modified = fetch_last_modified(gtfs_url)
    last_release_date = get_last_successful_release_timestamp(agency_id, cache_path)
    if last_release_date is None:
        return True, upstream_last_modified

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

    return repo_root / "data" / agency_id


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


def download_and_extract_zip(url: str, target_dir: Path) -> None:
    archive_path = target_dir.parent / f"{target_dir.name}.download.zip"
    request = build_http_request(url)

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        LOGGER.info("Downloading GTFS archive from %s", url)
        with urlopen(request, timeout=120) as response, archive_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)

        LOGGER.info("Extracting GTFS archive into %s", target_dir)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(target_dir)
    except (OSError, URLError, HTTPError, zipfile.BadZipFile) as error:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        raise ValueError(f"Failed to download or extract GTFS archive: {error}") from error
    finally:
        if archive_path.exists():
            archive_path.unlink()


def zip_sqlite_file(sqlite_path: Path) -> Path:
    zip_path = sqlite_path.with_suffix(f"{sqlite_path.suffix}.zip")

    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(sqlite_path, arcname=sqlite_path.name)

    LOGGER.info("Created SQLite archive: %s", zip_path)
    return zip_path


def cleanup_extracted_folder(target_dir: Path) -> None:
    if not target_dir.exists():
        return

    shutil.rmtree(target_dir)
    LOGGER.info("Removed extracted GTFS folder: %s", target_dir)


def count_csv_rows(file_path: Path) -> int:
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def count_table_rows(connection: sqlite3.Connection, table_name: str) -> int:
    result = connection.execute(
        f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
    ).fetchone()
    if result is None:
        raise ValueError(f"Failed to count rows for table {table_name}")
    return int(result[0])


def validate_database(sqlite_path: Path, csv_folder: Path) -> tuple[int, int]:
    gtfs_files = discover_gtfs_files(csv_folder)
    validated_tables = 0
    total_rows = 0
    sanity_tables = {"stops", "trips", "stop_times"}

    connection = sqlite3.connect(sqlite_path)
    try:
        for gtfs_file in gtfs_files:
            table_name = gtfs_file.stem
            csv_row_count = count_csv_rows(gtfs_file)
            table_row_count = count_table_rows(connection, table_name)

            if table_row_count != csv_row_count:
                LOGGER.error(
                    "Validation failed for %s: SQLite has %d rows, CSV has %d rows",
                    table_name,
                    table_row_count,
                    csv_row_count,
                )
                raise SystemExit(1)

            if table_name in sanity_tables and table_row_count == 0:
                LOGGER.error("Sanity check failed: %s contains 0 rows", table_name)
                raise SystemExit(1)

            validated_tables += 1
            total_rows += table_row_count
    finally:
        connection.close()

    return validated_tables, total_rows


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


def build_index_name(table_name: str, columns: tuple[str, ...]) -> str:
    return f"idx_{table_name}_{'_'.join(columns)}"


def create_recommended_indexes(
    connection: sqlite3.Connection, gtfs_files: list[Path]
) -> None:
    created_indexes = 0

    with connection:
        for gtfs_file in gtfs_files:
            table_name = gtfs_file.stem
            available_columns = set(read_gtfs_header(gtfs_file))
            for index_columns in INDEX_SPECS.get(table_name, []):
                if not set(index_columns).issubset(available_columns):
                    LOGGER.warning(
                        "Skipping index for %s on %s because required columns are missing",
                        table_name,
                        index_columns,
                    )
                    continue

                index_name = build_index_name(table_name, index_columns)
                quoted_columns = ", ".join(
                    quote_identifier(column_name) for column_name in index_columns
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    f"{quote_identifier(index_name)} ON "
                    f"{quote_identifier(table_name)} ({quoted_columns})"
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
    parser.add_argument(
        "--cleanup-extracted",
        action="store_true",
        help="Delete the extracted GTFS folder after a successful run when data was downloaded.",
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
    should_download = False
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
            should_download = True

    if should_download:
        try:
            download_and_extract_zip(gtfs_url, input_path)
        except ValueError as error:
            LOGGER.error("%s", error)
            return 1

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

    try:
        validated_tables, total_rows = validate_database(output_path, input_path)
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error, SystemExit) as error:
        if isinstance(error, SystemExit):
            raise
        LOGGER.error("Database validation failed: %s", error)
        return 1

    try:
        zip_path = zip_sqlite_file(output_path)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        LOGGER.error("Failed to create SQLite archive: %s", error)
        return 1

    if should_download and args.cleanup_extracted:
        try:
            cleanup_extracted_folder(input_path)
        except OSError as error:
            LOGGER.error("Failed to clean up extracted GTFS folder: %s", error)
            return 1

    LOGGER.info(
        "SQLite schema, data, metadata, and indexes initialized successfully: %s",
        output_path,
    )
    LOGGER.info("SQLite archive is available at: %s", zip_path)
    LOGGER.info(
        "Successfully validated %d tables and %d total rows.",
        validated_tables,
        total_rows,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())