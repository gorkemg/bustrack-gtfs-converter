# Release flow: main, staging, production

This repository follows the BusTrack branching model (project document `overview/git-branching-model.md`).

| Branch / tag | Role | Publishes? |
|---|---|---|
| `main` | default and integration branch; PRs with `security-check` | **no** |
| `staging` | pointer, fast-forward only | every push → `{agency}-prerelease`, `motis-prerelease` |
| `production` | pointer, fast-forward only | push touching `config/agencies.json` → production tiers; cron, dispatch, MOTIS and asset rollback always use `production` code |
| `prod-YYYYMMDD-HHMM` | tag on every production promotion | no |

Release tag names (`{agency}`, `{agency}-previous`, `{agency}-prerelease`, `{agency}-YYYYMMDD-HHMM`, `motis`, `motis-prerelease`) are a consumer contract and unchanged.

Scheduled and dispatched runs use the workflow *definition* from `main` (GitHub always runs schedules from the default branch), but every job checks out pointer code.

## Promote

Actions → **Promote** → *Run workflow* on `main`:

- `target: staging`, optional `sha` (default `main` HEAD, must be on `main`). The push builds the prerelease.
- `target: production`, optional `sha` (must equal the `staging` HEAD). Owner only. Tags `prod-*`. Tick `rebuild` to dispatch *GTFS Sync & Release* with `force_update` afterwards; code-only changes otherwise wait for the next upstream feed change.

```sh
R=gorkemg/bustrack-gtfs-converter
gh workflow run promote.yml -R $R --ref main -f target=staging
gh workflow run promote.yml -R $R --ref main -f target=production -f rebuild=true
```

The pointer moves with the `PROMOTE_SSH_KEY` deploy key (secret in the environments `staging` and `production`): a `GITHUB_TOKEN` push would trigger nothing and is refused for ranges that touch `.github/workflows`.

## Roll back

- Bad artifact: **Manual Rollback** (`rollback.yml`) restores `{agency}` from `{agency}-previous`.
- Bad code on production: **Roll back production pointer** (`rollback-pointer.yml`) with a `prod-*` tag, owner only. Then fix forward on `main`.

## Rules for agents

Merge into `main` where allowed (it publishes nothing). Promote `staging` only when the owner asked for a beta test. Never move `production`.
