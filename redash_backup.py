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
PG_CONTAINER   = "redash_postgres_1"
REDIS_CONTAINER = "redash_redis_1"
COMPOSE_FILE    = "/opt/redash/docker-compose.yml"
ENV_FILE        = "/opt/redash/.env"  # adjust if your env lives elsewhere

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
    if os.path.isfile(COMPOSE_FILE):
        shutil.copy(COMPOSE_FILE, workdir)
    else:
        logger.warning(f"{COMPOSE_FILE} not found, skipping")
    if os.path.isfile(ENV_FILE):
        shutil.copy(ENV_FILE, workdir)

    # 4) Zip it up
    archive = os.path.join(output_dir, f"redash-backup-{timestamp}.zip")
    logger.info(f"Creating archive {archive}")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(workdir):
            for fn in files:
                full = os.path.join(root, fn)
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
    # 1) Stop stack
    logger.info("Stopping existing Redash stack")
    run("docker-compose down")

    # 2) Restore Postgres
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

    # 3) Restore Redis
    redis_backup = os.path.join(temp, "redis-dump.rdb")
    if os.path.isfile(redis_backup):
        logger.info("Copying Redis dump back into container")
        run(f"docker cp {redis_backup} {REDIS_CONTAINER}:/data/dump.rdb")
    else:
        logger.warning("No redis-dump.rdb found, skipping")

    # 4) Restore compose/env (optional—only if you want to overwrite)
    if os.path.isfile(os.path.join(temp, os.path.basename(COMPOSE_FILE))):
        shutil.copy(
            os.path.join(temp, os.path.basename(COMPOSE_FILE)),
            COMPOSE_FILE
        )
    if os.path.isfile(os.path.join(temp, os.path.basename(ENV_FILE))):
        shutil.copy(
            os.path.join(temp, os.path.basename(ENV_FILE)),
            ENV_FILE
        )

    # 5) Start stack
    logger.info("Bringing Redash back up")
    run("docker-compose up -d")

    shutil.rmtree(temp)
    logger.info("Restore complete.")

def main():
    p = argparse.ArgumentParser(
        description="Redash full backup & restore"
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--backup",
        metavar="OUTDIR",
        help="Perform a backup to OUTDIR"
    )
    grp.add_argument(
        "--restore",
        metavar="ARCHIVE",
        help="Restore from ARCHIVE zip file"
    )
    p.add_argument(
        "--project-dir",
        default=os.getcwd(),
        help="Directory containing your docker-compose.yml"
    )
    args = p.parse_args()

    if args.backup:
        archive = backup(args.backup)
        print(f"✅ Backup written to: {archive}")
    elif args.restore:
        restore(args.restore, args.project_dir)
        print("✅ Restore finished.")

if __name__ == "__main__":
    main()
