from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import sqlite3
import uuid
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("gtfs_converter")
IMPORT_CHUNK_SIZE = 5000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PVTA_ROUTE_DETAILS_URL = "http://bustracker.pvta.com/InfoPoint/rest/routedetails/getallroutedetails"
CACHE_DIR_RELATIVE_PATH = Path("internal") / "assets" / "cache"
PVTA_ROUTE_DETAILS_CACHE_RELATIVE_PATH = CACHE_DIR_RELATIVE_PATH / "pvta_routedetails.xml"

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
        ("stop_id", "departure_time"),
        ("stop_id", "arrival_time"),
        ("stop_sequence",),
    ],
    "trips": [
        ("trip_id",),
        ("service_id", "route_id"),
        ("trip_headsign",),
        ("shape_id",),
    ],
    "calendar": [
        ("start_date", "end_date"),
        ("service_id", "start_date", "end_date"),
    ],
    "calendar_dates": [
        ("date", "service_id"),
        ("service_id", "date"),
    ],
    "routes": [
        ("route_id",),
    ],
    "shapes": [
        ("shape_id",),
    ],
    "stops": [
        ("stop_id",),
        ("stop_id", "stop_lat", "stop_lon"),
    ],
}


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_agencies_config_path() -> Path:
    return get_repo_root() / "config" / "agencies.json"


def get_release_cache_path() -> Path:
    return get_repo_root() / "data" / "release_cache.json"


def get_assets_cache_dir() -> Path:
    return get_repo_root() / CACHE_DIR_RELATIVE_PATH


def get_pvta_route_details_cache_path() -> Path:
    return get_repo_root() / PVTA_ROUTE_DETAILS_CACHE_RELATIVE_PATH


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


def parse_recorded_datetime(value: str) -> datetime:
    try:
        return parse_iso8601_datetime(value)
    except ValueError:
        return parse_http_datetime(value)


