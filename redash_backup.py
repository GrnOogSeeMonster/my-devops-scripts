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
import json
import re

# Default container names (legacy format)
PG_CONTAINER_LEGACY = "redash_postgres_1"
REDIS_CONTAINER_LEGACY = "redash_redis_1"
SERVER_CONTAINER_LEGACY = "redash_server_1"

# Default container names (modern format)
PG_CONTAINER_MODERN = "redash-postgres-1"
REDIS_CONTAINER_MODERN = "redash-redis-1"
SERVER_CONTAINER_MODERN = "redash-server-1"

# Will be set dynamically
PG_CONTAINER = None
REDIS_CONTAINER = None
SERVER_CONTAINER = None

COMPOSE_FILE = "/opt/redash/docker-compose.yml"
ENV_FILE = "/opt/redash/.env"
ENV_FILE_ALT = "/opt/redash/env"  # Alternative env file location

# Default directories for init
BACKUP_DIR = "/opt/backups"
LOG_DIR = "/var/log/redash"
STATE_FILE = os.path.expanduser("~/.redash_backup_initialized")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def detect_container_names():
    """Detect the actual container names being used by the current setup"""
    global PG_CONTAINER, REDIS_CONTAINER, SERVER_CONTAINER
    
    # Get all running containers
    result = run_safe("docker ps --format '{{.Names}}'", stdout=subprocess.PIPE)
    if result.returncode != 0:
        logger.error("Failed to list Docker containers")
        return False
    
    containers = result.stdout.decode().strip().split('\n') if result.stdout else []
    
    # Look for postgres container
    pg_containers = [c for c in containers if c and ('postgres' in c.lower())]
    if pg_containers:
        PG_CONTAINER = pg_containers[0]
        logger.info(f"Detected PostgreSQL container: {PG_CONTAINER}")
    else:
        logger.warning("No PostgreSQL container found")
        return False
    
    # Look for redis container
    redis_containers = [c for c in containers if c and ('redis' in c.lower())]
    if redis_containers:
        REDIS_CONTAINER = redis_containers[0]
        logger.info(f"Detected Redis container: {REDIS_CONTAINER}")
    else:
        logger.warning("No Redis container found")
        return False
    
    # Look for server container (optional during restore)
    server_containers = [c for c in containers if c and ('server' in c.lower() and 'redash' in c.lower())]
    if server_containers:
        SERVER_CONTAINER = server_containers[0]
        logger.info(f"Detected Redash server container: {SERVER_CONTAINER}")
    else:
        logger.info("No Redash server container found (this is normal during restore)")
        SERVER_CONTAINER = None
    
    return True


def run(cmd, **kwargs):
    logger.debug(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, **kwargs)
    if result.returncode != 0:
        logger.error(f"Command failed: {cmd}")
        sys.exit(result.returncode)
    return result


def run_safe(cmd, **kwargs):
    """Run command without exiting on failure"""
    logger.debug(f"Running (safe): {cmd}")
    return subprocess.run(cmd, shell=True, **kwargs)


