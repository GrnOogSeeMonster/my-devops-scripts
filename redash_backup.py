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
import time

# Default container names
PG_CONTAINER = "redash_postgres_1"
REDIS_CONTAINER = "redash_redis_1"
COMPOSE_FILE = "/opt/redash/docker-compose.yml"
ENV_FILE = "/opt/redash/.env"

# Default directories for init
BACKUP_DIR = "/opt/backups"
LOG_DIR = "/var/log/redash"
STATE_FILE = os.path.expanduser("~/.redash_backup_initialized")

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


def wait_for_container(container_name, timeout=30):
    logger.info(f"Waiting for container '{container_name}' to be healthy...")
    for i in range(timeout):
        result = subprocess.run(
            f"docker inspect --format='{{{{.State.Health.Status}}}}' {container_name}",
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if result.stdout.strip() == b'healthy':
            logger.info(f"Container '{container_name}' is healthy.")
            return
        time.sleep(1)
    logger.warning(f"Timed out waiting for container '{container_name}' to be healthy.")


def do_init():
    actions = []
    user = os.environ.get('SUDO_USER') or os.environ.get('USER')

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            prev = f.read().strip()
        print(f"⚠️  Initialization already ran on {prev}")
    else:
        for d in [BACKUP_DIR, LOG_DIR]:
            if not os.path.isdir(d):
                try:
                    os.makedirs(d, exist_ok=True)
                    actions.append(f"Created directory: {d}")
                except PermissionError:
                    logger.error(f"Permission denied creating {d}. Please run as root.")
                    sys.exit(1)
            else:
                actions.append(f"Directory already exists: {d}")

        try:
            timestamp = datetime.utcnow().isoformat()
            with open(STATE_FILE, "w") as f:
                f.write(timestamp)
            actions.append(f"Recorded initialization at {timestamp}")
        except PermissionError:
            logger.error(f"Permission denied writing state file {STATE_FILE}.")
            sys.exit(1)

        docker_ok = subprocess.run(
            "docker ps", shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0

        if docker_ok:
            actions.append("Docker CLI access OK")
        else:
            actions.append("Docker CLI access FAILED")
            if os.geteuid() == 0:
                try:
                    run(f"usermod -aG docker {user}")
                    actions.append(f"Added '{user}' to docker group")
                except SystemExit:
                    actions.append(f"Failed to add '{user}' to docker group; manual intervention needed")
            else:
                cmd = f"sudo usermod -aG docker {user}"
                logger.warning(cmd)
                actions.append(cmd)

        if os.geteuid() == 0:
            try:
                run(f"chown {user}:docker {BACKUP_DIR} {LOG_DIR}")
                actions.append(f"Set ownership of {BACKUP_DIR} and {LOG_DIR} to {user}:docker")
            except SystemExit:
                actions.append("Failed to chown dirs; manual intervention needed")
        else:
            warning = f"sudo chown {user}:docker {BACKUP_DIR} {LOG_DIR}"
            logger.warning(warning)
            actions.append(warning)

        print("✅ Initialization complete. Actions:")
        for a in actions:
            print(f" - {a}")

    sys.exit(0)


def backup(output_dir):
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    workdir = os.path.join(output_dir, f"redash-backup-{timestamp}")
    os.makedirs(workdir, exist_ok=True)
    logger.info(f"Backing up into {workdir}")

    pg_dump_file = os.path.join(workdir, f"postgres-{timestamp}.sql")
    run(f"docker exec {PG_CONTAINER} pg_dumpall -U postgres > {pg_dump_file}")

    logger.info("Triggering Redis SAVE")
    run(f"docker exec {REDIS_CONTAINER} redis-cli SAVE")
    run(f"docker cp {REDIS_CONTAINER}:/data/dump.rdb {os.path.join(workdir, 'redis-dump.rdb')}")

    for fn in [COMPOSE_FILE, ENV_FILE]:
        if os.path.isfile(fn):
            shutil.copy(fn, workdir)
        else:
            logger.warning(f"{fn} not found, skipping")

    archive = os.path.join(output_dir, f"redash-backup-{timestamp}.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(workdir):
            for file in files:
                full = os.path.join(root, file)
                arcname = os.path.relpath(full, workdir)
                zf.write(full, arcname)

    shutil.rmtree(workdir)
    logger.info("Backup complete.")
    return archive


def restore(archive, project_dir):
    temp = tempfile.mkdtemp(prefix="redash-restore-")
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(temp)

    os.chdir(project_dir)

    logger.info("Stopping Redash stack (excluding DB and Redis)...")
    run("docker-compose stop")

    logger.info("Starting Postgres and Redis for restore...")
    run("docker-compose up -d postgres redis")
    time.sleep(5)
    wait_for_container(PG_CONTAINER)
    wait_for_container(REDIS_CONTAINER)

    sql_file = next((f for f in os.listdir(temp) if f.endswith(".sql")), None)
    if sql_file:
        sql_path = os.path.join(temp, sql_file)
        logger.info(f"Restoring Postgres from {sql_path}")
        with open(sql_path, "rb") as src:
            proc = subprocess.Popen(
                f"docker exec -i {PG_CONTAINER} psql -U postgres",
                shell=True, stdin=src
            )
            proc.wait()
            if proc.returncode != 0:
                logger.error("Postgres restore failed")
                sys.exit(proc.returncode)
    else:
        logger.error("No .sql file found in archive")
        sys.exit(1)

    redis_backup = os.path.join(temp, "redis-dump.rdb")
    if os.path.isfile(redis_backup):
        logger.info("Restoring Redis dump")
        run(f"docker cp {redis_backup} {REDIS_CONTAINER}:/data/dump.rdb")
    else:
        logger.warning("No redis-dump.rdb found, skipping")

    for fn in [COMPOSE_FILE, ENV_FILE]:
        src = os.path.join(temp, os.path.basename(fn))
        if os.path.isfile(src):
            shutil.copy(src, fn)

    logger.info("Starting full Redash stack")
    run("docker-compose up -d")
    shutil.rmtree(temp)
    logger.info("✅ Restore complete.")


def main():
    parser = argparse.ArgumentParser(description="Redash backup/restore tool")
    parser.add_argument("--init", action="store_true", help="Initialize environment")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--backup", metavar="OUTDIR", help="Backup Redash")
    group.add_argument("--restore", metavar="ARCHIVE", help="Restore Redash")
    parser.add_argument("--project-dir", default="/opt/redash", help="Project dir")

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
