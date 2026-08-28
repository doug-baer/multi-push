#!/usr/bin/env python3
import os
import sys
import time
import random
import argparse
from queue import Empty
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import Manager, Process
from paramiko import SSHClient, AutoAddPolicy, ssh_exception, SFTPError
from tqdm import tqdm

# Configuration Defaults
SSH_PORT = 22
MAX_RETRIES = 5           # Maximum connection retry attempts per part
RETRY_DELAY = 5           # Base seconds to wait between retries
MAX_CONCURRENT_FILES = 4  # Maximum files being transferred simultaneously
MAX_CONNECTIONS = 100     # Maximum simultaneous SSH/SFTP sessions globally


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


def get_remote_file_size(remote_host, username, remote_path, connection_semaphore, connect_lock):
    """Attempts to fetch the size of a remote file via SFTP. Returns size in bytes or None."""
    ssh = None
    sftp = None
    with connection_semaphore:
        try:
            with connect_lock:
                ssh = SSHClient()
                ssh.set_missing_host_key_policy(AutoAddPolicy())
                ssh.connect(
                    remote_host,
                    port=SSH_PORT,
                    username=username,
                    timeout=15,
                    banner_timeout=30,
                    auth_timeout=30,
                )
                time.sleep(0.15)  # Pace connection handshakes

            sftp = ssh.open_sftp()
            return sftp.stat(remote_path).st_size
        except Exception:
            return None
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


def ensure_remote_dir(remote_host, username, remote_dir, connection_semaphore, connect_lock):
    """Recursively creates remote directories on the SFTP server (mkdir -p)."""
    ssh = None
    sftp = None
    dirs = [d for d in remote_dir.strip("/").split("/") if d]
    current_dir = ""

    with connection_semaphore:
        try:
            with connect_lock:
                ssh = SSHClient()
                ssh.set_missing_host_key_policy(AutoAddPolicy())
                ssh.connect(
                    remote_host,
                    port=SSH_PORT,
                    username=username,
                    timeout=15,
                    banner_timeout=30,
                    auth_timeout=30,
                )
                time.sleep(0.15)

            sftp = ssh.open_sftp()
            for folder in dirs:
                current_dir += "/" + folder
                try:
                    sftp.stat(current_dir)
                except FileNotFoundError:
                    try:
                        sftp.mkdir(current_dir)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Error] Failed creating directory structure {remote_dir}: {e}")
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


def create_remote_file_stub(remote_host, username, remote_path, connection_semaphore, connect_lock):
    """Creates/truncates remote destination file once before multi-process writing begins."""
    ssh = None
    sftp = None
    with connection_semaphore:
        try:
            with connect_lock:
                ssh = SSHClient()
                ssh.set_missing_host_key_policy(AutoAddPolicy())
                ssh.connect(
                    remote_host,
                    port=SSH_PORT,
                    username=username,
                    timeout=15,
                    banner_timeout=30,
                    auth_timeout=30,
                )
                time.sleep(0.15)

            sftp = ssh.open_sftp()
            with sftp.open(remote_path, "w") as f:
                f.truncate(0)
            return True
        except Exception as e:
            print(f"[Error] Failed initializing remote file {remote_path}: {e}")
            return False
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


def split_file_into_parts(file_path, num_parts):
    """Yields (part_number, offset, part_size) chunks for a local file."""
    file_size = os.path.getsize(file_path)
    part_size = file_size // num_parts
    for i in range(num_parts):
        offset = i * part_size
        if i == num_parts - 1:
            part_size = file_size - offset  # Remainder added to last chunk
        yield i, offset, part_size


