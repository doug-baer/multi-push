#!/usr/bin/env python3
import os
import sys
import argparse
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import Manager, Process
from paramiko import SSHClient, AutoAddPolicy, ssh_exception, SFTPError
from tqdm import tqdm
from queue import Empty

# Configuration Defaults
SSH_PORT = 22
PARTS_PER_FILE = 4       # Number of chunks to split each file into
MAX_RETRIES = 5          # Maximum connection retry attempts
RETRY_DELAY = 10         # Seconds to wait between retries
MAX_CONCURRENT_FILES = 4 # Maximum files being transferred simultaneously
MAX_CONNECTIONS = 100    # Maximum simultaneous SSH/SFTP sessions globally


def get_remote_file_size(remote_host, username, remote_path, connection_semaphore):
    """
    Attempts to fetch the size of a remote file via SFTP.
    Returns size in bytes if file exists, or None if it doesn't exist or errors.
    """
    ssh = None
    sftp = None
    with connection_semaphore:
        try:
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(remote_host, port=SSH_PORT, username=username, timeout=15)
            sftp = ssh.open_sftp()
            return sftp.stat(remote_path).st_size
        except (SFTPError, FileNotFoundError):
            return None
        except Exception as e:
            print(f"[Warning] Failed checking remote size for {remote_path}: {e}")
            return None
        finally:
            if sftp:
                sftp.close()
            if ssh:
                ssh.close()


def ensure_remote_dir(remote_host, username, remote_dir, connection_semaphore):
    """Recursively creates remote directories on the SFTP server (mkdir -p)."""
    ssh = None
    sftp = None
    dirs = remote_dir.strip("/").split("/")
    current_dir = ""

    with connection_semaphore:
        try:
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(remote_host, port=SSH_PORT, username=username, timeout=15)
            sftp = ssh.open_sftp()

            for folder in dirs:
                current_dir += "/" + folder
                try:
                    sftp.stat(current_dir)
                except FileNotFoundError:
                    sftp.mkdir(current_dir)
        except Exception as e:
            print(f"[Error] Failed creating directory structure {remote_dir}: {e}")
        finally:
            if sftp:
                sftp.close()
            if ssh:
                ssh.close()


def create_remote_file_stub(remote_host, username, remote_path, connection_semaphore):
    """Creates/truncates remote destination file once before multi-process writing begins."""
    ssh = None
    sftp = None
    with connection_semaphore:
        try:
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(remote_host, port=SSH_PORT, username=username, timeout=15)
            sftp = ssh.open_sftp()
            # Truncate/create empty file
            with sftp.open(remote_path, "w") as f:
                f.truncate(0)
            return True
        except Exception as e:
            print(f"[Error] Failed initializing remote file {remote_path}: {e}")
            return False
        finally:
            if sftp:
                sftp.close()
            if ssh:
                ssh.close()


def split_file_into_parts(file_path, num_parts):
    """Yields (part_number, offset, part_size) chunks for a local file."""
    file_size = os.path.getsize(file_path)
    part_size = file_size // num_parts
    for i in range(num_parts):
        offset = i * part_size
        if i == num_parts - 1:
            part_size = file_size - offset  # Remainder added to last chunk
        yield i, offset, part_size


def upload_part(remote_host, username, remote_path, local_path, num, offset, part_size, progress_queue, connection_semaphore):
    """Worker task uploading a specific chunk of a file using SFTP with jittered backoff retry logic."""
    attempt = 0
    while attempt < MAX_RETRIES:
        ssh = None
        sftp = None
        try:
            # Acquire slot from global connection pool before creating SSH session
            with connection_semaphore:
                ssh = SSHClient()
                ssh.set_missing_host_key_policy(AutoAddPolicy())
                ssh.connect(remote_host, port=SSH_PORT, username=username, timeout=20)
                sftp = ssh.open_sftp()

                with open(local_path, "rb") as local_file:
                    local_file.seek(offset)
                    # Open remote file in read-write mode (must already exist)
                    with sftp.open(remote_path, "r+") as remote_file:
                        remote_file.seek(offset)
                        remote_file.set_pipelined(True)

                        size_uploaded = 0
                        while size_uploaded < part_size:
                            buffer_size = min(32768, part_size - size_uploaded)
                            data = local_file.read(buffer_size)
                            if not data:
                                break

                            remote_file.write(data)
                            size_uploaded += len(data)
                            progress_queue.put(len(data))

            # Exit retry loop on successful upload completion
            return True

        except (ssh_exception.SSHException, SFTPError, OSError) as e:
            attempt += 1
            # Calculate backoff with exponential scaling + randomized jitter (1.0 to 5.0 seconds)
            # This prevents multiple workers from hammering SSH banner exchanges simultaneously
            jitter = random.uniform(1.0, 5.0)
            backoff_delay = (RETRY_DELAY * attempt) + jitter

            print(
                f"[Part {num}] Connection/SFTP error uploading {os.path.basename(local_path)} "
                f"(Attempt {attempt}/{MAX_RETRIES}): {e}. Retrying in {backoff_delay:.1f}s..."
            )
            time.sleep(backoff_delay)

        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass
            if ssh:
                try:
                    ssh.close()
                except Exception:
                    pass

    print(f"[Part {num}] Hard failure: Failed to upload {os.path.basename(local_path)} after {MAX_RETRIES} attempts.")
    return False


def determine_parts_per_file(file_size_bytes):
    """Dynamically determines the number of parallel streams based on file size."""
    MB = 1024 * 1024
    GB = 1024 * MB

    if file_size_bytes < 50 * MB:
        return 1
    elif file_size_bytes < 1 * GB:
        return 4
    else:
        return 16