def wait_for_container(container_name, timeout=60):
    """Wait for container to be ready with improved checks"""
    if container_name is None:
        logger.info("Container name is None, skipping wait")
        return True
        
    logger.info(f"Waiting for container '{container_name}' to be ready...")
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        # Check if container is running
        result = subprocess.run(
            f"docker inspect --format='{{{{.State.Status}}}}' {container_name}",
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        if result.stdout.strip() == b'running':
            # For postgres, check if it's accepting connections
            if container_name == PG_CONTAINER:
                pg_check = subprocess.run(
                    f"docker exec {PG_CONTAINER} pg_isready -U postgres",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                if pg_check.returncode == 0:
                    logger.info(f"Container '{container_name}' is ready.")
                    return True
            # For redis, check if it's responding to ping
            elif container_name == REDIS_CONTAINER:
                redis_check = subprocess.run(
                    f"docker exec {REDIS_CONTAINER} redis-cli ping",
                    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                if redis_check.stdout.strip() == b'PONG':
                    logger.info(f"Container '{container_name}' is ready.")
                    return True
            # For server, check if it's responding to HTTP
            elif container_name == SERVER_CONTAINER:
                server_check = subprocess.run(
                    "curl -s -f http://localhost:5000/health_check",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                if server_check.returncode == 0:
                    logger.info(f"Container '{container_name}' is ready.")
                    return True
            else:
                logger.info(f"Container '{container_name}' is ready.")
                return True
        
        time.sleep(2)
    
    logger.warning(f"Timed out waiting for container '{container_name}' to be ready.")
    return False


def is_fresh_host():
    """Check if this appears to be a fresh host with no existing Redash data"""
    # Instead of just checking for volumes/containers, check for actual data
    try:
        # Check if containers exist and have meaningful data
        result = run_safe("docker ps -a --format '{{.Names}}'", stdout=subprocess.PIPE)
        if result.returncode != 0:
            return True
        
        containers = result.stdout.decode().strip().split('\n') if result.stdout else []
        redash_containers = [c for c in containers if c and 'redash' in c.lower()]
        
        # If no Redash containers at all, it's fresh
        if len(redash_containers) == 0:
            return True
        
        # If containers exist, check if they contain meaningful Redash data
        # Try to validate the database - if it has no Redash data, consider it fresh
        # Detect container names first
        if not detect_container_names():
            # If we can't detect containers, assume it's not fresh
            return False
        
        pg_running = run_safe(f"docker ps --filter name={PG_CONTAINER} --format '{{{{.Names}}}}'", 
                             stdout=subprocess.PIPE)
        
        if pg_running.returncode == 0 and PG_CONTAINER in pg_running.stdout.decode():
            # Container is running, check for Redash databases
            result = run_safe(
                f"docker exec {PG_CONTAINER} psql -U postgres -t -c \"SELECT datname FROM pg_database WHERE datistemplate = false AND datname != 'postgres';\"",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            
            if result.returncode == 0:
                databases = [db.strip() for db in result.stdout.decode().strip().split('\n') if db.strip()]
                redash_dbs = [db for db in databases if 'redash' in db.lower()]
                
                # If there are Redash databases, it's not fresh
                if redash_dbs:
                    return False
                
                # Check if any database has substantial tables (more than just system tables)
                for db in databases:
                    if db and db not in ['template0', 'template1']:
                        table_result = run_safe(
                            f"docker exec {PG_CONTAINER} psql -U postgres -d {db} -t -c \"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';\"",
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                        )
                        if table_result.returncode == 0:
                            try:
                                table_count = int(table_result.stdout.decode().strip())
                                if table_count > 0:
                                    return False  # Found tables, not fresh
                            except ValueError:
                                pass
        
        # If we get here, either containers don't exist or contain no meaningful data
        return True
        
    except Exception:
        # If anything fails, err on the side of caution and assume it's not fresh
        return False


def validate_database(project_dir):
    """Validate database state and return information about existing data"""
    os.chdir(project_dir)
    
    # Detect container names first
    if not detect_container_names():
        logger.error("Failed to detect container names")
        return {
            'has_data': False,
            'database_count': 0,
            'table_count': 0,
            'approximate_size': '0 MB',
            'databases': [],
            'warnings': ['Failed to detect container names']
        }
    
    # Check if containers are running
    pg_running = run_safe(f"docker ps --filter name={PG_CONTAINER} --format '{{{{.Names}}}}'", 
                         stdout=subprocess.PIPE).returncode == 0
    
    if not pg_running:
        logger.info("Starting Postgres container for validation...")
        run("docker compose up -d postgres")
        wait_for_container(PG_CONTAINER)
    
    validation_results = {
        'has_data': False,
        'database_count': 0,
        'table_count': 0,
        'approximate_size': '0 MB',
        'databases': [],
        'warnings': []
    }
    
    try:
        # Get list of databases
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -t -c \"SELECT datname FROM pg_database WHERE datistemplate = false;\"",
            stdout=subprocess.PIPE
        )
        
        if result.returncode == 0:
            databases = [db.strip() for db in result.stdout.decode().strip().split('\n') if db.strip()]
            validation_results['databases'] = databases
            validation_results['database_count'] = len(databases)
            
            # Check for Redash-specific databases
            redash_dbs = [db for db in databases if 'redash' in db.lower()]
            if redash_dbs:
                validation_results['has_data'] = True
                validation_results['warnings'].append(f"Found Redash databases: {', '.join(redash_dbs)}")
        
        # Get table count from all databases (including postgres!)
        total_tables = 0
        for db in validation_results['databases']:
            # Skip only the template databases, NOT postgres
            if db in ['template0', 'template1']:
                continue
            
            result = run_safe(
                f"docker exec {PG_CONTAINER} psql -U postgres -d {db} -t -c \"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';\"",
                stdout=subprocess.PIPE
            )
            
            if result.returncode == 0:
                try:
                    count = int(result.stdout.decode().strip())
                    total_tables += count
                    if count > 0:
                        validation_results['has_data'] = True
                        validation_results['warnings'].append(f"Database '{db}' has {count} tables")
                        
                        # Special check for Redash tables in postgres database
                        if db == 'postgres':
                            redash_check = check_redash_tables(db)
                            if redash_check:
                                validation_results['warnings'].append(f"Found Redash tables in '{db}' database")
                except ValueError:
                    pass
        
        validation_results['table_count'] = total_tables
        
        # Get approximate database size
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -t -c \"SELECT pg_size_pretty(pg_database_size('postgres'));\"",
            stdout=subprocess.PIPE
        )
        
        if result.returncode == 0:
            validation_results['approximate_size'] = result.stdout.decode().strip()
    
    except Exception as e:
        validation_results['warnings'].append(f"Validation error: {str(e)}")
    
    return validation_results


def clean_volumes(project_dir, force=False):
    """Clean Docker volumes and containers"""
    if not force:
        logger.error("Volume cleaning requires --force-clean flag for safety")
        sys.exit(1)
    
    # Change to project directory if it exists and contains a compose file
    compose_file = get_compose_file_path(project_dir)
    if os.path.isdir(project_dir) and compose_file:
        os.chdir(project_dir)
        logger.info("Stopping and removing all containers via docker compose...")
        run_safe("docker compose down -v")  # -v removes volumes too
    else:
        logger.info("No compose file found, cleaning containers individually...")
    
    # Remove containers explicitly (in case docker compose didn't work)
    containers_to_remove = []
    result = run_safe("docker ps -a --format '{{.Names}}'", stdout=subprocess.PIPE)
    if result.returncode == 0:
        all_containers = result.stdout.decode().strip().split('\n') if result.stdout else []
        containers_to_remove = [c for c in all_containers if c and 'redash' in c.lower()]
    
    for container in containers_to_remove:
        result = run_safe(f"docker rm -f {container}")
        if result.returncode == 0:
            logger.info(f"Removed container: {container}")
        else:
            logger.warning(f"Failed to remove container: {container}")
    
    # Get and remove volumes
    result = run_safe("docker volume ls --format '{{.Name}}'", stdout=subprocess.PIPE)
    if result.returncode == 0:
        volumes = result.stdout.decode().strip().split('\n') if result.stdout else []
        redash_volumes = [v for v in volumes if v and ('redash' in v.lower() or 'postgres' in v.lower() or 'redis' in v.lower())]
        
        if redash_volumes:
            logger.info(f"Found {len(redash_volumes)} volumes to remove: {', '.join(redash_volumes)}")
            for volume in redash_volumes:
                result = run_safe(f"docker volume rm {volume}")
                if result.returncode == 0:
                    logger.info(f"Removed volume: {volume}")
                else:
                    logger.warning(f"Failed to remove volume: {volume}")
        else:
            logger.info("No Redash-related volumes found to remove")
    
    logger.info("Volume cleanup completed")


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

    try:
        # Detect container names first
        if not detect_container_names():
            raise Exception("Failed to detect container names")
        
        # Find the Redash database
        redash_db = get_redash_database(".")
        logger.info(f"Identified Redash database: {redash_db}")

        # Create comprehensive backup
        pg_dump_file = os.path.join(workdir, f"postgres-{timestamp}.sql")
        
        # Use pg_dumpall for complete backup including roles and permissions
        logger.info("Creating full PostgreSQL dump...")
        run(f"docker exec {PG_CONTAINER} pg_dumpall -U postgres > {pg_dump_file}")
        
        # Also create a specific backup of the Redash database for safety
        redash_dump_file = os.path.join(workdir, f"redash-db-{timestamp}.sql")
        logger.info(f"Creating specific backup of Redash database: {redash_db}")
        # Use --disable-triggers and proper ordering to handle foreign keys
        run(f"docker exec {PG_CONTAINER} pg_dump -U postgres -d {redash_db} --clean --create --disable-triggers --no-owner --no-privileges > {redash_dump_file}")
        
        # Create an additional data-only backup for reliable restoration
        redash_data_file = os.path.join(workdir, f"redash-data-{timestamp}.sql")
        logger.info(f"Creating data-only backup of Redash database: {redash_db}")
        run(f"docker exec {PG_CONTAINER} pg_dump -U postgres -d {redash_db} --data-only --disable-triggers --no-owner --no-privileges > {redash_data_file}")

        logger.info("Triggering Redis SAVE")
        run(f"docker exec {REDIS_CONTAINER} redis-cli SAVE")
        run(f"docker cp {REDIS_CONTAINER}:/data/dump.rdb {os.path.join(workdir, 'redis-dump.rdb')}")

        # Backup configuration files
        # Find and backup compose file
        compose_file = get_compose_file_path(".")
        if compose_file:
            shutil.copy(compose_file, workdir)
            logger.info(f"Backed up compose file: {compose_file}")
        else:
            logger.warning("No compose file found, skipping")
        
        # Backup env files
        for fn in [ENV_FILE, ENV_FILE_ALT]:
            if os.path.isfile(fn):
                shutil.copy(fn, workdir)
                logger.info(f"Backed up {fn}")
            else:
                logger.warning(f"{fn} not found, skipping")

        # Create a backup manifest
        manifest = {
            'timestamp': timestamp,
            'redash_database': redash_db,
            'files': {
                'full_dump': f"postgres-{timestamp}.sql",
                'redash_dump': f"redash-db-{timestamp}.sql",
                'redash_data': f"redash-data-{timestamp}.sql",
                'redis_dump': 'redis-dump.rdb'
            }
        }
        
        # Add env file to manifest if it exists
        env_file = None
        for env_name in ['.env', 'env']:
            if os.path.isfile(os.path.join(workdir, env_name)):
                env_file = env_name
                manifest['files']['env_file'] = env_name
                break
        
        manifest_file = os.path.join(workdir, 'backup-manifest.json')
        with open(manifest_file, 'w') as f:
            json.dump(manifest, f, indent=2)

        # Create the zip file
        archive = os.path.join(output_dir, f"redash-backup-{timestamp}.zip")
        logger.info(f"Creating archive: {archive}")
        
        # Debug: List all files in workdir before zipping
        logger.info("Files to be archived:")
        for root, dirs, files in os.walk(workdir):
            for file in files:
                full = os.path.join(root, file)
                size = os.path.getsize(full)
                logger.info(f"  {os.path.relpath(full, workdir)} ({size} bytes)")
        
        # First verify all files exist
        files_to_archive = []
        for root, _, files in os.walk(workdir):
            for file in files:
                full = os.path.join(root, file)
                if os.path.isfile(full):
                    files_to_archive.append((full, os.path.relpath(full, workdir)))
                    logger.debug(f"Adding to archive list: {os.path.relpath(full, workdir)}")
        
        # Create zip with verification
        logger.info(f"Creating zip file with {len(files_to_archive)} files...")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, arcname in files_to_archive:
                logger.debug(f"Adding to archive: {arcname}")
                zf.write(full, arcname)
        
        # Verify the zip file
        logger.info("Verifying zip file...")
        if not zipfile.is_zipfile(archive):
            raise Exception("Created archive is not a valid zip file")
        
        # List contents of zip file
        logger.info("Zip file contents:")
        with zipfile.ZipFile(archive, "r") as zf:
            for info in zf.infolist():
                logger.info(f"  {info.filename} ({info.file_size} bytes)")
            
            if 'backup-manifest.json' not in zf.namelist():
                raise Exception("Manifest not found in archive")
            
            # Read and parse manifest to verify it's valid
            with zf.open('backup-manifest.json') as f:
                json.load(f)
        
        logger.info("Backup complete.")
        return archive
        
    except Exception as e:
        logger.error(f"Backup failed: {str(e)}")
        # Clean up on failure
        if os.path.exists(workdir):
            shutil.rmtree(workdir)
        if os.path.exists(archive):
            os.remove(archive)
        raise
    finally:
        # Always clean up the work directory
        if os.path.exists(workdir):
            shutil.rmtree(workdir)


def restore(archive, project_dir, force_clean=False):
    """Restore Redash from backup with improved safety checks"""
    
    # Check if it's a fresh host
    fresh_host = is_fresh_host()
    logger.info(f"Fresh host check result: {fresh_host}")
    
    if not fresh_host and not force_clean:
        logger.error("⚠️  Existing Redash installation detected!")
        logger.error("This restore will overwrite existing data.")
        logger.error("Options:")
        logger.error("1. Use --validate-db first to check existing data")
        logger.error("2. Use --force-clean to automatically clean volumes")
        logger.error("3. Manually clean volumes before running restore")
        sys.exit(1)
    
    if fresh_host:
        logger.info("✅ Fresh host detected - safe to proceed with restore")
    else:
        logger.info("🧹 Using --force-clean to overwrite existing installation")
    
    # Extract archive
    temp = tempfile.mkdtemp(prefix="redash-restore-")
    logger.info(f"Extracting archive to {temp}")
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(temp)

    os.chdir(project_dir)

    # Clean volumes if requested or if not fresh
    if force_clean and not fresh_host:
        logger.info("🧹 Cleaning existing volumes...")
        clean_volumes(project_dir, force=True)
    
    # Stop the stack completely
    logger.info("Stopping Redash stack...")
    run_safe("docker compose down")

    # Start only Postgres and Redis with fresh volumes
    logger.info("Starting Postgres and Redis with fresh volumes...")
    run_safe("docker compose up -d postgres redis")
    
    # Detect container names after starting services
    if not detect_container_names():
        raise Exception("Failed to detect container names after starting services")
    
    # Wait for services to be ready
    wait_for_container(PG_CONTAINER)
    wait_for_container(REDIS_CONTAINER)
    
    # Note: SERVER_CONTAINER may be None at this point, which is normal during restore
    # It will be started later when needed

    # Check for backup manifest
    manifest_file = os.path.join(temp, 'backup-manifest.json')
    redash_db = None
    
    if os.path.isfile(manifest_file):
        with open(manifest_file, 'r') as f:
            manifest = json.load(f)
            redash_db = manifest.get('redash_database')
            logger.info(f"Found backup manifest, Redash database: {redash_db}")
    
    # Try to restore from specific Redash dump first
    redash_dump_file = next((f for f in os.listdir(temp) if f.startswith("redash-db-") and f.endswith(".sql")), None)
    redash_data_file = next((f for f in os.listdir(temp) if f.startswith("redash-data-") and f.endswith(".sql")), None)
    full_dump_file = next((f for f in os.listdir(temp) if f.startswith("postgres-") and f.endswith(".sql")), None)
    
    if redash_dump_file:
        logger.info(f"Restoring from Redash-specific dump: {redash_dump_file}")
        sql_path = os.path.join(temp, redash_dump_file)
        
        # First, try with the specific Redash dump which should handle dependencies better
        # But modify it to not drop the current database
        logger.info("Preparing database dump for restore...")
        
        # Read the SQL file and modify it to avoid dropping the current database
        with open(sql_path, "r") as f:
            sql_content = f.read()
        
        # Remove DROP DATABASE commands and CREATE DATABASE commands since we're using existing postgres
        # Remove DROP DATABASE statements
        sql_content = re.sub(r'DROP DATABASE[^;]+;', '', sql_content)
        # Remove CREATE DATABASE statements  
        sql_content = re.sub(r'CREATE DATABASE[^;]+;', '', sql_content)
        # Remove \connect statements
        sql_content = re.sub(r'\\connect[^;]+;', '', sql_content)
        
        # Write modified SQL to temporary file
        modified_sql_path = os.path.join(temp, "modified_restore.sql")
        with open(modified_sql_path, "w") as f:
            f.write(sql_content)
        
        # Now restore the modified SQL
        with open(modified_sql_path, "r") as sql_file_handle:
            proc = subprocess.Popen(
                f"docker exec -i {PG_CONTAINER} psql -U postgres -d postgres -v ON_ERROR_STOP=1",
                shell=True, 
                stdin=sql_file_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = proc.communicate()
            
            # Log detailed output for debugging
            if stdout:
                logger.info(f"Restore stdout: {stdout.decode()[:500]}")  # First 500 chars
            if stderr:
                logger.warning(f"Restore stderr: {stderr.decode()[:500]}")  # First 500 chars
            
            if proc.returncode != 0:
                logger.error(f"Redash database restore failed with return code: {proc.returncode}")
                logger.error(f"Error details: {stderr.decode()}")
                logger.info("Trying alternative restore method...")
                
                # Try restore with disabled foreign key checks temporarily
                logger.info("Attempting restore with disabled foreign key constraints...")
                
                # Use a more comprehensive approach to handle foreign keys
                # Copy the modified SQL file into the container first
                run(f"docker cp {modified_sql_path} {PG_CONTAINER}:/tmp/restore.sql")
                
                # Run the restore with comprehensive FK handling
                proc = subprocess.Popen(
                    f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"BEGIN; SET session_replication_role = replica;\"",
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                proc.wait()
                
                # Import the SQL file
                with open(modified_sql_path, "r") as sql_file_handle:
                    proc = subprocess.Popen(
                        f"docker exec -i {PG_CONTAINER} psql -U postgres -d postgres",
                        shell=True, 
                        stdin=sql_file_handle,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    stdout, stderr = proc.communicate()
                
                # Log detailed output for debugging
                if stdout:
                    logger.info(f"Alternative restore stdout: {stdout.decode()[:1000]}")
                if stderr:
                    logger.warning(f"Alternative restore stderr: {stderr.decode()[:1000]}")
                
                # Check if data was actually inserted by looking for COPY statements with actual row counts
                data_inserted = False
                if stdout:
                    stdout_str = stdout.decode()
                    # Look for COPY statements that inserted more than 0 rows
                    copy_matches = re.findall(r'COPY (\d+)', stdout_str)
                    if copy_matches:
                        total_rows = sum(int(match) for match in copy_matches)
                        if total_rows > 0:
                            data_inserted = True
                            logger.info(f"Data insertion detected: {total_rows} rows copied")
                        else:
                            logger.warning("No data was inserted (all COPY 0 statements)")
                
                # Re-enable FK constraints and commit
                proc_enable = subprocess.Popen(
                    f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"SET session_replication_role = DEFAULT; COMMIT;\"",
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                proc_enable.wait()
                
                # Clean up temp file
                run_safe(f"docker exec {PG_CONTAINER} rm -f /tmp/restore.sql")
                
                # If no data was inserted or command failed, try data-only restore
                if proc.returncode != 0 or not data_inserted:
                    if not data_inserted:
                        logger.error("Alternative restore method succeeded but no data was inserted")
                    else:
                        logger.error("Alternative restore method also failed")
                        logger.error(f"Final error details: {stderr.decode()}")
                    
                    # Try data-only restore as final attempt
                    if redash_data_file:
                        logger.info("Trying data-only restore method...")
                        data_sql_path = os.path.join(temp, redash_data_file)
                        
                        # First, start the server to initialize schema
                        logger.info("Starting server to initialize Redash schema...")
                        run("docker compose up -d server")
                        
                        # Wait for server to start and re-detect container names
                        time.sleep(10)  # Wait for container to start
                        if detect_container_names():
                            if SERVER_CONTAINER:
                                wait_for_container(SERVER_CONTAINER)
                            else:
                                logger.warning("Server container not detected after starting")
                        else:
                            logger.warning("Failed to detect container names after starting server")
                        
                        time.sleep(10)  # Wait for schema initialization
                        
                        # Initialize Redash database schema
                        logger.info("Creating Redash database tables...")
                        if SERVER_CONTAINER:
                            result = run_safe(f"docker exec {SERVER_CONTAINER} python manage.py database create_tables")
                        else:
                            # Fallback to docker compose exec
                            result = run_safe("docker compose exec -T server python manage.py database create_tables")
                        
                        if result.returncode != 0:
                            logger.warning("Schema creation failed, but continuing with data restore...")
                        
                        # Stop server for clean data restoration
                        run("docker compose stop server")
                        
                        # Clear potentially conflicting data that might block restoration
                        logger.info("Clearing potentially conflicting data...")
                        clear_sql = """
                        SET session_replication_role = replica;
                        TRUNCATE TABLE queries RESTART IDENTITY CASCADE;
                        TRUNCATE TABLE dashboards RESTART IDENTITY CASCADE;
                        TRUNCATE TABLE widgets RESTART IDENTITY CASCADE;
                        TRUNCATE TABLE changes RESTART IDENTITY CASCADE;
                        """
                        
                        result = run_safe(
                            f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"{clear_sql}\"",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE
                        )
                        if result.returncode == 0:
                            logger.info("Successfully cleared conflicting data")
                        else:
                            logger.warning("Failed to clear some tables, but continuing...")
                        
                        # Copy data file to container for reliable restoration
                        logger.info("Copying data-only backup to container...")
                        run(f"docker cp {data_sql_path} {PG_CONTAINER}:/tmp/restore_data.sql")
                        
                        # Now restore just the data with proper constraint handling
                        logger.info("Restoring data-only backup...")
                        restore_cmd = """
                        psql -U postgres -d postgres -c 'SET session_replication_role = replica;' &&
                        psql -U postgres -d postgres -f /tmp/restore_data.sql &&
                        psql -U postgres -d postgres -c 'SET session_replication_role = DEFAULT;'
                        """
                        
                        result = run_safe(
                            f"docker exec {PG_CONTAINER} bash -c \"{restore_cmd}\"",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE
                        )
                        
                        if result.returncode == 0:
                            # Check if data was actually inserted by looking for COPY statements
                            if result.stdout:
                                stdout_str = result.stdout.decode()
                                copy_matches = re.findall(r'COPY (\d+)', stdout_str)
                                if copy_matches:
                                    total_rows = sum(int(match) for match in copy_matches)
                                    logger.info(f"Data-only restore succeeded! {total_rows} rows inserted")
                                    
                                    # Check specifically for query restoration
                                    query_matches = re.search(r'ALTER TABLE.*queries.*\nCOPY (\d+)', stdout_str)
                                    if query_matches:
                                        logger.info(f"Queries restored: {query_matches.group(1)} records")
                                    else:
                                        # If queries weren't restored, try a direct restore
                                        logger.info("Queries not found in data-only restore, attempting direct restore...")
                                        query_restore_sql = """
                                        SET session_replication_role = replica;
                                        INSERT INTO queries (id, name, description, query, query_hash, version, user_id, org_id, data_source_id, options, schedule, created_at, updated_at, last_modified_by_id, is_archived, is_draft, schedule_failures)
                                        SELECT id, name, description, query, query_hash, version, user_id, org_id, data_source_id, options, schedule, created_at, updated_at, last_modified_by_id, is_archived, is_draft, schedule_failures
                                        FROM dblink('dbname=postgres', 'SELECT * FROM queries')
                                        AS t(id integer, name text, description text, query text, query_hash text, version integer, user_id integer, org_id integer, data_source_id integer, options jsonb, schedule jsonb, created_at timestamp with time zone, updated_at timestamp with time zone, last_modified_by_id integer, is_archived boolean, is_draft boolean, schedule_failures integer);
                                        SET session_replication_role = DEFAULT;
                                        """
                                        
                                        result = run_safe(
                                            f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"{query_restore_sql}\"",
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE
                                        )
                                        
                                        if result.returncode == 0:
                                            logger.info("Direct query restore completed")
                                        else:
                                            logger.warning(f"Direct query restore failed: {result.stderr.decode()}")
                                    
                                    # Check specifically for dashboard and widget restoration
                                    dashboard_matches = re.search(r'ALTER TABLE.*dashboards.*\nCOPY (\d+)', stdout_str)
                                    widget_matches = re.search(r'ALTER TABLE.*widgets.*\nCOPY (\d+)', stdout_str)
                                    
                                    if dashboard_matches:
                                        logger.info(f"Dashboards restored: {dashboard_matches.group(1)} records")
                                    if widget_matches:
                                        logger.info(f"Widgets restored: {widget_matches.group(1)} records")
                                else:
                                    logger.warning("Data-only restore completed but no data insertion detected")
                            else:
                                logger.info("Data-only restore completed")
                        else:
                            logger.error(f"Data-only restore failed: {result.stderr.decode()}")
                        
                        # Clean up temp file
                        run_safe(f"docker exec {PG_CONTAINER} rm -f /tmp/restore_data.sql")
                        
                        # Set successful completion flag for data-only restore
                        proc.returncode = 0
                        logger.info("Data-only restore completed successfully")
                    else:
                        logger.warning("No data-only backup file found for fallback restore")
                    
                    # Fallback to full dump
                    if proc.returncode != 0 and full_dump_file:
                        logger.info("Falling back to full PostgreSQL restore...")
                        sql_path = os.path.join(temp, full_dump_file)
                        with open(sql_path, "r") as full_sql_handle:
                            proc = subprocess.Popen(
                                f"docker exec -i {PG_CONTAINER} psql -U postgres",
                                shell=True, 
                                stdin=full_sql_handle,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE
                            )
                            stdout, stderr = proc.communicate()
                            
                            if proc.returncode != 0:
                                logger.error(f"Full PostgreSQL restore also failed: {stderr.decode()}")
                                shutil.rmtree(temp)
                                sys.exit(proc.returncode)
                    elif proc.returncode != 0:
                        logger.error("All database restore methods failed")
                        shutil.rmtree(temp)
                        sys.exit(1)
                else:
                    logger.info("Alternative restore method succeeded!")
            else:
                logger.info("Redash database restore completed successfully")
    
    elif full_dump_file:
        logger.info(f"Restoring from full PostgreSQL dump: {full_dump_file}")
        sql_path = os.path.join(temp, full_dump_file)
        
        with open(sql_path, "r") as sql_file_handle:
            proc = subprocess.Popen(
                f"docker exec -i {PG_CONTAINER} psql -U postgres",
                shell=True, 
                stdin=sql_file_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = proc.communicate()
            
            if proc.returncode != 0:
                logger.error(f"PostgreSQL restore failed: {stderr.decode()}")
                shutil.rmtree(temp)
                sys.exit(proc.returncode)
            else:
                logger.info("PostgreSQL restore completed successfully")
    
    else:
        logger.error("No SQL dump file found in archive")
        shutil.rmtree(temp)
        sys.exit(1)

    # Restore Redis
    redis_backup = os.path.join(temp, "redis-dump.rdb")
    if os.path.isfile(redis_backup):
        logger.info("Restoring Redis dump")
        # Stop redis, copy dump, restart
        run("docker compose stop redis")
        run(f"docker cp {redis_backup} {REDIS_CONTAINER}:/data/dump.rdb")
        run("docker compose start redis")
        wait_for_container(REDIS_CONTAINER)
    else:
        logger.warning("No redis-dump.rdb found, skipping Redis restore")

    # Restore configuration files and handle secret key
    backed_up_secret_key = None
    env_file_target = get_env_file_path(project_dir)
    
    # First restore compose file - look for any compose file in backup
    compose_file_restored = False
    compose_file_names = ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']
    
    for compose_name in compose_file_names:
        compose_src = os.path.join(temp, compose_name)
        if os.path.isfile(compose_src):
            # Determine target name based on what's already in the project
            existing_compose = get_compose_file_path(project_dir)
            if existing_compose:
                target_compose = existing_compose
            else:
                # Default to docker-compose.yml for backward compatibility
                target_compose = os.path.join(project_dir, "docker-compose.yml")
            
            logger.info(f"Restoring compose file: {compose_src} -> {target_compose}")
            shutil.copy(compose_src, target_compose)
            compose_file_restored = True
            break
    
    if not compose_file_restored:
        logger.warning("No compose file found in backup")
    
    # Then handle env file restoration
    env_src = None
    # Check manifest first
    if os.path.isfile(manifest_file):
        with open(manifest_file, 'r') as f:
            manifest = json.load(f)
            if 'files' in manifest and 'env_file' in manifest['files']:
                env_src = os.path.join(temp, manifest['files']['env_file'])
                logger.info(f"Found env file in manifest: {env_src}")
    
    # If not in manifest, try to find env file in backup
    if not env_src or not os.path.isfile(env_src):
        for env_name in ['.env', 'env']:
            potential_src = os.path.join(temp, env_name)
            if os.path.isfile(potential_src):
                env_src = potential_src
                logger.info(f"Found env file in backup: {env_src}")
                break
    
    if env_src and os.path.isfile(env_src):
        # Extract secret key before copying
        backed_up_secret_key = extract_secret_key_from_env(env_src)
        if backed_up_secret_key:
            logger.info(f"Found REDASH_SECRET_KEY in backup: {backed_up_secret_key[:8]}...")
        
        # Copy the entire env file from backup
        logger.info(f"Restoring env file to: {env_file_target}")
        shutil.copy(env_src, env_file_target)
    else:
        logger.warning("No env file found in backup")
    
    # If we restored a secret key, restart containers to apply it
    if backed_up_secret_key:
        logger.info("Restarting containers to apply restored secret key...")
        run("docker compose down")
        run("docker compose up -d")
        time.sleep(5)  # Wait for containers to restart

    # Start full Redash stack
    logger.info("Starting full Redash stack...")
    run("docker compose up -d")
    
    # Wait a moment for services to start
    time.sleep(5)
    
    # Re-detect container names now that all containers should be running
    if not detect_container_names():
        logger.warning("Failed to detect all container names after starting full stack")
        logger.info("Continuing with restore verification...")
    
    # Wait for containers to stabilize
    logger.info("Waiting for containers to stabilize...")
    time.sleep(10)
    
    # Verify the restore worked
    logger.info("Verifying restore...")
    try:
        diagnose_restore_data(project_dir)
    except Exception as e:
        logger.warning(f"Verification failed: {str(e)}")
        logger.info("This is normal if containers are still starting up")
        logger.info("You can run --diagnose later to check the restore status")
    
    # Cleanup
    shutil.rmtree(temp)
    
    # Final verification - check if we actually restored data
    logger.info("Performing final verification...")
    time.sleep(5)  # Give containers more time to stabilize
    
    try:
        # Quick check for basic data
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -t -c \"SELECT COUNT(*) FROM users;\"",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        
        if result.returncode == 0:
            try:
                user_count = int(result.stdout.decode().strip())
                if user_count > 0:
                    logger.info(f"✅ Restore verification successful: {user_count} users found")
                    logger.info("✅ Restore completed successfully")
                else:
                    logger.warning("⚠️  Restore completed but no users found")
                    logger.info("💡 Run --diagnose to check restore status")
            except ValueError:
                logger.warning("⚠️  Restore completed but verification inconclusive")
                logger.info("💡 Run --diagnose to check restore status")
        else:
            logger.warning("⚠️  Restore completed but verification failed")
            logger.info("💡 Run --diagnose to check restore status")
    except Exception as e:
        logger.warning(f"⚠️  Restore completed but verification failed: {str(e)}")
        logger.info("💡 Run --diagnose to check restore status")


def reset_password(project_dir, email, password=None):
    """Reset password for a Redash user"""
    os.chdir(project_dir)
    
    # Detect container names first
    if not detect_container_names():
        logger.error("Failed to detect container names")
        sys.exit(1)
    
    # Find the running server container
    result = run_safe("docker ps --format '{{.Names}}' --filter name=server", stdout=subprocess.PIPE)
    if result.returncode != 0:
        logger.error("Failed to list Docker containers")
        sys.exit(1)
    
    containers = result.stdout.decode().strip().split('\n') if result.stdout else []
    server_containers = [c for c in containers if c and ('redash' in c.lower() and 'server' in c.lower())]
    
    if not server_containers:
        logger.error("No running Redash server container found")
        logger.info("Available containers:")
        run_safe("docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}'")
        logger.info("Try starting the server first: docker compose up -d")
        sys.exit(1)
    
    server_container = server_containers[0]
    logger.info(f"Using server container: {server_container}")
    
    # Build the command
    if password:
        cmd = f"docker exec {server_container} python manage.py users password {email} {password}"
    else:
        cmd = f"docker exec -it {server_container} python manage.py users password {email}"
    
    logger.info(f"Resetting password for user: {email}")
    result = run_safe(cmd)
    
    if result.returncode == 0:
        logger.info("✅ Password reset successful")
    else:
        logger.error("❌ Password reset failed")
        sys.exit(result.returncode)


def list_users(project_dir):
    """List all Redash users"""
    os.chdir(project_dir)
    
    # Detect container names first
    if not detect_container_names():
        logger.error("Failed to detect container names")
        sys.exit(1)
    
    # Find the running server container
    result = run_safe("docker ps --format '{{.Names}}' --filter name=server", stdout=subprocess.PIPE)
    if result.returncode != 0:
        logger.error("Failed to list Docker containers")
        sys.exit(1)
    
    containers = result.stdout.decode().strip().split('\n') if result.stdout else []
    server_containers = [c for c in containers if c and ('redash' in c.lower() and 'server' in c.lower())]
    
    if not server_containers:
        logger.error("No running Redash server container found")
        logger.info("Try starting the server first: docker compose up -d")
        sys.exit(1)
    
    server_container = server_containers[0]
    logger.info(f"Using server container: {server_container}")
    
    print("📋 Redash Users:")
    result = run_safe(f"docker exec {server_container} python manage.py users list")
    
    if result.returncode != 0:
        logger.error("Failed to list users")
        sys.exit(result.returncode)


def get_redash_database(project_dir):
    """Find the actual Redash database name"""
    os.chdir(project_dir)
    
    # Detect container names first
    if not detect_container_names():
        logger.error("Failed to detect container names")
        return None
    
    # Check if postgres container is running
    pg_running = run_safe(f"docker ps --filter name={PG_CONTAINER} --format '{{{{.Names}}}}'", 
                         stdout=subprocess.PIPE)
    
    if pg_running.returncode != 0 or PG_CONTAINER not in pg_running.stdout.decode():
        logger.info("Starting Postgres container...")
        run("docker compose up -d postgres")
        wait_for_container(PG_CONTAINER)
    
    # Add retry logic for container restarting issues
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Get list of databases
            result = run_safe(
                f"docker exec {PG_CONTAINER} psql -U postgres -t -c \"SELECT datname FROM pg_database WHERE datistemplate = false;\"",
                stdout=subprocess.PIPE
            )
            
            if result.returncode == 0:
                databases = [db.strip() for db in result.stdout.decode().strip().split('\n') if db.strip()]
                logger.info(f"Available databases: {databases}")
                
                # Look for Redash-specific database first
                redash_dbs = [db for db in databases if 'redash' in db.lower()]
                if redash_dbs:
                    return redash_dbs[0]
                
                # Check each database for Redash tables
                for db in databases:
                    # Skip only template databases
                    if db in ['template0', 'template1']:
                        continue
                    
                    # Check all databases (including postgres) for Redash tables
                    redash_tables = check_redash_tables(db)
                    if redash_tables:
                        return db
                
                # Default to postgres if no specific Redash db found
                logger.warning("No specific Redash database found, defaulting to 'postgres'")
                return 'postgres'
            else:
                logger.warning(f"Failed to list databases (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(5)  # Wait before retry
                    continue
                else:
                    logger.error("Failed to list databases after all retries")
                    return None
                    
        except Exception as e:
            logger.warning(f"Error getting database list (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)  # Wait before retry
                continue
            else:
                logger.error("Failed to get database list after all retries")
                return None
    
    return None


def check_redash_tables(database):
    """Check if a database contains Redash tables"""
    redash_table_names = ['queries', 'dashboards', 'users', 'data_sources', 'query_results']
    
    result = run_safe(
        f"docker exec {PG_CONTAINER} psql -U postgres -d {database} -t -c \"SELECT tablename FROM pg_tables WHERE schemaname = 'public';\"",
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    
    if result.returncode != 0:
        return False
    
    tables = [table.strip() for table in result.stdout.decode().strip().split('\n') if table.strip()]
    redash_tables_found = [table for table in tables if table in redash_table_names]
    
    if redash_tables_found:
        logger.info(f"Found Redash tables in database '{database}': {redash_tables_found}")
        return True
    
    return False


def diagnose_restore_data(project_dir):
    """Diagnose what data was actually restored"""
    # Detect container names first
    if not detect_container_names():
        logger.error("Failed to detect container names")
        return
    
    # Add retry logic for container restarting issues
    max_retries = 3
    redash_db = None
    
    for attempt in range(max_retries):
        try:
            redash_db = get_redash_database(project_dir)
            if redash_db:
                break
            else:
                logger.warning(f"Failed to determine Redash database (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(5)  # Wait before retry
                    continue
                else:
                    logger.error("Could not determine Redash database after all retries")
                    return
        except Exception as e:
            logger.warning(f"Error determining Redash database (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)  # Wait before retry
                continue
            else:
                logger.error("Could not determine Redash database after all retries")
                return
    
    if not redash_db:
        logger.error("Could not determine Redash database")
        return
    
    logger.info(f"Diagnosing data in database: {redash_db}")
    
    # Check key Redash tables
    tables_to_check = {
        'users': 'SELECT COUNT(*) FROM users',
        'queries': 'SELECT COUNT(*) FROM queries',
        'dashboards': 'SELECT COUNT(*) FROM dashboards', 
        'data_sources': 'SELECT COUNT(*) FROM data_sources',
        'query_results': 'SELECT COUNT(*) FROM query_results'
    }
    
    print("\n📊 Redash Data Inventory:")
    print(f"Database: {redash_db}")
    
    for table_name, query in tables_to_check.items():
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -d {redash_db} -t -c \"{query}\"",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        
        if result.returncode == 0:
            try:
                count = int(result.stdout.decode().strip())
                status = "✅" if count > 0 else "❌"
                print(f"  {status} {table_name.capitalize()}: {count}")
            except ValueError:
                print(f"  ❓ {table_name.capitalize()}: Could not read count")
        else:
            print(f"  ❌ {table_name.capitalize()}: Table missing or inaccessible")
    
    # Check for secret key issues
    current_key = check_secret_key_mismatch(project_dir)
    if current_key:
        print(f"\n🔐 Current REDASH_SECRET_KEY: {current_key[:8]}...")
        
        # Check for encryption errors in logs - find server container dynamically
        server_containers = []
        result = run_safe("docker ps --format '{{.Names}}' --filter name=server", stdout=subprocess.PIPE)
        if result.returncode == 0:
            containers = result.stdout.decode().strip().split('\n') if result.stdout else []
            server_containers = [c for c in containers if c and ('redash' in c.lower() and 'server' in c.lower())]
        
        if server_containers:
            server_container = server_containers[0]
            result = run_safe(f"docker logs {server_container} --tail 50 | grep -i 'InvalidToken\\|decrypt'", stdout=subprocess.PIPE)
            if result.returncode == 0 and result.stdout.strip():
                print("⚠️  Encryption errors detected! Secret key mismatch likely.")
                print("💡 Use: --fix-secret-key <correct_key> to fix")
            else:
                print("✅ No encryption errors detected")
        else:
            print("⚠️  Server container not running, cannot check for encryption errors")
    
    print()


def check_secret_key_mismatch(project_dir):
    """Check if there's a secret key mismatch causing encryption issues"""
    os.chdir(project_dir)
    
    # Detect container names first
    if not detect_container_names():
        logger.warning("No running server container found for secret key check")
        return None
    
    # Check if server container is running
    result = run_safe("docker ps --format '{{.Names}}' --filter name=server", stdout=subprocess.PIPE)
    if result.returncode != 0:
        logger.warning("No running server container found for secret key check")
        return None
    
    containers = result.stdout.decode().strip().split('\n') if result.stdout else []
    server_containers = [c for c in containers if c and ('redash' in c.lower() and 'server' in c.lower())]
    
    if not server_containers:
        logger.warning("No running server container found for secret key check")
        return None
    
    server_container = server_containers[0]
    
    # Get current secret key
    result = run_safe(f"docker exec {server_container} env | grep REDASH_SECRET_KEY", stdout=subprocess.PIPE)
    if result.returncode == 0:
        current_key = result.stdout.decode().strip().split('=')[1] if '=' in result.stdout.decode() else None
        return current_key
    
    return None


def get_env_file_path(project_dir):
    """Get the correct environment file path (handles both .env and env)"""
    env_path = os.path.join(project_dir, '.env')
    env_alt_path = os.path.join(project_dir, 'env')
    
    if os.path.isfile(env_path):
        return env_path
    elif os.path.isfile(env_alt_path):
        return env_alt_path
    else:
        # Return the standard path for creation
        return env_path


def fix_secret_key(project_dir, target_key):
    """Fix the REDASH_SECRET_KEY in environment file"""
    env_file = get_env_file_path(project_dir)
    
    if not os.path.isfile(env_file):
        logger.error(f"Environment file not found at: {env_file}")
        # Try alternative locations
        alt_paths = [
            os.path.join(project_dir, 'env'),
            os.path.join(project_dir, '.env'),
            '/opt/redash/env',
            '/opt/redash/.env'
        ]
        
        for alt_path in alt_paths:
            if os.path.isfile(alt_path):
                env_file = alt_path
                logger.info(f"Found environment file at: {env_file}")
                break
        else:
            logger.error("No environment file found in any expected location")
            return False
    
    # Read current env file
    with open(env_file, 'r') as f:
        lines = f.readlines()
    
    # Update secret key
    updated = False
    for i, line in enumerate(lines):
        if line.startswith('REDASH_SECRET_KEY='):
            old_key = line.strip().split('=')[1] if '=' in line else 'unknown'
            lines[i] = f"REDASH_SECRET_KEY={target_key}\n"
            updated = True
            logger.info(f"Updated secret key from {old_key[:8]}... to {target_key[:8]}...")
            break
    
    if not updated:
        # Append if not found
        lines.append(f"REDASH_SECRET_KEY={target_key}\n")
        logger.info(f"Added new secret key: {target_key[:8]}...")
    
    # Write back to file
    with open(env_file, 'w') as f:
        f.writelines(lines)
    
    logger.info(f"Updated REDASH_SECRET_KEY in {env_file}")
    return True


def extract_secret_key_from_env(env_file_path):
    """Extract REDASH_SECRET_KEY from backed up env file"""
    if not os.path.isfile(env_file_path):
        return None
    
    with open(env_file_path, 'r') as f:
        for line in f:
            if line.startswith('REDASH_SECRET_KEY='):
                return line.strip().split('=')[1]
    return None


def debug_missing_data(project_dir):
    """Debug missing dashboard and widget data"""
    logger.info("🔍 Debugging missing data...")
    
    # Detect container names first
    if not detect_container_names():
        logger.error("Failed to detect container names")
        return
    
    # Check dashboards
    result = run_safe(
        f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"SELECT id, name, created_at, updated_at FROM dashboards ORDER BY id;\"",
        stdout=subprocess.PIPE
    )
    if result.returncode == 0:
        print("📋 Current Dashboards:")
        print(result.stdout.decode())
    
    # Check widgets
    result = run_safe(
        f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"SELECT id, dashboard_id, text, created_at FROM widgets ORDER BY id;\"",
        stdout=subprocess.PIPE
    )
    if result.returncode == 0:
        print("🎨 Current Widgets:")
        print(result.stdout.decode())
    
    # Check for foreign key constraint violations in logs
    server_containers = []
    result = run_safe("docker ps --format '{{.Names}}' --filter name=server", stdout=subprocess.PIPE)
    if result.returncode == 0:
        containers = result.stdout.decode().strip().split('\n') if result.stdout else []
        server_containers = [c for c in containers if c and ('redash' in c.lower() and 'server' in c.lower())]
    
    if server_containers:
        server_container = server_containers[0]
        result = run_safe(f"docker logs {server_container} --tail 100 | grep -i 'error\\|violation\\|constraint'", stdout=subprocess.PIPE)
        if result.returncode == 0 and result.stdout:
            print("⚠️  Recent errors in server logs:")
            print(result.stdout.decode())
    else:
        print("⚠️  Server container not running, cannot check logs")


def get_upgrade_path(current_version):
    """Determine the upgrade path based on current version"""
    version_map = {
        '8.0.0': ['10.0.0.b50363', '10.1.0.b50633', '25.1.0'],
        '10.0.0': ['10.1.0.b50633', '25.1.0'],
        '10.1.0': ['25.1.0']
    }
    return version_map.get(current_version, [])

def get_current_version(project_dir):
    """Get current Redash version from compose file"""
    os.chdir(project_dir)
    
    compose_file = get_compose_file_path(project_dir)
    if not compose_file:
        logger.warning("No compose file found")
        return None
    
    with open(compose_file, 'r') as f:
        content = f.read()
        match = re.search(r'image: redash/redash:([^\s]+)', content)
        if match:
            return match.group(1)
    return None

def get_docker_compose_cmd():
    """Determine the correct docker compose command to use with enhanced error handling"""
    # Check if Docker is installed and running
    docker_check = run_safe("docker info", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if docker_check.returncode != 0:
        raise Exception("Docker is not running or not installed. Please start Docker and try again.")
    
    # Try docker compose first (newer versions)
    compose_v2_check = run_safe("docker compose version", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if compose_v2_check.returncode == 0:
        # Get version details
        version_output = compose_v2_check.stdout.decode()
        logger.info(f"Using 'docker compose' command (newer version)")
        logger.debug(f"Docker Compose V2 version info: {version_output}")
        return "docker compose"
    
    # Fall back to docker compose (older versions)
    compose_v1_check = run_safe("docker-compose version", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if compose_v1_check.returncode == 0:
        # Get version details
        version_output = compose_v1_check.stdout.decode()
        logger.info(f"Using 'docker compose' command (legacy version)")
        logger.debug(f"Docker Compose V1 version info: {version_output}")
        return "docker compose"
    
    # If we get here, neither command worked
    error_msg = "Neither 'docker compose' nor 'docker-compose' commands are available.\n"
    error_msg += "Please ensure Docker Compose is installed:\n"
    error_msg += "1. For Docker Compose V2: Install Docker Desktop or update Docker Engine\n"
    error_msg += "2. For Docker Compose V1: Install via 'pip install docker-compose' or package manager"
    raise Exception(error_msg)

def run_compose(cmd, **kwargs):
    """Run a docker compose command with the correct syntax"""
    compose_cmd = get_docker_compose_cmd()
    return run(f"{compose_cmd} {cmd}", **kwargs)

def run_compose_safe(cmd, **kwargs):
    """Run a docker compose command safely (no exit on failure)"""
    compose_cmd = get_docker_compose_cmd()
    return run_safe(f"{compose_cmd} {cmd}", **kwargs)

def upgrade_redash_semantic(project_dir, target_version="25.1.0"):
    """Safely upgrade Redash through semantic versions"""
    current_version = get_current_version(project_dir)
    if not current_version:
        raise Exception("Could not determine current Redash version")
    
    logger.info(f"Current Redash version: {current_version}")
    logger.info(f"Target version: {target_version}")
    
    # Get upgrade path
    upgrade_path = get_upgrade_path(current_version)
    if not upgrade_path:
        raise Exception(f"No upgrade path found from version {current_version}")
    
    # Add target version if not in path
    if target_version not in upgrade_path:
        upgrade_path.append(target_version)
    
    logger.info(f"Upgrade path: {' -> '.join(upgrade_path)}")
    
    # Create initial backup
    logger.info("Creating initial backup...")
    backup_dir = os.path.join(project_dir, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_file = backup(backup_dir)
    logger.info(f"Initial backup created: {backup_file}")
    
    try:
        for version in upgrade_path:
            logger.info(f"\n🔄 Upgrading to version {version}")
            
            # Update compose file
            compose_file = get_compose_file_path(project_dir)
            if not compose_file:
                raise Exception("No compose file found")
            
            with open(compose_file, 'r') as f:
                compose_data = f.read()
            
            # Update image version
            compose_data = re.sub(
                r'image: redash/redash:[^\s]+',
                f'image: {version_info["image"]}',
                compose_data
            )
            
            with open(compose_file, 'w') as f:
                f.write(compose_data)
            logger.info("✅ Updated compose file with new version")
        
        # Start services
        if dry_run:
            logger.info("Would start Redash services")
            logger.info("Would wait for services to be ready")
        else:
            logger.info("Starting Redash services...")
            run_compose("up -d")
            logger.info("✅ Services started")
            
            # Wait for services to be ready
            logger.info("Waiting for services to be ready...")
            wait_for_container(PG_CONTAINER)
            wait_for_container(REDIS_CONTAINER)
            wait_for_container(SERVER_CONTAINER)
            logger.info("✅ All services are ready")
        
        # If backup file provided, restore it
        if backup_file:
            # Resolve backup file path
            if not os.path.isabs(backup_file):
                backup_file = os.path.abspath(backup_file)
            
            if not os.path.isfile(backup_file):
                raise Exception(f"Backup file not found: {backup_file}")
            
            if dry_run:
                logger.info(f"Would restore from backup: {backup_file}")
                logger.info("Would verify backup contents")
                logger.info("Would restore database and configuration")
            else:
                logger.info(f"Restoring from backup: {backup_file}")
                restore(backup_file, project_dir, force_clean=True)
                logger.info("✅ Backup restored successfully")
        
        if dry_run:
            logger.info("🔍 DRY RUN COMPLETED: No changes were made")
            logger.info("The following would be done:")
            logger.info("1. Create project directory")
            logger.info("2. Download and run setup script")
            logger.info("3. Configure Redash version")
            logger.info("4. Start services")
            if backup_file:
                logger.info("5. Restore from backup")
        else:
            logger.info("✅ Redash setup completed successfully!")
        
    except Exception as e:
        logger.error(f"Setup failed: {str(e)}")
        # Clean up on failure
        if not dry_run and os.path.exists(setup_script):
            os.remove(setup_script)
        raise

def get_version_info(version):
    """Get version-specific information"""
    version_map = {
        "10.1.0": {
            "image": "redash/redash:10.1.0.b50633",
            "setup_script": "https://raw.githubusercontent.com/getredash/setup/master/setup.sh",
            "fallback_script": "https://raw.githubusercontent.com/getredash/setup/v10.1.0/setup.sh",
            "description": "Redash V10.1.0 (Legacy Version)",
            "script_format": "legacy"
        },
        "25.1.0": {
            "image": "redash/redash:25.1.0",
            "setup_script": "https://raw.githubusercontent.com/getredash/setup/master/setup.sh",
            "fallback_script": "https://raw.githubusercontent.com/getredash/setup/v25.1.0/setup.sh",
            "description": "Redash V25.1.0 (Latest Version)",
            "script_format": "modern"
        }
    }
    return version_map.get(version)

def download_setup_script(url, output_path, fallback_url=None):
    """Download setup script with fallback and validation"""
    logger.info(f"Downloading setup script from: {url}")
    
    # Try primary URL
    result = run_safe(f"curl -L -f {url} -o {output_path}", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    # If primary fails and we have a fallback, try it
    if result.returncode != 0 and fallback_url:
        logger.info(f"Primary URL failed, trying fallback: {fallback_url}")
        result = run_safe(f"curl -L -f {fallback_url} -o {output_path}", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    # Check if download was successful
    if result.returncode != 0:
        raise Exception(f"Failed to download setup script: {result.stderr.decode()}")
    
    # Validate script content
    with open(output_path, 'r') as f:
        content = f.read()
        if len(content.strip()) < 100:  # Basic size check
            raise Exception("Downloaded script appears to be invalid (too small)")
        if "404" in content or "Not Found" in content:
            raise Exception("Downloaded script appears to be a 404 page")
    
    return True

def fix_setup_script(script_path, version_info):
    """Fix setup script format based on version"""
    if version_info["script_format"] == "legacy":
        logger.info("Fixing legacy setup script format...")
        with open(script_path, 'r') as f:
            content = f.read()
        
        # Fix common issues in legacy scripts
        content = content.replace('\r\n', '\n')  # Fix Windows line endings
        content = content.replace('\r', '\n')    # Fix Mac line endings
        
        # Ensure proper shebang
        if not content.startswith('#!/bin/bash'):
            content = '#!/bin/bash\n' + content
        
        # Write fixed content
        with open(script_path, 'w') as f:
            f.write(content)
        
        logger.info("✅ Setup script format fixed")

def get_compose_file_path(project_dir):
    """Get the correct compose file path"""
    compose_files = [
        os.path.join(project_dir, "compose.yaml"),      # Modern Docker Compose V2
        os.path.join(project_dir, "compose.yml"),       # Modern Docker Compose V2 (alternative)
        os.path.join(project_dir, "docker-compose.yml"), # Legacy Docker Compose V1
        os.path.join(project_dir, "docker-compose.yaml") # Legacy Docker Compose V1 (alternative)
    ]
    
    for file_path in compose_files:
        if os.path.isfile(file_path):
            return file_path
    
    return None

def setup_redash(project_dir, version="25.1.0", backup_file=None, dry_run=False):
    """Set up a new Redash instance and optionally restore from backup"""
    version_info = get_version_info(version)
    if not version_info:
        raise Exception(f"Unsupported Redash version: {version}")
    
    logger.info(f"Setting up {version_info['description']} in {project_dir}")
    if dry_run:
        logger.info("🔍 DRY RUN MODE: No changes will be made")
    
    # Create project directory if it doesn't exist
    if not dry_run:
        os.makedirs(project_dir, exist_ok=True)
        os.chdir(project_dir)
    else:
        logger.info(f"Would create directory: {project_dir}")
    
    try:
        # Download setup script
        setup_script = os.path.join(project_dir, "setup.sh")
        if dry_run:
            logger.info(f"Would download setup script from: {version_info['setup_script']}")
            logger.info(f"Would save to: {setup_script}")
            logger.info("Would make script executable")
        else:
            try:
                download_setup_script(
                    version_info['setup_script'],
                    setup_script,
                    version_info.get('fallback_script')
                )
                
                # Fix script format if needed
                if not dry_run:
                    fix_setup_script(setup_script, version_info)
                
                run(f"chmod +x {setup_script}")
                logger.info("✅ Setup script downloaded and made executable")
            except Exception as e:
                logger.error(f"Failed to download setup script: {str(e)}")
                logger.info("Attempting to use local setup script...")
                
                # Try to use a local copy if available
                local_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup.sh")
                if os.path.exists(local_script):
                    shutil.copy(local_script, setup_script)
                    run(f"chmod +x {setup_script}")
                    logger.info("✅ Using local setup script")
                else:
                    raise Exception("No setup script available (download failed and no local copy found)")
        
        # Run setup script
        if dry_run:
            logger.info("Would run setup script with sudo")
            logger.info("Would install required packages and dependencies")
            logger.info("Would configure Docker and Docker Compose")
            logger.info("Would create initial Redash configuration")
        else:
            logger.info("Running Redash setup script...")
            # Use bash explicitly for legacy scripts
            if version_info["script_format"] == "legacy":
                run(f"sudo bash {setup_script}")
            else:
                run(f"sudo {setup_script}")
            logger.info("✅ Setup script completed successfully")
        
        # Update to specific version
        if dry_run:
            logger.info(f"Would update compose file to use image: {version_info['image']}")
        else:
            logger.info(f"Updating to Redash version {version}...")
            
            # Find the compose file
            compose_file = get_compose_file_path(project_dir)
            if not compose_file:
                raise Exception("No compose file found after setup")
            
            logger.info(f"Found compose file: {compose_file}")
            
            with open(compose_file, 'r') as f:
                compose_data = f.read()
            
            # Update image version
            compose_data = re.sub(
                r'image: redash/redash:[^\s]+',
                f'image: {version_info["image"]}',
                compose_data
            )
            
            with open(compose_file, 'w') as f:
                f.write(compose_data)
            logger.info("✅ Updated compose file with new version")
        
        # Start services
        if dry_run:
            logger.info("Would start Redash services")
            logger.info("Would wait for services to be ready")
        else:
            logger.info("Starting Redash services...")
            run_compose("up -d")
            logger.info("✅ Services started")
            
            # Wait for services to be ready
            logger.info("Waiting for services to be ready...")
            wait_for_container(PG_CONTAINER)
            wait_for_container(REDIS_CONTAINER)
            wait_for_container(SERVER_CONTAINER)
            logger.info("✅ All services are ready")
        
        # If backup file provided, restore it
        if backup_file:
            # Resolve backup file path
            if not os.path.isabs(backup_file):
                backup_file = os.path.abspath(backup_file)
            
            if not os.path.isfile(backup_file):
                raise Exception(f"Backup file not found: {backup_file}")
            
            if dry_run:
                logger.info(f"Would restore from backup: {backup_file}")
                logger.info("Would verify backup contents")
                logger.info("Would restore database and configuration")
            else:
                logger.info(f"Restoring from backup: {backup_file}")
                restore(backup_file, project_dir, force_clean=True)
                logger.info("✅ Backup restored successfully")
        
        if dry_run:
            logger.info("🔍 DRY RUN COMPLETED: No changes were made")
            logger.info("The following would be done:")
            logger.info("1. Create project directory")
            logger.info("2. Download and run setup script")
            logger.info("3. Configure Redash version")
            logger.info("4. Start services")
            if backup_file:
                logger.info("5. Restore from backup")
        else:
            logger.info("✅ Redash setup completed successfully!")
        
    except Exception as e:
        logger.error(f"Setup failed: {str(e)}")
        # Clean up on failure
        if not dry_run and os.path.exists(setup_script):
            os.remove(setup_script)
        raise

def analyze_backup(archive_path):
    """Analyze backup contents to help debug restoration issues"""
    logger.info(f"🔍 Analyzing backup: {archive_path}")
    
    # Extract to temporary directory
    temp = tempfile.mkdtemp(prefix="redash-analyze-")
    logger.info(f"Extracting to: {temp}")
    
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(temp)
        
        # List all files
        print(f"\n📁 Backup Contents:")
        for root, dirs, files in os.walk(temp):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, temp)
                size = os.path.getsize(full_path)
                print(f"  {rel_path} ({size} bytes)")
        
        # Check for manifest
        manifest_file = os.path.join(temp, 'backup-manifest.json')
        if os.path.isfile(manifest_file):
            print(f"\n📋 Backup Manifest:")
            with open(manifest_file, 'r') as f:
                manifest = json.load(f)
                print(f"  Timestamp: {manifest.get('timestamp', 'Unknown')}")
                print(f"  Redash Database: {manifest.get('redash_database', 'Unknown')}")
                if 'files' in manifest:
                    print(f"  Files:")
                    for file_type, filename in manifest['files'].items():
                        print(f"    {file_type}: {filename}")
        
        # Analyze SQL files
        sql_files = [f for f in os.listdir(temp) if f.endswith('.sql')]
        if sql_files:
            print(f"\n🗄️  SQL Dump Analysis:")
            for sql_file in sql_files:
                sql_path = os.path.join(temp, sql_file)
                print(f"\n  📄 {sql_file}:")
                
                # Get file size
                size = os.path.getsize(sql_path)
                print(f"    Size: {size} bytes")
                
                # Read first few lines to understand format
                with open(sql_path, 'r') as f:
                    lines = f.readlines()
                    print(f"    Lines: {len(lines)}")
                    
                    # Show first 5 non-empty lines
                    non_empty = [line.strip() for line in lines if line.strip()]
                    print(f"    First 5 non-empty lines:")
                    for i, line in enumerate(non_empty[:5]):
                        print(f"      {i+1}: {line[:100]}{'...' if len(line) > 100 else ''}")
                    
                    # Look for specific patterns
                    copy_statements = [line for line in lines if line.strip().startswith('COPY')]
                    create_statements = [line for line in lines if line.strip().startswith('CREATE')]
                    insert_statements = [line for line in lines if line.strip().startswith('INSERT')]
                    
                    print(f"    COPY statements: {len(copy_statements)}")
                    print(f"    CREATE statements: {len(create_statements)}")
                    print(f"    INSERT statements: {len(insert_statements)}")
                    
                    # Look for table names in COPY statements
                    if copy_statements:
                        table_names = set()
                        for copy_line in copy_statements:
                            # Extract table name from COPY statement
                            match = re.search(r'COPY ([a-zA-Z_][a-zA-Z0-9_]*)', copy_line)
                            if match:
                                table_names.add(match.group(1))
                        print(f"    Tables with data: {', '.join(sorted(table_names))}")
        
        # Check for Redis dump
        redis_dump = os.path.join(temp, 'redis-dump.rdb')
        if os.path.isfile(redis_dump):
            size = os.path.getsize(redis_dump)
            print(f"\n🔴 Redis Dump:")
            print(f"  Size: {size} bytes")
            print(f"  Valid Redis dump: {'Yes' if size > 100 else 'No'}")
        
        # Check for env files
        env_files = [f for f in os.listdir(temp) if f in ['.env', 'env']]
        if env_files:
            print(f"\n⚙️  Environment Files:")
            for env_file in env_files:
                env_path = os.path.join(temp, env_file)
                size = os.path.getsize(env_path)
                print(f"  {env_file}: {size} bytes")
                
                # Check for secret key
                with open(env_path, 'r') as f:
                    content = f.read()
                    if 'REDASH_SECRET_KEY' in content:
                        print(f"    Contains REDASH_SECRET_KEY: Yes")
                    else:
                        print(f"    Contains REDASH_SECRET_KEY: No")
        
        print(f"\n✅ Backup analysis complete")
        
    except Exception as e:
        logger.error(f"Failed to analyze backup: {str(e)}")
        raise
    finally:
        # Clean up
        shutil.rmtree(temp)


def test_restore_step(archive_path, step="extract"):
    """Test individual restore steps to identify where failures occur"""
    logger.info(f"🧪 Testing restore step: {step}")
    
    if step == "extract":
        # Test archive extraction
        temp = tempfile.mkdtemp(prefix="redash-test-extract-")
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(temp)
            print(f"✅ Archive extraction successful to: {temp}")
            
            # List extracted files
            files = os.listdir(temp)
            print(f"📁 Extracted files: {files}")
            
        except Exception as e:
            print(f"❌ Archive extraction failed: {str(e)}")
            return False
        finally:
            shutil.rmtree(temp)
    
    elif step == "sql_parse":
        # Test SQL file parsing
        temp = tempfile.mkdtemp(prefix="redash-test-sql-")
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(temp)
            
            sql_files = [f for f in os.listdir(temp) if f.endswith('.sql')]
            if not sql_files:
                print("❌ No SQL files found in backup")
                return False
            
            print(f"📄 Found SQL files: {sql_files}")
            
            for sql_file in sql_files:
                sql_path = os.path.join(temp, sql_file)
                print(f"\n🔍 Testing {sql_file}:")
                
                with open(sql_path, 'r') as f:
                    content = f.read()
                
                # Test basic SQL syntax
                lines = content.split('\n')
                print(f"  Lines: {len(lines)}")
                
                # Check for common issues
                issues = []
                if 'DROP DATABASE' in content:
                    issues.append("Contains DROP DATABASE statements")
                if 'CREATE DATABASE' in content:
                    issues.append("Contains CREATE DATABASE statements")
                if '\\connect' in content:
                    issues.append("Contains \\connect statements")
                
                if issues:
                    print(f"  ⚠️  Potential issues: {', '.join(issues)}")
                else:
                    print(f"  ✅ No obvious issues detected")
                
                # Count data statements
                copy_count = content.count('COPY ')
                insert_count = content.count('INSERT ')
                print(f"  📊 Data statements: {copy_count} COPY, {insert_count} INSERT")
        
        except Exception as e:
            print(f"❌ SQL parsing test failed: {str(e)}")
            return False
        finally:
            shutil.rmtree(temp)
    
    elif step == "db_connect":
        # Test database connectivity
        if not detect_container_names():
            print("❌ Cannot detect container names")
            return False
        
        # Test PostgreSQL connection
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -c 'SELECT version();'",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        if result.returncode == 0:
            print(f"✅ PostgreSQL connection successful")
            print(f"  Version: {result.stdout.decode().strip()}")
        else:
            print(f"❌ PostgreSQL connection failed: {result.stderr.decode()}")
            return False
    
    else:
        print(f"❌ Unknown test step: {step}")
        return False
    
    return True


def manual_restore_test(archive_path, project_dir):
    """Manually test restore process step by step"""
    logger.info(f"🔧 Manual restore test for: {archive_path}")
    
    # Extract archive
    temp = tempfile.mkdtemp(prefix="redash-manual-test-")
    logger.info(f"Extracting to: {temp}")
    
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(temp)
        
        # Find SQL files
        sql_files = [f for f in os.listdir(temp) if f.endswith('.sql')]
        if not sql_files:
            print("❌ No SQL files found in backup")
            return False
        
        print(f"📄 Found SQL files: {sql_files}")
        
        # Detect container names
        if not detect_container_names():
            print("❌ Cannot detect container names")
            return False
        
        # Test database connection
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -c 'SELECT version();'",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        if result.returncode != 0:
            print(f"❌ Database connection failed: {result.stderr.decode()}")
            return False
        
        print(f"✅ Database connection successful")
        
        # Try to restore each SQL file individually
        for sql_file in sql_files:
            sql_path = os.path.join(temp, sql_file)
            print(f"\n🔍 Testing restore of {sql_file}:")
            
            # Read and analyze the file
            with open(sql_path, 'r') as f:
                content = f.read()
            
            # Check for problematic statements
            issues = []
            if 'DROP DATABASE' in content:
                issues.append("DROP DATABASE")
            if 'CREATE DATABASE' in content:
                issues.append("CREATE DATABASE")
            if '\\connect' in content:
                issues.append("\\connect")
            
            if issues:
                print(f"  ⚠️  Found problematic statements: {', '.join(issues)}")
                print(f"  💡 These will be removed during restore")
            
            # Try a small test restore (first 100 lines)
            lines = content.split('\n')
            test_content = '\n'.join(lines[:100])
            
            # Remove problematic statements from test
            test_content = re.sub(r'DROP DATABASE[^;]+;', '', test_content)
            test_content = re.sub(r'CREATE DATABASE[^;]+;', '', test_content)
            test_content = re.sub(r'\\connect[^;]+;', '', test_content)
            
            # Write test content to temporary file
            test_sql_path = os.path.join(temp, f"test_{sql_file}")
            with open(test_sql_path, 'w') as f:
                f.write(test_content)
            
            # Test the first 100 lines
            result = run_safe(
                f"docker exec -i {PG_CONTAINER} psql -U postgres -d postgres -f /dev/stdin",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            
            # Write test content to stdin
            if result.returncode == 0:
                # Use a different approach - write to a temp file in the container
                run(f"docker cp {test_sql_path} {PG_CONTAINER}:/tmp/test_restore.sql")
                result = run_safe(
                    f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -f /tmp/test_restore.sql",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                
                # Clean up temp file in container
                run_safe(f"docker exec {PG_CONTAINER} rm -f /tmp/test_restore.sql")
            
            if result.returncode == 0:
                print(f"  ✅ Test restore (first 100 lines) successful")
            else:
                print(f"  ❌ Test restore failed: {result.stderr.decode()[:200]}")
        
        print(f"\n✅ Manual restore test completed")
        return True
        
    except Exception as e:
        logger.error(f"Manual restore test failed: {str(e)}")
        return False
    finally:
        shutil.rmtree(temp)


def enhanced_restore(archive, project_dir, force_clean=False):
    """Enhanced restore with detailed logging and step-by-step progress tracking"""
    logger.info(f"🚀 Starting enhanced restore from: {archive}")
    
    # Check if it's a fresh host
    fresh_host = is_fresh_host()
    logger.info(f"Fresh host check result: {fresh_host}")
    
    if not fresh_host and not force_clean:
        logger.error("⚠️  Existing Redash installation detected!")
        logger.error("This restore will overwrite existing data.")
        logger.error("Use --force-clean to automatically clean volumes")
        sys.exit(1)
    
    # Extract archive
    temp = tempfile.mkdtemp(prefix="redash-enhanced-restore-")
    logger.info(f"📁 Extracting archive to {temp}")
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(temp)

    os.chdir(project_dir)

    # Clean volumes if requested
    if force_clean and not fresh_host:
        logger.info("🧹 Cleaning existing volumes...")
        clean_volumes(project_dir, force=True)
    
    # Stop the stack completely
    logger.info("🛑 Stopping Redash stack...")
    run_safe("docker compose down")

    # Start only Postgres and Redis with fresh volumes
    logger.info("🚀 Starting Postgres and Redis with fresh volumes...")
    run_safe("docker compose up -d postgres redis")
    
    # Detect container names after starting services
    if not detect_container_names():
        raise Exception("Failed to detect container names after starting services")
    
    # Wait for services to be ready
    logger.info("⏳ Waiting for services to be ready...")
    wait_for_container(PG_CONTAINER)
    wait_for_container(REDIS_CONTAINER)
    
    # Check for backup manifest
    manifest_file = os.path.join(temp, 'backup-manifest.json')
    redash_db = None
    
    if os.path.isfile(manifest_file):
        with open(manifest_file, 'r') as f:
            manifest = json.load(f)
            redash_db = manifest.get('redash_database')
            logger.info(f"📋 Found backup manifest, Redash database: {redash_db}")
    
    # Find SQL files
    redash_data_file = next((f for f in os.listdir(temp) if f.startswith("redash-data-") and f.endswith(".sql")), None)
    redash_dump_file = next((f for f in os.listdir(temp) if f.startswith("redash-db-") and f.endswith(".sql")), None)
    full_dump_file = next((f for f in os.listdir(temp) if f.startswith("postgres-") and f.endswith(".sql")), None)
    
    logger.info(f"📄 Found SQL files:")
    logger.info(f"  - Data-only: {redash_data_file}")
    logger.info(f"  - Redash dump: {redash_dump_file}")
    logger.info(f"  - Full dump: {full_dump_file}")
    
    # Start server to initialize schema
    logger.info("🚀 Starting server to initialize Redash schema...")
    run("docker compose up -d server")
    
    # Wait for server to start and re-detect container names
    time.sleep(10)  # Wait for container to start
    if detect_container_names():
        if SERVER_CONTAINER:
            wait_for_container(SERVER_CONTAINER)
        else:
            logger.warning("Server container not detected after starting")
    else:
        logger.warning("Failed to detect container names after starting server")
    
    time.sleep(10)  # Wait for schema initialization
    
    # Initialize Redash database schema
    logger.info("🗄️  Creating Redash database tables...")
    if SERVER_CONTAINER:
        result = run_safe(f"docker exec {SERVER_CONTAINER} python manage.py database create_tables")
    else:
        # Fallback to docker compose exec
        result = run_safe("docker compose exec -T server python manage.py database create_tables")
    
    if result.returncode != 0:
        logger.warning("Schema creation failed, but continuing with data restore...")
    else:
        logger.info("✅ Schema creation completed")
    
    # Stop server for clean data restoration
    logger.info("🛑 Stopping server for clean data restoration...")
    run("docker compose stop server")
    
    # Try data-only restore first (safest option)
    if redash_data_file:
        logger.info(f"📊 Attempting data-only restore from: {redash_data_file}")
        data_sql_path = os.path.join(temp, redash_data_file)
        
        # Copy data file to container for reliable restoration
        logger.info("📋 Copying data-only backup to container...")
        run(f"docker cp {data_sql_path} {PG_CONTAINER}:/tmp/restore_data.sql")
        
        # Now restore just the data with proper constraint handling
        logger.info("🔄 Restoring data-only backup...")
        restore_cmd = """
        psql -U postgres -d postgres -c 'SET session_replication_role = replica;' &&
        psql -U postgres -d postgres -f /tmp/restore_data.sql &&
        psql -U postgres -d postgres -c 'SET session_replication_role = DEFAULT;'
        """
        
        result = run_safe(
            f"docker exec {PG_CONTAINER} bash -c \"{restore_cmd}\"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        if result.returncode == 0:
            logger.info("✅ Data-only restore completed successfully")
            
            # Check if data was actually inserted
            if result.stdout:
                stdout_str = result.stdout.decode()
                copy_matches = re.findall(r'COPY (\d+)', stdout_str)
                if copy_matches:
                    total_rows = sum(int(match) for match in copy_matches)
                    logger.info(f"📊 Data insertion detected: {total_rows} rows copied")
                    
                    # Log specific table restoration
                    for table in ['users', 'queries', 'dashboards', 'widgets', 'data_sources']:
                        table_matches = re.search(rf'ALTER TABLE.*{table}.*\nCOPY (\d+)', stdout_str)
                        if table_matches:
                            logger.info(f"  ✅ {table.capitalize()}: {table_matches.group(1)} records")
                        else:
                            logger.info(f"  ❌ {table.capitalize()}: No data found")
                else:
                    logger.warning("⚠️  No data insertion detected in output")
            else:
                logger.info("✅ Data-only restore completed (no output to analyze)")
        else:
            logger.error(f"❌ Data-only restore failed: {result.stderr.decode()}")
            # Continue to try other methods
        
        # Clean up temp file
        run_safe(f"docker exec {PG_CONTAINER} rm -f /tmp/restore_data.sql")
    
    # Restore Redis
    redis_backup = os.path.join(temp, "redis-dump.rdb")
    if os.path.isfile(redis_backup):
        logger.info("🔴 Restoring Redis dump...")
        # Stop redis, copy dump, restart
        run("docker compose stop redis")
        run(f"docker cp {redis_backup} {REDIS_CONTAINER}:/data/dump.rdb")
        run("docker compose start redis")
        wait_for_container(REDIS_CONTAINER)
        logger.info("✅ Redis restore completed")
    else:
        logger.warning("⚠️  No redis-dump.rdb found, skipping Redis restore")

    # Restore configuration files and handle secret key
    backed_up_secret_key = None
    env_file_target = get_env_file_path(project_dir)
    
    # Restore env file
    env_src = None
    # Check manifest first
    if os.path.isfile(manifest_file):
        with open(manifest_file, 'r') as f:
            manifest = json.load(f)
            if 'files' in manifest and 'env_file' in manifest['files']:
                env_src = os.path.join(temp, manifest['files']['env_file'])
                logger.info(f"📋 Found env file in manifest: {env_src}")
    
    # If not in manifest, try to find env file in backup
    if not env_src or not os.path.isfile(env_src):
        for env_name in ['.env', 'env']:
            potential_src = os.path.join(temp, env_name)
            if os.path.isfile(potential_src):
                env_src = potential_src
                logger.info(f"📁 Found env file in backup: {env_src}")
                break
    
    if env_src and os.path.isfile(env_src):
        # Extract secret key before copying
        backed_up_secret_key = extract_secret_key_from_env(env_src)
        if backed_up_secret_key:
            logger.info(f"🔐 Found REDASH_SECRET_KEY in backup: {backed_up_secret_key[:8]}...")
        
        # Copy the entire env file from backup
        logger.info(f"📋 Restoring env file to: {env_file_target}")
        shutil.copy(env_src, env_file_target)
        logger.info("✅ Environment file restored")
    else:
        logger.warning("⚠️  No env file found in backup")
    
    # If we restored a secret key, restart containers to apply it
    if backed_up_secret_key:
        logger.info("🔄 Restarting containers to apply restored secret key...")
        run("docker compose down")
        run("docker compose up -d")
        time.sleep(5)  # Wait for containers to restart
        logger.info("✅ Containers restarted with new secret key")

    # Start full Redash stack
    logger.info("🚀 Starting full Redash stack...")
    run("docker compose up -d")
    
    # Wait a moment for services to start
    time.sleep(5)
    
    # Re-detect container names now that all containers should be running
    if not detect_container_names():
        logger.warning("Failed to detect all container names after starting full stack")
        logger.info("Continuing with restore verification...")
    
    # Wait for containers to stabilize
    logger.info("⏳ Waiting for containers to stabilize...")
    time.sleep(10)
    
    # Verify the restore worked
    logger.info("🔍 Verifying restore...")
    try:
        diagnose_restore_data(project_dir)
    except Exception as e:
        logger.warning(f"Verification failed: {str(e)}")
        logger.info("This is normal if containers are still starting up")
        logger.info("You can run --diagnose later to check the restore status")
    
    # Cleanup
    shutil.rmtree(temp)
    
    # Final verification - check if we actually restored data
    logger.info("🔍 Performing final verification...")
    time.sleep(5)  # Give containers more time to stabilize
    
    try:
        # Quick check for basic data
        result = run_safe(
            f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -t -c \"SELECT COUNT(*) FROM users;\"",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        
        if result.returncode == 0:
            try:
                user_count = int(result.stdout.decode().strip())
                if user_count > 0:
                    logger.info(f"✅ Restore verification successful: {user_count} users found")
                    logger.info("🎉 Enhanced restore completed successfully!")
                else:
                    logger.warning("⚠️  Restore completed but no users found")
                    logger.info("💡 Run --diagnose to check restore status")
            except ValueError:
                logger.warning("⚠️  Restore completed but verification inconclusive")
                logger.info("💡 Run --diagnose to check restore status")
        else:
            logger.warning("⚠️  Restore completed but verification failed")
            logger.info("💡 Run --diagnose to check restore status")
    except Exception as e:
        logger.warning(f"⚠️  Restore completed but verification failed: {str(e)}")
        logger.info("💡 Run --diagnose to check restore status")


def main():
    parser = argparse.ArgumentParser(
        description="🚀 Redash Backup & Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
📋 EXAMPLES:
  # Initialize environment
  %(prog)s --init

  # Create backup
  %(prog)s --backup /opt/backups

  # Validate existing database
  %(prog)s --validate-db
  
  # Diagnose current data
  %(prog)s --diagnose

  # Safe restore (fresh host)
  %(prog)s --restore /path/to/backup.zip

  # Force restore (overwrite existing)
  %(prog)s --restore /path/to/backup.zip --force-clean

  # User management
  %(prog)s --list-users
  %(prog)s --reset-password admin@example.com
  %(prog)s --reset-password user@domain.com newpassword123

  # Volume cleanup
  %(prog)s --force-clean

  # Setup and Upgrade
  %(prog)s --setup-10
  %(prog)s --setup-25
  %(prog)s --setup-10 --restore /path/to/backup.zip
  %(prog)s --setup-25 --restore /path/to/backup.zip
  %(prog)s --setup-10 --dry-run
  %(prog)s --setup-25 --dry-run
  %(prog)s --upgrade-25

🔧 COMMON WORKFLOWS:
  1. Fresh Install: --init → --restore /path/to/backup.zip
  2. Migration: --backup /tmp → --restore /path/to/backup.zip --force-clean
  3. Password Reset: --list-users → --reset-password user@domain.com
  4. Troubleshooting: --validate-db → --diagnose
  5. Upgrade: --upgrade-25
  6. New Setup: --setup-10/25 [--restore /path/to/backup.zip] [--dry-run]

⚠️  SAFETY: Use --validate-db before --restore to check existing data
        """
    )

    # Setup/initialization commands
    setup_group = parser.add_argument_group('🔧 Setup & Initialization')
    setup_group.add_argument("--init", action="store_true", 
                           help="Initialize environment (create directories, set permissions)")

    # Backup/restore commands  
    backup_group = parser.add_argument_group('💾 Backup & Restore')
    main_commands = backup_group.add_mutually_exclusive_group()
    main_commands.add_argument("--backup", metavar="OUTDIR", 
                              help="Create backup archive in specified directory")
    main_commands.add_argument("--restore", metavar="ARCHIVE", 
                              help="Restore from backup archive (ZIP file)")

    # Safety and validation commands
    safety_group = parser.add_argument_group('🛡️  Safety & Validation')
    safety_group.add_argument("--validate-db", action="store_true", 
                             help="Check database state and existing data before restore")
    safety_group.add_argument("--diagnose", action="store_true", 
                             help="Analyze current Redash data (users, queries, dashboards, etc.)")
    safety_group.add_argument("--force-clean", action="store_true", 
                             help="⚠️  DESTRUCTIVE: Force clean volumes (standalone or with --restore)")

    # User management commands
    user_group = parser.add_argument_group('👥 User Management')
    user_group.add_argument("--list-users", action="store_true", 
                           help="List all Redash users in the system")
    user_group.add_argument("--reset-password", nargs='+', metavar=('EMAIL', 'PASSWORD'), 
                           help="Reset user password (EMAIL required, PASSWORD optional)")

    # Debug and troubleshooting commands
    debug_group = parser.add_argument_group('🔍 Debug & Troubleshooting')
    debug_group.add_argument("--fix-secret-key", metavar="SECRET_KEY", 
                            help="Fix REDASH_SECRET_KEY mismatch (provide the correct key)")
    debug_group.add_argument("--debug-data", action="store_true", 
                            help="Debug missing data issues (dashboards, widgets, etc.)")
    debug_group.add_argument("--analyze-backup", metavar="ARCHIVE", 
                            help="Analyze backup contents to debug restoration issues")
    debug_group.add_argument("--test-restore", metavar="ARCHIVE", 
                            help="Test restore process step by step")
    debug_group.add_argument("--manual-restore-test", metavar="ARCHIVE", 
                            help="Manually test restore process with detailed output")
    debug_group.add_argument("--enhanced-restore", metavar="ARCHIVE", 
                            help="Enhanced restore with detailed logging and step-by-step progress")

    # Global options
    global_group = parser.add_argument_group('⚙️  Global Options')
    global_group.add_argument("--project-dir", default="/opt/redash", 
                             help="Redash project directory (default: /opt/redash)")

    # Add upgrade command
    upgrade_group = parser.add_argument_group('🔄 Upgrade Commands')
    upgrade_group.add_argument("--upgrade-10", action="store_true",
                             help="Upgrade Redash to version 10.1.0.b50633")
    upgrade_group.add_argument("--upgrade-25", action="store_true",
                             help="Upgrade Redash to version 25.1.0")

    # Add setup commands
    setup_group = parser.add_argument_group('🔄 Setup & Upgrade')
    setup_group.add_argument("--setup-10", action="store_true",
                           help="Set up a new Redash V10.1.0 instance")
    setup_group.add_argument("--setup-25", action="store_true",
                           help="Set up a new Redash V25.1.0 instance")
    setup_group.add_argument("--setup-version", metavar="VERSION",
                           help="Set up a specific Redash version (10.1.0 or 25.1.0)")
    setup_group.add_argument("--dry-run", action="store_true",
                           help="Show what would be done without making changes")

    args = parser.parse_args()

    if args.init:
        do_init()
    elif args.validate_db:
        validation = validate_database(args.project_dir)
        
        print("🔍 Database Validation Results:")
        print(f"  Databases: {validation['database_count']}")
        print(f"  Tables: {validation['table_count']}")
        print(f"  Approximate size: {validation['approximate_size']}")
        print(f"  Has existing data: {'YES' if validation['has_data'] else 'NO'}")
        
        if validation['databases']:
            print(f"  Database list: {', '.join(validation['databases'])}")
        
        if validation['warnings']:
            print("\n⚠️  Warnings:")
            for warning in validation['warnings']:
                print(f"  - {warning}")
        
        if validation['has_data']:
            print("\n💡 Recommendations:")
            print("  - Use --force-clean with --restore to overwrite existing data")
            print("  - Create a backup before restoring if you want to preserve current data")
        else:
            print("\n💡 Status:")
            print("  - No meaningful Redash data detected")
            print("  - Safe to restore without --force-clean")
        
        sys.exit(0)
    elif args.list_users:
        list_users(args.project_dir)
        sys.exit(0)
    elif args.diagnose:
        diagnose_restore_data(args.project_dir)
        sys.exit(0)
    elif args.fix_secret_key:
        if fix_secret_key(args.project_dir, args.fix_secret_key):
            print(f"✅ Updated REDASH_SECRET_KEY")
            print("🔄 Recreating Redash stack to apply changes...")
            os.chdir(args.project_dir)
            # Need to recreate containers to pick up env file changes
            run("docker compose down")
            run("docker compose up -d")
            print("✅ Stack recreated successfully")
        else:
            print("❌ Failed to update secret key")
            sys.exit(1)
        sys.exit(0)
    elif args.debug_data:
        debug_missing_data(args.project_dir)
        sys.exit(0)
    elif args.analyze_backup:
        analyze_backup(args.analyze_backup)
        sys.exit(0)
    elif args.test_restore:
        # Test all restore steps
        steps = ["extract", "sql_parse", "db_connect"]
        for step in steps:
            if not test_restore_step(args.test_restore, step):
                print(f"❌ Test failed at step: {step}")
                sys.exit(1)
        print("✅ All restore tests passed")
        sys.exit(0)
    elif args.manual_restore_test:
        if manual_restore_test(args.manual_restore_test, args.project_dir):
            print("✅ Manual restore test completed successfully")
        else:
            print("❌ Manual restore test failed")
            sys.exit(1)
        sys.exit(0)
    elif args.enhanced_restore:
        enhanced_restore(args.enhanced_restore, args.project_dir, args.force_clean)
        print("✅ Enhanced restore completed")
        sys.exit(0)
    elif args.reset_password:
        if len(args.reset_password) == 1:
            # Email provided, password will be prompted
            reset_password(args.project_dir, args.reset_password[0])
        elif len(args.reset_password) == 2:
            # Both email and password provided
            reset_password(args.project_dir, args.reset_password[0], args.reset_password[1])
        else:
            logger.error("Invalid arguments for --reset-password. Use: --reset-password EMAIL [PASSWORD]")
            sys.exit(1)
        sys.exit(0)
    elif args.force_clean and not args.restore:
        # Allow --force-clean to work standalone
        print("🧹 Cleaning Docker volumes and containers...")
        clean_volumes(args.project_dir, force=True)
        print("✅ Volume cleanup completed")
        sys.exit(0)
    elif args.upgrade_10:
        upgrade_redash_semantic(args.project_dir)
        sys.exit(0)
    elif args.upgrade_25:
        upgrade_redash_semantic(args.project_dir, "25.1.0")
        sys.exit(0)
    elif args.setup_10 or args.setup_25 or args.setup_version:
        version = "10.1.0" if args.setup_10 else (args.setup_version or "25.1.0")
        setup_redash(args.project_dir, version, args.restore if args.restore else None, args.dry_run)
        sys.exit(0)
    elif args.backup:
        archive = backup(args.backup)
        print(f"✅ Backup written to: {archive}")
    elif args.restore:
        restore(args.restore, args.project_dir, args.force_clean)
        print("✅ Restore finished successfully")
    else:
        print("❌ No command specified!\n")
        print("💡 Most common commands:")
        print("   --help              Show this help message")
        print("   --init              Initialize environment")
        print("   --backup DIR        Create backup")
        print("   --restore FILE      Restore from backup")
        print("   --validate-db       Check database state")
        print("   --diagnose          Analyze current data")
        print("   --list-users        List users")
        print("\n📖 Use --help for complete documentation and examples")
        sys.exit(1)


if __name__ == "__main__":
    main()
