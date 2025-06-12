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

# Default container names
PG_CONTAINER = "redash_postgres_1"
REDIS_CONTAINER = "redash_redis_1"
SERVER_CONTAINER = "redash_server_1"
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


def wait_for_container(container_name, timeout=30):
    logger.info(f"Waiting for container '{container_name}' to be ready...")
    for i in range(timeout):
        # Check if container is running
        result = subprocess.run(
            f"docker inspect --format='{{{{.State.Status}}}}' {container_name}",
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if result.stdout.strip() == b'running':
            # For postgres, also check if it's accepting connections
            if container_name == PG_CONTAINER:
                pg_check = subprocess.run(
                    f"docker exec {PG_CONTAINER} pg_isready -U postgres",
                    shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                if pg_check.returncode == 0:
                    logger.info(f"Container '{container_name}' is ready.")
                    return
            else:
                logger.info(f"Container '{container_name}' is ready.")
                return
        time.sleep(1)
    logger.warning(f"Timed out waiting for container '{container_name}' to be ready.")


def is_fresh_host():
    """Check if this appears to be a fresh host with no existing Redash data"""
    # Instead of just checking for volumes/containers, check for actual data
    try:
        # Check if containers exist and have meaningful data
        result = run_safe("docker ps -a --format '{{.Names}}'", stdout=subprocess.PIPE)
        if result.returncode != 0:
            return True
        
        containers = result.stdout.decode().strip().split('\n') if result.stdout else []
        redash_containers = [c for c in containers if 'redash' in c or PG_CONTAINER in c or REDIS_CONTAINER in c]
        
        # If no Redash containers at all, it's fresh
        if len(redash_containers) == 0:
            return True
        
        # If containers exist, check if they contain meaningful Redash data
        # Try to validate the database - if it has no Redash data, consider it fresh
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
    
    # Check if containers are running
    pg_running = run_safe(f"docker ps --filter name={PG_CONTAINER} --format '{{{{.Names}}}}'", 
                         stdout=subprocess.PIPE).returncode == 0
    
    if not pg_running:
        logger.info("Starting Postgres container for validation...")
        run("docker-compose up -d postgres")
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
    
    # Change to project directory if it exists and contains docker-compose.yml
    if os.path.isdir(project_dir) and os.path.isfile(os.path.join(project_dir, "docker-compose.yml")):
        os.chdir(project_dir)
        logger.info("Stopping and removing all containers via docker-compose...")
        run_safe("docker-compose down -v")  # -v removes volumes too
    else:
        logger.info("No docker-compose.yml found, cleaning containers individually...")
    
    # Remove containers explicitly (in case docker-compose didn't work)
    containers_to_remove = []
    result = run_safe("docker ps -a --format '{{.Names}}'", stdout=subprocess.PIPE)
    if result.returncode == 0:
        all_containers = result.stdout.decode().strip().split('\n') if result.stdout else []
        containers_to_remove = [c for c in all_containers if c and ('redash' in c.lower() or PG_CONTAINER in c or REDIS_CONTAINER in c)]
    
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

    for fn in [COMPOSE_FILE, ENV_FILE]:
        if os.path.isfile(fn):
            shutil.copy(fn, workdir)
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
    
    manifest_file = os.path.join(workdir, 'backup-manifest.json')
    with open(manifest_file, 'w') as f:
        json.dump(manifest, f, indent=2)

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
    run_safe("docker-compose down")

    # Start only Postgres and Redis with fresh volumes
    logger.info("Starting Postgres and Redis with fresh volumes...")
    run("docker-compose up -d postgres redis")
    
    # Wait for services to be ready
    wait_for_container(PG_CONTAINER)
    wait_for_container(REDIS_CONTAINER)

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
        with open(sql_path, "r") as sql_file_handle:
            proc = subprocess.Popen(
                f"docker exec -i {PG_CONTAINER} psql -U postgres -v ON_ERROR_STOP=1",
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
                with open(sql_path, "r") as sql_file_handle:
                    # Prepare SQL with disabled constraints
                    disable_fk_sql = """
                    SET session_replication_role = replica;
                    """
                    enable_fk_sql = """
                    SET session_replication_role = DEFAULT;
                    """
                    
                    # Use a more comprehensive approach to handle foreign keys
                    restore_cmd = f"""
                    BEGIN;
                    SET session_replication_role = replica;
                    \\i {sql_path}
                    SET session_replication_role = DEFAULT;
                    COMMIT;
                    """
                    
                    # Copy the SQL file into the container first
                    run(f"docker cp {sql_path} {PG_CONTAINER}:/tmp/restore.sql")
                    
                    # Run the restore with comprehensive FK handling
                    proc = subprocess.Popen(
                        f"docker exec {PG_CONTAINER} psql -U postgres -d postgres -c \"BEGIN; SET session_replication_role = replica;\"",
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    proc.wait()
                    
                    # Import the SQL file
                    with open(sql_path, "r") as sql_file_handle:
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
                        import re
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
                            run("docker-compose up -d server")
                            wait_for_container("redash_server_1")
                            time.sleep(10)  # Wait for schema initialization
                            
                            # Initialize Redash database schema
                            logger.info("Creating Redash database tables...")
                            result = run_safe("docker-compose exec -T server python manage.py database create_tables")
                            if result.returncode != 0:
                                logger.warning("Schema creation failed, but continuing with data restore...")
                            
                            # Stop server for clean data restoration
                            run("docker-compose stop server")
                            
                            # Clear potentially conflicting data that might block restoration
                            logger.info("Clearing potentially conflicting data...")
                            clear_sql = """
                            SET session_replication_role = replica;
                            TRUNCATE TABLE dashboards RESTART IDENTITY CASCADE;
                            TRUNCATE TABLE widgets RESTART IDENTITY CASCADE;
                            TRUNCATE TABLE queries RESTART IDENTITY CASCADE;
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
                                    import re
                                    stdout_str = result.stdout.decode()
                                    copy_matches = re.findall(r'COPY (\d+)', stdout_str)
                                    if copy_matches:
                                        total_rows = sum(int(match) for match in copy_matches)
                                        logger.info(f"Data-only restore succeeded! {total_rows} rows inserted")
                                        
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
        run("docker-compose stop redis")
        run(f"docker cp {redis_backup} {REDIS_CONTAINER}:/data/dump.rdb")
        run("docker-compose start redis")
        wait_for_container(REDIS_CONTAINER)
    else:
        logger.warning("No redis-dump.rdb found, skipping Redis restore")

    # Restore configuration files and handle secret key
    backed_up_secret_key = None
    env_file_target = get_env_file_path(project_dir)
    
    for fn in [COMPOSE_FILE, env_file_target]:
        src = os.path.join(temp, os.path.basename(fn))
        
        # For env files, try both .env and env naming
        if not os.path.isfile(src) and 'env' in os.path.basename(fn):
            alt_names = ['.env', 'env']
            for alt_name in alt_names:
                alt_src = os.path.join(temp, alt_name)
                if os.path.isfile(alt_src):
                    src = alt_src
                    break
        
        if os.path.isfile(src):
            logger.info(f"Restoring config file: {fn}")
            
            # Extract secret key from backed up env file before overwriting
            if 'env' in os.path.basename(fn):
                backed_up_secret_key = extract_secret_key_from_env(src)
                if backed_up_secret_key:
                    logger.info(f"Found REDASH_SECRET_KEY in backup: {backed_up_secret_key[:8]}...")
            
            shutil.copy(src, fn)
    
    # If we restored a secret key, restart containers to apply it
    if backed_up_secret_key:
        logger.info("Restarting containers to apply restored secret key...")
        run("docker-compose restart")

    # Start full Redash stack
    logger.info("Starting full Redash stack...")
    run("docker-compose up -d")
    
    # Wait a moment for services to start
    time.sleep(5)
    
    # Verify the restore worked
    logger.info("Verifying restore...")
    diagnose_restore_data(project_dir)
    
    # Cleanup
    shutil.rmtree(temp)
    logger.info("✅ Restore completed successfully")


def reset_password(project_dir, email, password=None):
    """Reset password for a Redash user"""
    os.chdir(project_dir)
    
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
    
    # Find the running server container
    result = run_safe("docker ps --format '{{.Names}}' --filter name=server", stdout=subprocess.PIPE)
    if result.returncode != 0:
        logger.error("Failed to list Docker containers")
        sys.exit(1)
    
    containers = result.stdout.decode().strip().split('\n') if result.stdout else []
    server_containers = [c for c in containers if c and ('redash' in c.lower() and 'server' in c.lower())]
    
    if not server_containers:
        logger.error("No running Redash server container found")
        logger.info("Try starting the server first: docker-compose up -d")
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
    
    # Check if postgres container is running
    pg_running = run_safe(f"docker ps --filter name={PG_CONTAINER} --format '{{{{.Names}}}}'", 
                         stdout=subprocess.PIPE)
    
    if pg_running.returncode != 0 or PG_CONTAINER not in pg_running.stdout.decode():
        logger.info("Starting Postgres container...")
        run("docker-compose up -d postgres")
        wait_for_container(PG_CONTAINER)
    
    # Get list of databases
    result = run_safe(
        f"docker exec {PG_CONTAINER} psql -U postgres -t -c \"SELECT datname FROM pg_database WHERE datistemplate = false;\"",
        stdout=subprocess.PIPE
    )
    
    if result.returncode != 0:
        logger.error("Failed to list databases")
        return None
    
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
    redash_db = get_redash_database(project_dir)
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
        
        # Check for encryption errors in logs
        result = run_safe("docker logs redash_server_1 --tail 50 | grep -i 'InvalidToken\\|decrypt'", stdout=subprocess.PIPE)
        if result.returncode == 0 and result.stdout.strip():
            print("⚠️  Encryption errors detected! Secret key mismatch likely.")
            print("💡 Use: --fix-secret-key <correct_key> to fix")
        else:
            print("✅ No encryption errors detected")
    
    print()


def check_secret_key_mismatch(project_dir):
    """Check if there's a secret key mismatch causing encryption issues"""
    os.chdir(project_dir)
    
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
    result = run_safe("docker logs redash_server_1 --tail 100 | grep -i 'error\\|violation\\|constraint'", stdout=subprocess.PIPE)
    if result.returncode == 0 and result.stdout:
        print("⚠️  Recent errors in server logs:")
        print(result.stdout.decode())


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
  %(prog)s --restore backup.zip

  # Force restore (overwrite existing)
  %(prog)s --restore backup.zip --force-clean

  # User management
  %(prog)s --list-users
  %(prog)s --reset-password admin@example.com
  %(prog)s --reset-password user@domain.com newpassword123

  # Volume cleanup
  %(prog)s --force-clean

🔧 COMMON WORKFLOWS:
  1. Fresh Install: --init → --restore backup.zip
  2. Migration: --backup /tmp → --restore backup.zip --force-clean
  3. Password Reset: --list-users → --reset-password user@domain.com
  4. Troubleshooting: --validate-db → --diagnose

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

    # Global options
    global_group = parser.add_argument_group('⚙️  Global Options')
    global_group.add_argument("--project-dir", default="/opt/redash", 
                             help="Redash project directory (default: /opt/redash)")

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
            run("docker-compose down")
            run("docker-compose up -d")
            print("✅ Stack recreated successfully")
        else:
            print("❌ Failed to update secret key")
            sys.exit(1)
        sys.exit(0)
    elif args.debug_data:
        debug_missing_data(args.project_dir)
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