def process_file(file_path, remote_directory, remote_host, username, progress_queue, position, connection_semaphore):
    """Handles checking, staging, parallel chunk execution, and post-transfer verification for a single file."""
    file_name = os.path.basename(file_path)
    remote_file_path = os.path.join(remote_directory, file_name).replace("\\", "/")
    total_size = os.path.getsize(file_path)

    # 1. Check if remote file exists and size already matches
    remote_size = get_remote_file_size(remote_host, username, remote_file_path, connection_semaphore)
    if remote_size == total_size:
        print(f"[Skip] {file_name} matches target size ({total_size} bytes).")
        return True

    # 2. Pre-create target file stub
    if not create_remote_file_stub(remote_host, username, remote_file_path, connection_semaphore):
        print(f"[Fail] Could not initialize remote file stub for {file_name}.")
        return False

    # 3. Determine dynamic stream count and launch chunk processes directly (no nested executors)
    parts_per_file = determine_parts_per_file(total_size)
    parts = list(split_file_into_parts(file_path, parts_per_file))

    progress_bar = tqdm(
        total=total_size,
        desc=f"Pushing {file_name[:20]} ({parts_per_file} str)",
        unit="B",
        unit_scale=True,
        position=position,
        leave=False,
    )

    try:
        processes = []
        for num, offset, part_size in parts:
            p = Process(
                target=upload_part,
                args=(
                    remote_host,
                    username,
                    remote_file_path,
                    file_path,
                    num,
                    offset,
                    part_size,
                    progress_queue,
                    connection_semaphore,
                ),
            )
            processes.append(p)
            p.start()

        # Non-blocking progress queue drain while processes run
        while any(p.is_alive() for p in processes):
            while True:
                try:
                    bytes_added = progress_queue.get_nowait()  # Safe non-blocking read
                    progress_bar.update(bytes_added)
                except Empty:
                    break
            time.sleep(0.05)

        # Final queue drain after processes complete
        while True:
            try:
                bytes_added = progress_queue.get_nowait()
                progress_bar.update(bytes_added)
            except Empty:
                break

        # Wait for all processes to terminate cleanly
        for p in processes:
            p.join()

        # Check if all processes completed with exitcode 0
        if any(p.exitcode != 0 for p in processes):
            print(f"[Error] One or more chunk processes failed for file: {file_name}")
            return False

    finally:
        progress_bar.close()

    # 4. Final Size Verification
    final_remote_size = get_remote_file_size(remote_host, username, remote_file_path, connection_semaphore)
    if final_remote_size == total_size:
        return True
    else:
        print(f"[Validation Failed] {file_name} final size mismatch. Expected {total_size}, got {final_remote_size}.")
        return False


def process_directory(directory_path, remote_directory, remote_host, username, max_connections):
    """Walks directory, sets up remote folder tree, and executes transfers with explicit manager cleanup."""
    manager = Manager()
    try:
        progress_queue = manager.Queue()
        connection_semaphore = manager.Semaphore(max_connections)

        file_tasks = []

        print("Scanning directory tree and creating remote paths...")
        for root, _, files in os.walk(directory_path):
            relative_path = os.path.relpath(root, directory_path)
            current_remote_dir = (
                remote_directory if relative_path == "."
                else os.path.join(remote_directory, relative_path).replace("\\", "/")
            )

            ensure_remote_dir(remote_host, username, current_remote_dir, connection_semaphore)

            for file_name in files:
                file_path = os.path.join(root, file_name)
                if os.path.isfile(file_path):
                    file_tasks.append((file_path, current_remote_dir))

        print(f"Found {len(file_tasks)} files. Submitting to worker pool...")

        successful = 0
        failed = 0

        with ProcessPoolExecutor(max_workers=MAX_CONCURRENT_FILES) as file_executor:
            futures = {
                file_executor.submit(
                    process_file,
                    file_path,
                    remote_dir,
                    remote_host,
                    username,
                    progress_queue,
                    idx % MAX_CONCURRENT_FILES,
                    connection_semaphore,
                ): file_path
                for idx, (file_path, remote_dir) in enumerate(file_tasks)
            }

            for future in as_completed(futures):
                path = futures[future]
                try:
                    res = future.result()
                    if res:
                        successful += 1
                    else:
                        failed += 1
                except Exception as e:
                    print(f"Unhandled exception processing {path}: {e}")
                    failed += 1

        print("\n================ Transfer Summary ================")
        print(f"Total Files Handled: {len(file_tasks)}")
        print(f"Successfully Processed/Verified: {successful}")
        print(f"Failed Transfers: {failed}")
        print("==================================================")

    except KeyboardInterrupt:
        print("\n[Terminated] KeyboardInterrupt received. Aborting transfers...")
        sys.exit(1)
    finally:
        # Explicitly shut down the Manager process to prevent hanging on exit
        manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Multi-threaded chunked SFTP Directory Synchronization.")
    parser.add_argument("--directory_path", required=True, help="Path to local directory")
    parser.add_argument("--remote_host", required=True, help="SFTP server host/IP")
    parser.add_argument("--username", required=True, help="SFTP username")
    parser.add_argument("--remote_directory", required=True, help="SFTP base target directory")
    parser.add_argument("--max_connections", type=int, default=MAX_CONNECTIONS, help="Max simultaneous connections (Default: 100)")

    args = parser.parse_args()

    process_directory(
        args.directory_path,
        args.remote_directory,
        args.remote_host,
        args.username,
        args.max_connections,
    )


if __name__ == "__main__":
    main()
    