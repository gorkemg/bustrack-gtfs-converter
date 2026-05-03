# Platforms and Release URLs

## Release asset contract

This repository publishes versioned release assets for mobile/web clients on GitHub Releases.

Repository:

- `gorkemg/bustrack-gtfs-converter`

Tag strategy:

- production: `{agency}`
- rollback backup: `{agency}-previous`
- archive snapshot: `{agency}-YYYYMMDD-HHMM`

Examples:

- `pvta`
- `pvta-previous`
- `pvta-20260503-1430`

Expected asset names from the converter:

- `{agency}.sqlite`
- `{agency}.sqlite.zip`
- optionally `{agency}.release-notes.md` when built from an agency download flow

Typical GitHub release asset URL pattern:

- `https://github.com/gorkemg/bustrack-gtfs-converter/releases/download/{tag}/{agency}.sqlite.zip`

Examples:

- `https://github.com/gorkemg/bustrack-gtfs-converter/releases/download/pvta/pvta.sqlite.zip`
- `https://github.com/gorkemg/bustrack-gtfs-converter/releases/download/uta/uta.sqlite.zip`

Important:

- Do not rely on GitHub's generic `latest` release URL.
- Select the asset by explicit tag and agency.
- Treat `.sqlite.zip` as the distribution format and unzip before opening with SQLite libraries.

## Validation before activation

Before an app swaps to a downloaded database, validate:

1. The file can be opened as SQLite.
2. `app_metadata` exists.
3. `agency_id` matches the expected app or feed context.
4. `schema_version` is supported by the client.
5. `feed_start_date` and `feed_end_date` look plausible.
6. Key tables such as `stops`, `trips`, and `stop_times` are non-empty when that matters for your flow.

Useful query:

```sql
SELECT key, value FROM app_metadata WHERE key IN (
  'agency_id',
  'schema_version',
  'feed_start_date',
  'feed_end_date',
  'build_timestamp',
  'build_id'
) ORDER BY key;
```

## iOS and Swift

Recommended flow:

1. Download `{agency}.sqlite.zip` to a temporary file.
2. Unzip into `Application Support`.
3. Keep a stable local filename like `gtfs-active.sqlite`.
4. Open with SQLite.swift, GRDB, or the system SQLite C API.
5. Query `app_metadata` before promoting a candidate DB to active use.

Example path choices:

- bundled seed DB: `Bundle.main.url(forResource: "pvta", withExtension: "sqlite")`
- downloaded DB: `FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first`

Swift sketch:

```swift
let appSupport = try FileManager.default.url(
    for: .applicationSupportDirectory,
    in: .userDomainMask,
    appropriateFor: nil,
    create: true
)
let dbURL = appSupport.appendingPathComponent("gtfs-active.sqlite")
```

Notes:

- If you use `URLSessionDownloadTask`, move the file out of the temporary location before unzipping or opening it.
- Do not try to query the ZIP directly.
- Preserve the DB on disk; SQLite expects random file access.

## Android with Kotlin or Java

Recommended flow:

1. Download `{agency}.sqlite.zip` with `OkHttp`, `HttpURLConnection`, Ktor, or WorkManager-based background code.
2. Unzip to `context.noBackupFilesDir`, `context.filesDir`, or another app-private directory.
3. Open via `android.database.sqlite`, SQLDelight, or Room in raw-query mode if you are not generating entities for every GTFS table.

Good local directories:

- `context.filesDir`
- `context.noBackupFilesDir`
- `context.getDatabasePath("gtfs-active.sqlite").parentFile`

Kotlin sketch:

```kotlin
val dbFile = File(context.noBackupFilesDir, "gtfs-active.sqlite")
val db = SQLiteDatabase.openDatabase(
    dbFile.path,
    null,
    SQLiteDatabase.OPEN_READONLY
)
```

Notes:

- `Room` is possible but often awkward for a dynamic GTFS schema plus raw transit queries.
- Raw SQLite access is usually simpler because GTFS tables mirror CSV headers and may vary by feed.
- Validate `app_metadata` before switching any pointer or filename that marks the DB as active.

## Web

Web apps cannot rely on the native filesystem like mobile apps. Typical options:

- download and unzip on a backend, then expose query APIs from the server
- use a browser SQLite runtime such as `sql.js` or a WASM SQLite build after fetching the unzipped `.sqlite`
- cache the DB in IndexedDB or the Origin Private File System if your stack supports it

Practical caution:

- Shipping large SQLite files directly to browsers may hurt startup time.
- For web, server-side querying or precomputed API endpoints are often the better default.

## Local inspection

Useful commands:

```bash
unzip -l data/pvta.sqlite.zip
sqlite3 data/pvta.sqlite ".tables"
sqlite3 data/pvta.sqlite "SELECT key, value FROM app_metadata ORDER BY key;"
sqlite3 data/pvta.sqlite "SELECT route_id, route_short_name, route_rt_id FROM routes LIMIT 20;"
```

If you need to confirm the DB really belongs to a target agency:

```bash
sqlite3 data/pvta.sqlite "SELECT value FROM app_metadata WHERE key = 'agency_id';"
```
