# BusTrack GTFS Data Engine

A serverless GTFS Schedule pipeline that downloads transit feeds, converts them into SQLite databases, validates the result, and publishes versioned release artifacts for mobile clients.

## Core Architecture

The repository keeps feed processing out of the production app stack. Conversion, indexing, validation, release creation, and rollback preparation run in GitHub Actions, while client applications consume prebuilt SQLite artifacts by agency tag.

### Data Pipeline Stages
1. **Ingestion:** Automated retrieval of GTFS ZIP archives via authenticated HTTP requests.
2. **Transformation:** Python-based conversion of GTFS `.txt` source files into SQLite tables derived from the feed headers.
3. **Performance Optimization:** Post-import creation of query-oriented SQLite indexes.
4. **Validation:** Row-count validation, required-table sanity checks, and metadata generation for app-side verification.
5. **Distribution:** Multi-tier release strategy via GitHub's global CDN.

---

## Release & Redundancy Strategy

To ensure zero-downtime and high reliability for mobile end-users, this repository employs a triple-tier release model.

| Tier | Tag Format | Scope | Purpose |
| :--- | :--- | :--- | :--- |
| **Production** | `{agency}` | Stable | Primary endpoint for production mobile applications. |
| **Previous** | `{agency}-previous` | Backup | Immediate rollback point in case of data inconsistencies. |
| **Archive** | `{agency}-YYYYMMDD-HHMM` | Historical | Immutable record for audit trails and regression testing. |

The repository does not rely on GitHub's global `Latest` badge. Clients are expected to fetch explicit agency tags such as `pvta` or `uta`.

---

## Metadata Specification (`app_metadata`)

Every generated SQLite database contains an `app_metadata` table. This serves as a "Digital Passport" for the dataset, allowing mobile clients to perform atomic validation before activating a new database version.

| Key | Description |
| :--- | :--- |
| `agency_id` | Unique identifier (e.g., `pvta`, `uta`) to prevent cross-agency data loading. |
| `build_timestamp` | ISO-8601 timestamp of the conversion process. |
| `schema_version` | Internal versioning of the database structure. |
| `git_commit_sha` | The exact version of the conversion logic used. |
| `workflow_run_id` | Reference to the GitHub Action run for full traceability. |
| `feed_start_date` | Commencement date of the GTFS schedule validity. |
| `feed_end_date` | Expiration date of the GTFS schedule validity. |

`feed_start_date` and `feed_end_date` are resolved from `feed_info.txt` when available, with a fallback to `calendar.txt`.

---

## Operations & Maintenance

### Local Conversion

Run the converter directly against a local GTFS folder:

```bash
python scripts/convert.py path/to/feed --output data/custom.sqlite
```

Run the agency-based flow using the configured URL from `config/agencies.json`:

```bash
python scripts/convert.py --agency pvta --cleanup-extracted
```

### Adding a New Agency
To integrate a new transit agency, append its configuration to `config/agencies.json`:

```json
{
  "id": "new-agency-id",
  "url": "https://source-url.com/gtfs.zip"
}
```

That is sufficient for normal operation. The sync workflow derives its agency matrix directly from `config/agencies.json`, and a push to that file also triggers the workflow.

The rollback workflow also validates the entered agency ID against `config/agencies.json`, so no workflow edits are required when onboarding a new agency.

## Manual Emergency Rollback

If an upstream GTFS update ships broken data, use the Manual Rollback workflow to restore the production tag from the previous release asset.

1. Open the Actions tab in GitHub.
2. Select the `Manual Rollback` workflow.
3. Enter the affected agency ID.
4. Start the workflow manually.

The workflow downloads `${agency}-previous.sqlite.zip` from the `${agency}-previous` release and uploads it back to the `${agency}` production release.

## Validation Summary

The pipeline currently validates:

- successful GTFS file discovery
- row counts between CSV input and SQLite output
- non-empty `stops`, `trips`, and `stop_times` tables
- release provenance fields in `app_metadata`

## Repository Layout

- `scripts/convert.py`: end-to-end GTFS download, conversion, validation, packaging, and metadata generation
- `config/agencies.json`: configured agencies and source URLs
- `data/`: generated runtime artifacts; keep only placeholder files in git
- `.github/workflows/gtfs-sync-release.yml`: scheduled and config-triggered sync, validation, and archive/previous/production publishing with a dynamic agency matrix
- `.github/workflows/rollback.yml`: manual rollback from `previous` to `production` with agency validation against `config/agencies.json`
- `tests/test_convert.py`: standard-library regression tests

# License

This project is licensed under the MIT License - see LICENSE.md for details.