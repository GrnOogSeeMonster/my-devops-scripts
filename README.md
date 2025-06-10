# Redash Backup & Restore Utility

This script provides three primary functions for your Docker-based Redash instance:

1. **Initialization** (`--init`)  
   Ensure required directories exist and record that initialization has run.
2. **Backup** (`--backup <OUTDIR>`)  
   Export PostgreSQL, Redis, Docker Compose, and environment files into a timestamped ZIP.
3. **Restore** (`--restore <ZIP> --project-dir <DIR>`)  
   Unzip, load data back into containers, and bring your stack back up.

---

## Prerequisites

- Ubuntu 18.04+ (Bionic or later)
- Python 3.x
- Docker & Docker Compose installed
- Your Redash stack lives under `/opt/redash` (adjust `COMPOSE_FILE` & `ENV_FILE` in the script if needed)

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

## Command-line Options

| Flag                  | Description                                                   |
| --------------------- | ------------------------------------------------------------- |
| `--init`              | Initialize directories & record state                         |
| `--backup <OUTDIR>`   | Backup into specified directory                               |
| `--restore <ARCHIVE>` | Restore from given ZIP archive                                |
| `--project-dir <DIR>` | Directory containing your `docker-compose.yml` (default: cwd) |
| `-h`, `--help`        | Show usage information                                        |

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