def normalize_release_metadata_value(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def build_http_request(url: str, method: str = "GET", github_api: bool = False) -> Request:
    headers = {
        "User-Agent": USER_AGENT,
    }
    if github_api:
        headers["Accept"] = "application/vnd.github+json"

    github_token = os.environ.get("GITHUB_TOKEN")
    if github_api and github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    return Request(url, headers=headers, method=method)


def fetch_last_modified(gtfs_url: str) -> datetime:
    request = build_http_request(gtfs_url, method="HEAD")
    with urlopen(request, timeout=30) as response:
        last_modified = response.headers.get("Last-Modified")

    if not last_modified:
        raise ValueError(f"No Last-Modified header returned for {gtfs_url}")

    return parse_http_datetime(last_modified)


def fetch_source_metadata(gtfs_url: str) -> dict[str, str]:
    request = build_http_request(gtfs_url, method="HEAD")
    with urlopen(request, timeout=30) as response:
        last_modified = response.headers.get("Last-Modified")
        etag = response.headers.get("ETag")

    metadata = {
        "source_last_modified": normalize_release_metadata_value(last_modified),
        "source_etag": normalize_release_metadata_value(etag),
    }
    if not metadata["source_last_modified"] and not metadata["source_etag"]:
        raise ValueError(f"No source metadata headers returned for {gtfs_url}")

    return metadata


def parse_release_metadata_from_body(body: str) -> dict[str, str]:
    metadata = {
        "source_last_modified": "",
        "source_etag": "",
    }

    for line in body.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("<!-- source_last_modified:") and stripped_line.endswith("-->"):
            metadata["source_last_modified"] = normalize_release_metadata_value(
                stripped_line.removeprefix("<!-- source_last_modified:").removesuffix("-->")
            )
        if stripped_line.startswith("<!-- source_etag:") and stripped_line.endswith("-->"):
            metadata["source_etag"] = normalize_release_metadata_value(
                stripped_line.removeprefix("<!-- source_etag:").removesuffix("-->")
            )

    return metadata


def fetch_release_source_metadata_from_github(agency_id: str) -> dict[str, str] | None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        return None

    github_api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    release_url = f"{github_api_url}/repos/{repository}/releases/tags/{agency_id}"
    request = build_http_request(release_url, github_api=True)

    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise

    if not isinstance(payload, dict):
        raise ValueError("GitHub release response must be a JSON object")

    body = str(payload.get("body", ""))
    metadata = parse_release_metadata_from_body(body)
    published_at = payload.get("published_at") or payload.get("created_at")
    if published_at:
        metadata["released_at"] = parse_github_datetime(str(published_at)).isoformat(timespec="seconds")

    return metadata


def get_last_successful_release_metadata(
    agency_id: str, cache_path: Path | None = None
) -> dict[str, str]:
    github_release_metadata = fetch_release_source_metadata_from_github(agency_id)
    if github_release_metadata is not None:
        return github_release_metadata

    effective_cache_path = cache_path or get_release_cache_path()
    release_cache = load_release_cache(effective_cache_path)
    return {
        "released_at": normalize_release_metadata_value(release_cache.get(agency_id, {}).get("released_at")),
        "source_last_modified": normalize_release_metadata_value(release_cache.get(agency_id, {}).get("source_last_modified")),
        "source_etag": normalize_release_metadata_value(release_cache.get(agency_id, {}).get("source_etag")),
    }


def compare_source_metadata(
    upstream_metadata: dict[str, str], last_release_metadata: dict[str, str]
) -> tuple[bool, str]:
    upstream_etag = upstream_metadata.get("source_etag", "")
    release_etag = last_release_metadata.get("source_etag", "")
    if upstream_etag and release_etag:
        if upstream_etag != release_etag:
            return True, "source_etag_changed"
        return False, "source_etag_unchanged"

    upstream_last_modified = upstream_metadata.get("source_last_modified", "")
    release_last_modified = last_release_metadata.get("source_last_modified", "")
    if upstream_last_modified and release_last_modified:
        upstream_dt = parse_http_datetime(upstream_last_modified)
        release_dt = parse_recorded_datetime(release_last_modified)
        if upstream_dt > release_dt:
            return True, "source_last_modified_newer"
        return False, "source_last_modified_not_newer"

    return True, "missing_comparable_release_metadata"


def needs_update(
    agency_id: str, gtfs_url: str, cache_path: Path | None = None
) -> tuple[bool, dict[str, str], dict[str, str], str]:
    upstream_metadata = fetch_source_metadata(gtfs_url)
    last_release_metadata = get_last_successful_release_metadata(agency_id, cache_path)

    if not last_release_metadata.get("source_last_modified") and not last_release_metadata.get("source_etag"):
        return True, upstream_metadata, last_release_metadata, "no_previous_source_metadata"

    should_update, decision_reason = compare_source_metadata(upstream_metadata, last_release_metadata)
    return should_update, upstream_metadata, last_release_metadata, decision_reason


def update_release_cache(
    agency_id: str,
    upstream_metadata: dict[str, str],
    cache_path: Path | None = None,
) -> None:
    effective_cache_path = cache_path or get_release_cache_path()
    release_cache = load_release_cache(effective_cache_path)
    release_cache[agency_id] = {
        "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_last_modified": normalize_release_metadata_value(upstream_metadata.get("source_last_modified")),
        "source_etag": normalize_release_metadata_value(upstream_metadata.get("source_etag")),
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


def is_folder_empty(folder_path: Path) -> bool:
    if not folder_path.exists() or not folder_path.is_dir():
        return True

    return not any(folder_path.iterdir())


def flatten_extracted_gtfs_files(target_dir: Path) -> None:
    direct_txt_files = [path for path in target_dir.iterdir() if path.is_file() and path.suffix == ".txt"]
    if direct_txt_files:
        return

    nested_txt_files = [
        path for path in target_dir.rglob("*.txt") if path.is_file() and path.parent != target_dir
    ]
    if not nested_txt_files:
        return

    for source_path in nested_txt_files:
        destination_path = target_dir / source_path.name
        if destination_path.exists() and destination_path != source_path:
            raise ValueError(
                f"Duplicate GTFS file encountered while flattening archive: {destination_path.name}"
            )
        shutil.move(str(source_path), str(destination_path))


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

        flatten_extracted_gtfs_files(target_dir)
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


def list_table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table_name)})"
    ).fetchall()
    return {str(row[1]) for row in rows}


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_route_rt_id_column(connection: sqlite3.Connection) -> None:
    columns = list_table_columns(connection, "routes")
    if "route_rt_id" in columns:
        return

    connection.execute(
        f"ALTER TABLE {quote_identifier('routes')} "
        f"ADD COLUMN {quote_identifier('route_rt_id')} TEXT NOT NULL DEFAULT ''"
    )


def create_route_rt_id_index(connection: sqlite3.Connection) -> None:
    index_name = build_index_name("routes", ("route_rt_id",))
    connection.execute(
        "CREATE INDEX IF NOT EXISTS "
        f"{quote_identifier(index_name)} ON "
        f"{quote_identifier('routes')} ({quote_identifier('route_rt_id')})"
    )


def xml_child_text(element: ElementTree.Element, child_name: str) -> str:
    for child in list(element):
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name == child_name:
            return (child.text or "").strip()
    return ""


