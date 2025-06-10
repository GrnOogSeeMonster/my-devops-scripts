#!/usr/bin/env python3
import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime

# Default container names (adjust if yours differ)
PG_CONTAINER    = "redash_postgres_1"
REDIS_CONTAINER = "redash_redis_1"
COMPOSE_FILE    = "/opt/redash/docker-compose.yml"
ENV_FILE        = "/opt/redash/.env"  # adjust if your env lives elsewhere

# Default directories for init
BACKUP_DIR      = "/opt/backups"
LOG_DIR         = "/var/log/redash"
STATE_FILE      = os.path.expanduser("~/.redash_backup_initialized")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

def run(cmd, **kwargs):
    logger.debug(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, **kwargs)
    if result.returncode != 0:
        logger.error(f"Command failed: {cmd}")
        sys.exit(result.returncode)
    return result


def do_init():
    """
    Create required directories and record initialization state.
    """
    actions = []

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            prev = f.read().strip()
        print(f"⚠️  Initialization already ran on {prev}")
    else:
        # Ensure backup and log dirs
        for d in [BACKUP_DIR, LOG_DIR]:
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
                actions.append(f"Created directory: {d}")
            else:
                actions.append(f"Directory already exists: {d}")
        timestamp = datetime.utcnow().isoformat()
        with open(STATE_FILE, "w") as f:
            f.write(timestamp)
        actions.append(f"Recorded initialization at {timestamp}")

        print("✅ Initialization complete. Actions:")
        for a in actions:
            print(f" - {a}")
    sys.exit(0)


def backup(output_dir):
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    workdir = os.path.join(output_dir, f"redash-backup-{timestamp}")
    os.makedirs(workdir, exist_ok=True)
    logger.info(f"Backing up into {workdir}")

    # 1) Postgres dump
    pg_dump_file = os.path.join(workdir, f"postgres-{timestamp}.sql")
    cmd = (
        f"docker exec {PG_CONTAINER} pg_dumpall -U postgres "
        f"> {pg_dump_file}"
    )
    run(cmd)

    # 2) Redis dump
    logger.info("Triggering Redis SAVE")
    run(f"docker exec {REDIS_CONTAINER} redis-cli SAVE")
    redis_dump = os.path.join(workdir, "redis-dump.rdb")
    run(f"docker cp {REDIS_CONTAINER}:/data/dump.rdb {redis_dump}")

    # 3) Compose & env
    for fn in [COMPOSE_FILE, ENV_FILE]:
        if os.path.isfile(fn):
            shutil.copy(fn, workdir)
        else:
            logger.warning(f"{fn} not found, skipping")

    # 4) Zip it up
    archive = os.path.join(output_dir, f"redash-backup-{timestamp}.zip")
    logger.info(f"Creating archive {archive}")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(workdir):
            for file in files:
                full = os.path.join(root, file)
                arcname = os.path.relpath(full, workdir)
                zf.write(full, arcname)

    # 5) Cleanup
    shutil.rmtree(workdir)
    logger.info("Backup complete.")
    return archive


def restore(archive, project_dir):
    temp = tempfile.mkdtemp(prefix="redash-restore-")
    logger.info(f"Extracting {archive} to {temp}")
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(temp)

    os.chdir(project_dir)
    logger.info("Stopping existing Redash stack")
    run("docker-compose down")

    # Restore Postgres
    sqls = [f for f in os.listdir(temp) if f.endswith(".sql")]
    if not sqls:
        logger.error("No .sql dump found in archive")
        sys.exit(1)
    sql_file = os.path.join(temp, sqls[0])
    logger.info(f"Restoring Postgres from {sql_file}")
    with open(sql_file, "rb") as src:
        proc = subprocess.Popen(
            f"docker exec -i {PG_CONTAINER} psql -U postgres",
            shell=True, stdin=src
        )
        proc.wait()
        if proc.returncode != 0:
            logger.error("Postgres restore failed")
            sys.exit(proc.returncode)

    # Restore Redis
    redis_backup = os.path.join(temp, "redis-dump.rdb")
    if os.path.isfile(redis_backup):
        logger.info("Copying Redis dump back into container")
        run(f"docker cp {redis_backup} {REDIS_CONTAINER}:/data/dump.rdb")
    else:
        logger.warning("No redis-dump.rdb found, skipping")

    # Optionally restore compose/env
    for fn in [COMPOSE_FILE, ENV_FILE]:
        base = os.path.basename(fn)
        src = os.path.join(temp, base)
        if os.path.isfile(src):
            shutil.copy(src, fn)

    logger.info("Bringing Redash back up")
    run("docker-compose up -d")
    shutil.rmtree(temp)
    logger.info("Restore complete.")


def main():
    parser = argparse.ArgumentParser(
        description="Redash full backup, restore, and initialization"
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Initialize directories and record state"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--backup", metavar="OUTDIR",
        help="Perform a backup to OUTDIR"
    )
    group.add_argument(
        "--restore", metavar="ARCHIVE",
        help="Restore from ARCHIVE zip file"
    )
    parser.add_argument(
        "--project-dir", default=os.getcwd(),
        help="Directory containing your docker-compose.yml"
    )

    # Show help if no arguments provided
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()

    if args.init:
        do_init()
    elif args.backup:
        archive = backup(args.backup)
        print(f"✅ Backup written to: {archive}")
    elif args.restore:
        restore(args.restore, args.project_dir)
        print("✅ Restore finished.")
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
