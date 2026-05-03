# Schema and GTFS Differences

This repository converts each GTFS `.txt` file into a SQLite table with the same base name:

- `stops.txt` -> `stops`
- `routes.txt` -> `routes`
- `trips.txt` -> `trips`
- `stop_times.txt` -> `stop_times`
- and so on for every discovered GTFS `.txt` file

## Import behavior

- Table names come from filenames without `.txt`.
- Column names come from the CSV header row unchanged.
- Rows are imported as-is, except empty strings become `NULL`.
- The converter drops and recreates GTFS tables on each run.
- The converter does not create SQLite primary keys, foreign keys, or unique constraints for GTFS tables.

## SQLite type inference

The converter uses simple name-based inference:

- `REAL`
  - explicit known columns: `amount`, `shape_dist_traveled`, `shape_pt_lat`, `shape_pt_lon`, `stop_lat`, `stop_lon`
  - any column ending in `_lat` or `_lon`
- `INTEGER`
  - explicit known columns such as `direction_id`, `drop_off_type`, `pickup_type`, `route_type`, `wheelchair_accessible`, `wheelchair_boarding`, `stop_sequence`, `timepoint`, `exception_type`
  - any column ending in `_sequence`, `_sort_order`, `_count`, or `_type`
- `TEXT`
  - everything else

Important consequences:

- GTFS dates like `20260503` stay `TEXT`, not a date type.
- GTFS datetimes are not normalized into timestamp columns.
- GTFS times like `27:15:00` stay `TEXT`, which is correct for transit service days.
- IDs such as `trip_id`, `route_id`, `service_id`, `stop_id`, `shape_id` stay `TEXT`.

## Project-specific additions

### `app_metadata`

The converter creates a key-value table:

```sql
CREATE TABLE app_metadata (
  key TEXT PRIMARY KEY,
  value TEXT
)
```

All values are physically stored as `TEXT`, but their semantic meaning differs:

| Key | Meaning | Semantic type | Example |
| --- | --- | --- | --- |
| `build_timestamp` | UTC build time of conversion | ISO-8601 datetime string | `2026-05-03T12:34:56+00:00` |
| `build_id` | Unique build identifier | UUID string | `550e8400-e29b-41d4-a716-446655440000` |
| `agency_id` | Agency from `config/agencies.json` | String enum | `pvta`, `uta` |
| `git_commit_sha` | Converter revision | Git SHA string | `abc123...` |
| `workflow_run_id` | GitHub Actions run identifier | String containing integer-like ID | `987654321` |
| `feed_start_date` | Effective service period start | GTFS date string `YYYYMMDD` | `20260501` |
| `feed_end_date` | Effective service period end | GTFS date string `YYYYMMDD` | `20261231` |
| `gtfs_source_filename` | Local extracted folder name used as source | String | `pvta` |
| `schema_version` | App-facing schema contract version | String | `1.0` |

Notes:

- `feed_start_date` and `feed_end_date` are taken from `feed_info.txt` when both fields exist there.
- If `feed_info.txt` is missing or incomplete, the converter falls back to the first row of `calendar.txt`.
- `app_metadata` is recreated on every conversion run.

### `routes.route_rt_id`

The converter adds this non-standard column:

```sql
ALTER TABLE routes ADD COLUMN route_rt_id TEXT NOT NULL DEFAULT ''
```

Meaning:

- `route_rt_id` is the app/realtime-facing route identifier.
- For most agencies, it defaults to `route_id`.
- For `pvta`, the converter tries to map GTFS routes to provider realtime route IDs using the PVTA route details feed at:
  - `http://bustracker.pvta.com/InfoPoint/rest/routedetails/getallroutedetails`
- That payload is cached locally at:
  - `internal/assets/cache/pvta_routedetails.xml`

PVTA matching priority:

1. GTFS `routes.route_id` -> provider `ShortName`
2. GTFS `routes.route_short_name` -> provider `ShortName`
3. GTFS `routes.route_id` -> provider `RouteAbbreviation`

If no PVTA match is found, `route_rt_id` falls back to `route_id`.

## Recommended indexes

The converter creates these indexes when the needed columns exist:

- `stop_times`
  - `(trip_id)`
  - `(stop_id, departure_time)`
  - `(stop_id, arrival_time)`
  - `(stop_sequence)`
- `trips`
  - `(trip_id)`
  - `(service_id, route_id)`
  - `(trip_headsign)`
  - `(shape_id)`
- `calendar`
  - `(start_date, end_date)`
  - `(service_id, start_date, end_date)`
- `calendar_dates`
  - `(date, service_id)`
  - `(service_id, date)`
- `routes`
  - `(route_id)`
  - `(route_rt_id)` after enrichment
- `shapes`
  - `(shape_id)`
- `stops`
  - `(stop_id)`
  - `(stop_id, stop_lat, stop_lon)`

## Validation performed by the converter

After building the DB, the converter validates:

- row counts per GTFS file match CSV-to-SQLite
- `stops`, `trips`, and `stop_times` are not empty
- `app_metadata` exists and is populated by the build flow

## Good inspection queries

```sql
SELECT key, value FROM app_metadata ORDER BY key;
SELECT route_id, route_short_name, route_rt_id FROM routes ORDER BY route_id LIMIT 50;
SELECT name, sql FROM sqlite_master WHERE type = 'index' ORDER BY name;
PRAGMA table_info(routes);
PRAGMA table_info(app_metadata);
```