def normalize_route_match_key(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(value.split()).upper()


def parse_pvta_route_details_records(xml_payload: bytes) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(xml_payload)
    except ElementTree.ParseError:
        payload = json.loads(xml_payload.decode("utf-8-sig"))
        if not isinstance(payload, list):
            raise

        records: list[dict[str, str]] = []
        for route in payload:
            if not isinstance(route, dict):
                continue

            route_id = str(route.get("RouteId") or "").strip()
            route_abbreviation = str(route.get("RouteAbbreviation") or "").strip()
            short_name = str(route.get("ShortName") or "").strip()
            if route_id:
                records.append(
                    {
                        "route_rt_id": route_id,
                        "route_abbreviation": route_abbreviation,
                        "short_name": short_name,
                    }
                )

        return records

    records: list[dict[str, str]] = []

    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name != "Route":
            continue

        route_id = xml_child_text(element, "RouteId")
        route_abbreviation = xml_child_text(element, "RouteAbbreviation")
        short_name = xml_child_text(element, "ShortName")
        if route_id:
            records.append(
                {
                    "route_rt_id": route_id,
                    "route_abbreviation": route_abbreviation,
                    "short_name": short_name,
                }
            )

    return records


def parse_pvta_route_details_mapping(xml_payload: bytes) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for record in parse_pvta_route_details_records(xml_payload):
        route_abbreviation = record.get("route_abbreviation", "")
        route_rt_id = record.get("route_rt_id", "")
        if route_abbreviation and route_rt_id:
            mapping[route_abbreviation] = route_rt_id
    return mapping


def load_pvta_route_details_xml(cache_path: Path | None = None) -> bytes | None:
    effective_cache_path = cache_path or get_pvta_route_details_cache_path()
    request = build_http_request(PVTA_ROUTE_DETAILS_URL)

    try:
        LOGGER.info("Downloading PVTA route details from %s", PVTA_ROUTE_DETAILS_URL)
        with urlopen(request, timeout=30) as response:
            xml_payload = response.read()
    except (OSError, URLError, HTTPError) as error:
        LOGGER.warning("PVTA route details download failed: %s", error)
        if not effective_cache_path.exists():
            LOGGER.warning("PVTA route details cache is unavailable: %s", effective_cache_path)
            return None

        LOGGER.warning("Using cached PVTA route details XML: %s", effective_cache_path)
        try:
            return effective_cache_path.read_bytes()
        except OSError as cache_error:
            LOGGER.warning("PVTA route details cache could not be read: %s", cache_error)
            return None

    try:
        effective_cache_path.parent.mkdir(parents=True, exist_ok=True)
        effective_cache_path.write_bytes(xml_payload)
    except OSError as cache_error:
        LOGGER.warning("PVTA route details XML could not be cached: %s", cache_error)
    else:
        LOGGER.info("Cached PVTA route details XML at %s", effective_cache_path)

    return xml_payload


def build_provider_route_rt_id_records(
    agency_id: str, cache_path: Path | None = None
) -> list[dict[str, str]]:
    if agency_id != "pvta":
        return []

    xml_payload = load_pvta_route_details_xml(cache_path)
    if xml_payload is None:
        return []

    try:
        records = parse_pvta_route_details_records(xml_payload)
    except (ElementTree.ParseError, json.JSONDecodeError, UnicodeDecodeError) as error:
        LOGGER.warning("PVTA route details payload could not be parsed: %s", error)
        return []

    LOGGER.info("Loaded %d PVTA realtime route records", len(records))
    return records


def enrich_route_realtime_ids(
    connection: sqlite3.Connection,
    agency_id: str,
    cache_path: Path | None = None,
) -> None:
    if not table_exists(connection, "routes"):
        LOGGER.warning("Skipping route realtime ID enrichment because routes table is missing")
        return

    provider_records = build_provider_route_rt_id_records(agency_id, cache_path)

    with connection:
        ensure_route_rt_id_column(connection)
        route_columns = list_table_columns(connection, "routes")
        connection.execute(
            f"UPDATE {quote_identifier('routes')} "
            f"SET {quote_identifier('route_rt_id')} = COALESCE(CAST({quote_identifier('route_id')} AS TEXT), '')"
        )

        if provider_records:
            short_name_map: dict[str, str] = {}
            route_abbreviation_map: dict[str, str] = {}
            for record in provider_records:
                route_rt_id = record.get("route_rt_id", "")
                normalized_short_name = normalize_route_match_key(record.get("short_name"))
                normalized_route_abbreviation = normalize_route_match_key(
                    record.get("route_abbreviation")
                )
                if route_rt_id and normalized_short_name:
                    short_name_map[normalized_short_name] = route_rt_id
                if route_rt_id and normalized_route_abbreviation:
                    route_abbreviation_map[normalized_route_abbreviation] = route_rt_id

            select_columns = ["rowid", "route_id"]
            if "route_short_name" in route_columns:
                select_columns.append("route_short_name")
            route_rows = connection.execute(
                "SELECT "
                + ", ".join(quote_identifier(column_name) for column_name in select_columns)
                + f" FROM {quote_identifier('routes')}"
            ).fetchall()

            route_rt_id_updates: list[tuple[str, int]] = []
            for route_row in route_rows:
                rowid = int(route_row[0])
                route_id = "" if route_row[1] is None else str(route_row[1])
                route_short_name = ""
                if len(route_row) > 2 and route_row[2] is not None:
                    route_short_name = str(route_row[2])

                normalized_route_id = normalize_route_match_key(route_id)
                normalized_route_short_name = normalize_route_match_key(route_short_name)

                matched_route_rt_id = ""
                match_level = ""
                matched_value = ""

                if normalized_route_id in short_name_map:
                    matched_route_rt_id = short_name_map[normalized_route_id]
                    match_level = "level_1_route_id_to_short_name"
                    matched_value = normalized_route_id
                elif normalized_route_short_name in short_name_map:
                    matched_route_rt_id = short_name_map[normalized_route_short_name]
                    match_level = "level_2_route_short_name_to_short_name"
                    matched_value = normalized_route_short_name
                elif normalized_route_id in route_abbreviation_map:
                    matched_route_rt_id = route_abbreviation_map[normalized_route_id]
                    match_level = "level_3_route_id_to_route_abbreviation"
                    matched_value = normalized_route_id

                if matched_route_rt_id:
                    route_rt_id_updates.append((matched_route_rt_id, rowid))
                    if matched_route_rt_id != route_id:
                        LOGGER.info(
                            "PVTA route realtime mapping applied: route_id=%s, route_short_name=%s, route_rt_id=%s, match_level=%s, matched_value=%s",
                            route_id,
                            route_short_name,
                            matched_route_rt_id,
                            match_level,
                            matched_value,
                        )

            if route_rt_id_updates:
                connection.executemany(
                    f"UPDATE {quote_identifier('routes')} "
                    f"SET {quote_identifier('route_rt_id')} = ? "
                    "WHERE rowid = ?",
                    route_rt_id_updates,
                )

        connection.execute(
            f"UPDATE {quote_identifier('routes')} "
            f"SET {quote_identifier('route_rt_id')} = COALESCE(NULLIF({quote_identifier('route_rt_id')}, ''), "
            f"COALESCE(CAST({quote_identifier('route_id')} AS TEXT), ''))"
        )
        create_route_rt_id_index(connection)

        verification_short_name_sql = "''"
        if "route_short_name" in route_columns:
            verification_short_name_sql = (
                f"COALESCE({quote_identifier('route_short_name')}, '')"
            )
        for route_id, route_short_name, route_rt_id in connection.execute(
            f"SELECT {quote_identifier('route_id')}, "
            f"{verification_short_name_sql}, "
            f"{quote_identifier('route_rt_id')} "
            f"FROM {quote_identifier('routes')} "
            f"WHERE {quote_identifier('route_rt_id')} != COALESCE(CAST({quote_identifier('route_id')} AS TEXT), '') "
            f"ORDER BY {quote_identifier('route_id')}"
        ).fetchall():
            LOGGER.info(
                "PVTA route realtime ID verified: route_id=%s, route_short_name=%s, route_rt_id=%s",
                route_id,
                route_short_name,
                route_rt_id,
            )

    LOGGER.info("Initialized routes.route_rt_id for agency %s", agency_id)


def read_first_csv_row(file_path: Path) -> dict[str, str] | None:
    if not file_path.exists():
        return None

    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        first_row = next(reader, None)

    if first_row is None:
        return None

    return {str(key): value or "" for key, value in first_row.items() if key is not None}


def extract_feed_date_range(input_path: Path) -> tuple[str, str]:
    feed_info_row = read_first_csv_row(input_path / "feed_info.txt")
    if feed_info_row is not None:
        feed_start_date = feed_info_row.get("feed_start_date", "").strip()
        feed_end_date = feed_info_row.get("feed_end_date", "").strip()
        if feed_start_date and feed_end_date:
            return feed_start_date, feed_end_date

    calendar_row = read_first_csv_row(input_path / "calendar.txt")
    if calendar_row is not None:
        feed_start_date = calendar_row.get("start_date", "").strip()
        feed_end_date = calendar_row.get("end_date", "").strip()
        if feed_start_date and feed_end_date:
            return feed_start_date, feed_end_date

    return "", ""


def create_app_metadata(
    connection: sqlite3.Connection,
    input_path: Path,
    agency_id: str,
    schema_version: str = "1.0",
) -> None:
    feed_start_date, feed_end_date = extract_feed_date_range(input_path)
    metadata_rows = [
        (
            "build_timestamp",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        ("build_id", str(uuid.uuid4())),
        ("agency_id", agency_id),
        ("git_commit_sha", os.environ.get("GITHUB_SHA", "")),
        ("workflow_run_id", os.environ.get("GITHUB_RUN_ID", "")),
        ("feed_start_date", feed_start_date),
        ("feed_end_date", feed_end_date),
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


def write_release_notes(
    release_notes_path: Path,
    agency_id: str,
    source_metadata: dict[str, str],
    validated_tables: int,
    total_rows: int,
) -> None:
    workflow_run_id = os.environ.get("GITHUB_RUN_ID", "")
    github_repository = os.environ.get("GITHUB_REPOSITORY", "")
    github_server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    github_sha = os.environ.get("GITHUB_SHA", "")
    workflow_url = ""
    if workflow_run_id and github_repository:
        workflow_url = f"{github_server_url}/{github_repository}/actions/runs/{workflow_run_id}"

    notes_lines = [
        f"Automated GTFS release for {agency_id}.",
        f"Validated tables: {validated_tables}",
        f"Validated rows: {total_rows}",
    ]
    if workflow_url:
        notes_lines.append(f"Workflow run: {workflow_url}")
    if github_sha:
        notes_lines.append(f"Commit: {github_sha}")
    notes_lines.extend(
        [
            "",
            f"<!-- source_last_modified: {normalize_release_metadata_value(source_metadata.get('source_last_modified'))} -->",
            f"<!-- source_etag: {normalize_release_metadata_value(source_metadata.get('source_etag'))} -->",
        ]
    )

    release_notes_path.write_text("\n".join(notes_lines) + "\n", encoding="utf-8")
    LOGGER.info("Wrote release notes to %s", release_notes_path)


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
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="Force download and conversion even if no newer GTFS source metadata is detected.",
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

    upstream_metadata: dict[str, str] | None = None
    should_download = False
    release_check_failed = False
    if args.agency and gtfs_url and args.input_path is None:
        if args.force_update:
            should_download = True
            LOGGER.info(
                "Force update enabled; skipping remote release comparison and downloading GTFS source."
            )
        else:
            try:
                should_update, upstream_metadata, last_release_metadata, decision_reason = needs_update(
                    args.agency, gtfs_url
                )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                HTTPError,
                URLError,
            ) as error:
                release_check_failed = True
                LOGGER.warning("Release check failed, evaluating local GTFS availability: %s", error)
            else:
                LOGGER.info(
                    "Release decision for %s: should_update=%s, reason=%s, upstream_last_modified=%s, upstream_etag=%s, release_source_last_modified=%s, release_source_etag=%s",
                    args.agency,
                    should_update,
                    decision_reason,
                    upstream_metadata.get("source_last_modified", ""),
                    upstream_metadata.get("source_etag", ""),
                    last_release_metadata.get("source_last_modified", ""),
                    last_release_metadata.get("source_etag", ""),
                )
                if not should_update:
                    LOGGER.info("No update needed; skipping download, conversion, and release creation.")
                    return 0
                should_download = True

    if gtfs_url and release_check_failed and (not input_path.exists() or is_folder_empty(input_path)):
        if release_check_failed:
            LOGGER.info("Local GTFS folder is missing or empty after release-check failure. Forcing download.")
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
        enrich_route_realtime_ids(connection, args.agency or input_path.name)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOGGER.error("Failed to enrich route realtime IDs: %s", error)
        connection.close()
        return 1

    try:
        create_app_metadata(connection, input_path, args.agency or input_path.name)
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

    if args.agency and upstream_metadata is not None:
        try:
            update_release_cache(args.agency, upstream_metadata)
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

    if args.agency and upstream_metadata is not None:
        release_notes_path = output_path.with_suffix(".release-notes.md")
        try:
            write_release_notes(
                release_notes_path,
                args.agency,
                upstream_metadata,
                validated_tables,
                total_rows,
            )
        except OSError as error:
            LOGGER.error("Failed to write release notes: %s", error)
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