def upload_part(
    remote_host,
    username,
    remote_path,
    local_path,
    num,
    offset,
    part_size,
    progress_queue,
    connection_semaphore,
    connect_lock,
):
    """Worker task uploading a specific chunk with connection pacing and robust exception catching."""
    attempt = 0
    while attempt < MAX_RETRIES:
        ssh = None
        sftp = None
        try:
            with connection_semaphore:
                with connect_lock:
                    ssh = SSHClient()
                    ssh.set_missing_host_key_policy(AutoAddPolicy())
                    ssh.connect(
                        remote_host,
                        port=SSH_PORT,
                        username=username,
                        timeout=20,
                        banner_timeout=30,
                        auth_timeout=30,
                    )
                    time.sleep(0.15)  # Pace connection handshakes

                sftp = ssh.open_sftp()

                with open(local_path, "rb") as local_file:
                    local_file.seek(offset)
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

            return True

        except Exception as e:
            attempt += 1
            jitter = random.uniform(1.0, 4.0)
            backoff_delay = (RETRY_DELAY * attempt) + jitter

            tqdm.write(
                f"[Part {num}] Connection dropped for {os.path.basename(local_path)} "
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


def process_file(
    file_path,
    remote_directory,
    remote_host,
    username,
    progress_queue,
    position,
    connection_semaphore,
    connect_lock,
):
    """Handles checking, staging, chunk execution, and verification for a single file."""
    file_name = os.path.basename(file_path)
    remote_file_path = os.path.join(remote_directory, file_name).replace("\\", "/")
    total_size = os.path.getsize(file_path)

    # 1. Skip if remote file matches local size
    remote_size = get_remote_file_size(remote_host, username, remote_file_path, connection_semaphore, connect_lock)
    if remote_size == total_size:
        tqdm.write(f"[Skip] {file_name} matches target size ({total_size} bytes).")
        return True

    # 2. Pre-create target file stub
    if not create_remote_file_stub(remote_host, username, remote_file_path, connection_semaphore, connect_lock):
        tqdm.write(f"[Fail] Could not initialize remote file stub for {file_name}.")
        return False

    # 3. Determine stream count & execute chunks cleanly
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
                    connect_lock,
                ),
            )
            processes.append(p)
            p.start()

        # Drain queue while processes run (non-blocking)
        while any(p.is_alive() for p in processes):
            while True:
                try:
                    bytes_added = progress_queue.get_nowait()
                    progress_bar.update(bytes_added)
                except Empty:
                    break
            time.sleep(0.05)

        # Drain remaining queue entries after processes end
        while True:
            try:
                bytes_added = progress_queue.get_nowait()
                progress_bar.update(bytes_added)
            except Empty:
                break

        for p in processes:
            p.join()

        if any(p.exitcode != 0 for p in processes):
            tqdm.write(f"[Error] One or more chunk processes failed for file: {file_name}")
            return False

    finally:
        progress_bar.clear()
        progress_bar.close()

    # 4. Final Verification
    final_remote_size = get_remote_file_size(remote_host, username, remote_file_path, connection_semaphore, connect_lock)
    if final_remote_size == total_size:
        return True
    else:
        tqdm.write(f"[Validation Failed] {file_name} size mismatch. Expected {total_size}, got {final_remote_size}.")
        return False


def process_directory(directory_path, remote_directory, remote_host, username, max_connections):
    """Walks directory, builds remote tree, and runs transfers with managed process pool."""
    manager = Manager()
    try:
        progress_queue = manager.Queue()
        connection_semaphore = manager.Semaphore(max_connections)
        connect_lock = manager.Lock()

        file_tasks = []

        print("Scanning directory tree and creating remote paths...")
        for root, _, files in os.walk(directory_path):
            relative_path = os.path.relpath(root, directory_path)
            current_remote_dir = (
                remote_directory
                if relative_path == "."
                else os.path.join(remote_directory, relative_path).replace("\\", "/")
            )

            ensure_remote_dir(remote_host, username, current_remote_dir, connection_semaphore, connect_lock)

            for file_name in files:
                file_path = os.path.join(root, file_name)
                if os.path.isfile(file_path):
                    file_tasks.append((file_path, current_remote_dir))

        print(f"Found {len(file_tasks)} files. Submitting to worker pool (Max Connections: {max_connections})...\n")

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
                    connect_lock,
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
                    tqdm.write(f"Unhandled exception processing {path}: {e}")
                    failed += 1

        # Flush streams and advance past all progress bar positions cleanly
        sys.stdout.flush()
        sys.stderr.flush()
        print("\n" * MAX_CONCURRENT_FILES)

        print("================ Transfer Summary ================")
        print(f"Total Files Handled: {len(file_tasks)}")
        print(f"Successfully Processed/Verified: {successful}")
        print(f"Failed Transfers: {failed}")
        print("==================================================")

    except KeyboardInterrupt:
        print("\n[Terminated] KeyboardInterrupt received. Aborting transfers...")
        sys.exit(1)
    finally:
        manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Multi-threaded chunked SFTP Directory Synchronization.")
    parser.add_argument("--directory_path", required=True, help="Path to local directory")
    parser.add_argument("--remote_host", required=True, help="SFTP server host/IP")
    parser.add_argument("--username", required=True, help="SFTP username")
    parser.add_argument("--remote_directory", required=True, help="SFTP base target directory")
    parser.add_argument(
        "--max_connections",
        type=int,
        default=MAX_CONNECTIONS,
        help="Max simultaneous connections (Default: 100)",
    )

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