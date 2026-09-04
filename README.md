# my-devops-scripts

Operational tooling for self-hosted [Redash](https://redash.io/): a single Python script
that backs up, restores, diagnoses and version-upgrades a Docker Compose Redash stack.

---

## Status

**In use and considerably larger than this README once described.** Last worked on
25 June 2025.

| | |
|---|---|
| Size | `redash_backup.py` is 2,715 lines / 36 functions; `backup_redash.sh` is a 90-line shell wrapper |
| Scope | Backup and restore, plus database validation, user management, secret-key repair, version upgrade and a set of restore-diagnosis commands |
| Tests | None. It is verified by use against a real instance |
| Blast radius | `--restore` and `--force-clean` stop the stack and destroy volumes. Read the flag before you type it |

The script grew out of a real restore that went wrong: most of what is beyond `--backup`
and `--restore` exists because a restore silently produced an empty dashboard list and
the cause turned out to be a `REDASH_SECRET_KEY` mismatch between the backup and the
target host. `--analyze-backup`, `--diagnose`, `--debug-data` and `--fix-secret-key` are the tools
built to find that class of problem quickly.

It detects both the legacy (`redash_postgres_1`) and modern (`redash-postgres-1`)
container naming, and both `docker-compose` and `docker compose`, so it works across
Redash installs of different vintages.

---

## Commands

**Setup**

| Flag | |
|---|---|
| `--init` | Create `/opt/backups` and `/var/log/redash`, record initialisation state |

**Backup and restore**

| Flag | |
|---|---|
| `--backup <OUTDIR>` | Postgres dump + Redis `dump.rdb` + `docker-compose.yml` + `.env` into a timestamped ZIP |
| `--restore <ARCHIVE>` | Stop the stack, load Postgres and Redis, restore configs, bring it back up |
| `--enhanced-restore <ARCHIVE>` | Restore with extra validation and recovery steps |
| `--force-clean` | **Destructive.** Wipe volumes before restoring |

**Safety and validation**

| Flag | |
|---|---|
| `--validate-db` | Check the database is present and populated |
| `--diagnose` | Diagnose a restore that appears to have produced no data |

**User management**

| Flag | |
|---|---|
| `--list-users` | List Redash users |
| `--reset-password <EMAIL> [PASSWORD]` | Reset a user's password |

**Debug and troubleshooting**

| Flag | |
|---|---|
| `--analyze-backup <ARCHIVE>` | Inspect an archive's contents without restoring |
| `--test-restore <ARCHIVE>` | Dry-run a restore step by step |
| `--manual-restore-test <ARCHIVE>` | Walk a restore manually for debugging |
| `--debug-data` | Trace missing data after a restore |
| `--fix-secret-key <KEY>` | Repair a `REDASH_SECRET_KEY` mismatch between backup and host |

**Version management**

| Flag | |
|---|---|
| `--upgrade-10`, `--upgrade-25` | Semantic upgrade to Redash 10 / 25 |
| `--setup-10`, `--setup-25`, `--setup-version <V>` | Fresh install at a given version |
| `--dry-run` | Preview an upgrade or setup without executing |

**Global**

| Flag | |
|---|---|
| `--project-dir <DIR>` | Directory holding `docker-compose.yml` (default `/opt/redash`) |

---

## Prerequisites

Ubuntu 18.04+, Python 3, Docker and Docker Compose, and a Redash stack under
`/opt/redash` (or pass `--project-dir`).

---

## Installation

1. Copy `redash_backup.py` to `/usr/local/bin/` and make it executable:
   ```bash
   sudo mv redash_backup.py /usr/local/bin/
   sudo chmod +x /usr/local/bin/redash_backup.py
   ```
2. Ensure the script meets your environment. Edit container names or paths at the top of the file if necessary.

---

## Usage

### 1. Initialize

Set up backup and log directories, and record initialization state:

```bash
/usr/local/bin/redash_backup.py --init
```

- Creates `/opt/backups` & `/var/log/redash` if missing
- Writes a state file at `~/.redash_backup_initialized`
- If run again, reports the original initialization timestamp

### 2. Backup

Perform a full backup, placing a ZIP in your chosen directory:

```bash
/usr/local/bin/redash_backup.py --backup /opt/backups
```

- Generates `redash-backup-<TIMESTAMP>.zip`
- Captures:
  - Full PostgreSQL dump
  - Redis `dump.rdb`
  - `docker-compose.yml` & `.env`

### 3. Restore

Restore from a backup archive:

```bash
/usr/local/bin/redash_backup.py --restore /opt/backups/redash-backup-20250610T020000Z.zip \
    --project-dir /opt/redash
```

- Stops the current Redash stack
- Loads data into Postgres & Redis
- Restores Compose/Env files if present
- Brings the stack back up in detached mode

---

## Automation (cron)

Add a daily backup at 2 AM UTC to root’s crontab (`crontab -e`):

```
0 2 * * * /usr/local/bin/redash_backup.py --backup /opt/backups \
    >> /var/log/redash/backup.log 2>&1
```

---

## Logical Flow

1. **Init**: create folders → record state → exit.
2. **Backup**: create workdir → dump Postgres → dump Redis → copy configs → zip → cleanup.
3. **Restore**: extract ZIP → stop stack → restore Postgres → restore Redis → copy configs → start stack → cleanup.

---
