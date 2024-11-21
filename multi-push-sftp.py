#!/usr/bin/env python3
import os
import argparse
import time
from paramiko import SSHClient, AutoAddPolicy, ssh_exception
from tqdm import tqdm
from multiprocessing import Process, Semaphore, Manager, current_process

# Configuration
USERNAME = "holadmin"
SSH_PORT = 22
CHUNKS_COUNT = 2  # Number of parts, "chunks" per file for simultaneous upload
#CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB for each chunk -- not used in favor of splitting each file into CHUNKS_COUNT parts
MAX_RETRIES = 3  # Maximum number of retries for failed SSH/SFTP connections
RETRY_DELAY = 30  # Delay in seconds between retries

# Semaphore to limit the number of concurrent processes
MAX_PROCESSES = 1
process_semaphore = Semaphore(MAX_PROCESSES)

# Track whether the remote file has been created
created = False


def split_file_into_parts(file_path, chunks_count):
    """Yield parts of a file, dividing it by the number of chunks."""
    file_size = os.path.getsize(file_path)
    part_size = file_size // chunks_count
    for i in range(chunks_count):
        offset = i * part_size
        if i == chunks_count - 1:
            part_size = file_size - offset  # Last part gets the remainder
        yield i, offset, part_size


def upload_part(remote_host, remote_path, local_path, num, offset, part_size, progress_queue):
    """
    Uploads a specific part of the file to the remote host using SFTP.
    Includes retry logic for SSH/SFTP connection failures.
    """
    global created
    attempt = 0

    while attempt < MAX_RETRIES:
        try:
            print(f"Process {num} attempting SSH connection (Attempt {attempt + 1})")
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(remote_host, port=SSH_PORT, username=USERNAME, banner_timeout=60, timeout=30)
            #time.sleep(30) # a throttle to keep from spamming the remote host -- at start, sometimes the host drops us for fear of DDoS
            # TODO: figure out where is the best place to put this so that we wait for a connection to occur before trying again and again

            sftp = ssh.open_sftp()
            with open(local_path, "rb") as local_file:
                local_file.seek(offset)  # Seek to the correct file offset in local file
                if not created:
                    with process_semaphore:
                        if not created:  # Double-check if another process created it
                            mode = "w"
                            created = True
                        else:
                            mode = "r+"
                else:
                    mode = "r+"

                with sftp.open(remote_path, mode) as remote_file:
                    remote_file.seek(offset)  # Seek to the correct file offset on remote
                    remote_file.set_pipelined(True)  # Enable pipelining for performance

                    size_uploaded = 0
                    while size_uploaded < part_size:
                        buffer_size = min(32768, part_size - size_uploaded)  # Limit buffer size
                        data = local_file.read(buffer_size)
                        if not data:
                            break  # End if no data is left to read

                        remote_file.write(data)  # Write the buffer to remote file
                        size_uploaded += len(data)

                        # Update progress
                        progress_queue.put(len(data))

        except ssh_exception.SSHException as e:
            attempt += 1
            print(f"Process {num} failed (Attempt {attempt}/{MAX_RETRIES}). Error: {e}")
            time.sleep(RETRY_DELAY)

        else:
            print(f"Process {num} completed successfully.")
            return  # Exit on success
                    
        finally:
            ssh.close() # always close the connection

    print(f"Process {num} failed after {MAX_RETRIES} attempts.")


def process_file(file_path, remote_directory, remote_host, username, progress_queue):
    """
    Processes a single file by dividing it into parts, transferring each part concurrently,
    and validating the file size after transfer.
    """
    file_name = os.path.basename(file_path)
    remote_file_path = os.path.join(remote_directory, file_name).replace("\\", "/")
    total_size = os.path.getsize(file_path)

    # Initialize progress bar
    with tqdm(total=total_size, desc=f"Transferring {file_name}", unit="B", unit_scale=True) as progress_bar:
        processes = []

        # Create processes for each part of the file
        for num, offset, part_size in split_file_into_parts(file_path, CHUNKS_COUNT):
            args = (remote_host, remote_file_path, file_path, num, offset, part_size, progress_queue)
            process = Process(target=upload_part, args=args)
            processes.append(process)
            process.start()

        # Update progress bar based on queue
        while any(process.is_alive() for process in processes):
            while not progress_queue.empty():
                bytes_transferred = progress_queue.get()
                progress_bar.update(bytes_transferred)
            time.sleep(0.1)  # Small delay to reduce CPU usage
            # TODO: a clean way to handle filling up the progress bar for smaller files when the transfer completes very quickly 
            # this can sometimes leave the bar at <100% even when a larger file has transferred successfully

        # Wait for all processes to finish
        for process in processes:
            process.join()

    # Validate file size
    if validate_file_size(remote_host, remote_file_path, file_path):
        print(f"File {file_name} successfully transferred and validated.")
    else:
        print(f"File {file_name} transfer validation failed.")


def process_directory(directory_path, remote_directory, remote_host, username):
    """Recursively processes all files in the specified directory with a limited number of processes."""
    file_processes = []
    manager = Manager()
    progress_queue = manager.Queue()

    for root, _, files in os.walk(directory_path):
        # Compute the remote directory path based on the current directory structure
        relative_path = os.path.relpath(root, directory_path)
        current_remote_dir = os.path.join(remote_directory, relative_path).replace("\\", "/")

        print(f'Current directory: {current_remote_dir}')

        for file_name in files:
            file_path = os.path.join(root, file_name)
            if os.path.isfile(file_path):
                # Use a separate process for each file
                process = Process(target=process_file, args=(file_path, current_remote_dir, remote_host, username, progress_queue))
                process.start()
                file_processes.append(process)

    # Wait for all file processes to finish
    for process in file_processes:
        process.join()

    print("Completed transferring all files in directory.")


def validate_file_size(remote_host, remote_path, local_path):
    """
    Validates that the remote file size matches the local file size.
    """
    local_size = os.path.getsize(local_path)
    try:
        ssh = SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        ssh.connect(remote_host, port=SSH_PORT, username=USERNAME)

        sftp = ssh.open_sftp()
        remote_size = sftp.stat(remote_path).st_size
        sftp.close()
        ssh.close()

        if local_size == remote_size:
            print(f"Validation successful: {local_path} ({local_size} bytes) matches {remote_path} ({remote_size} bytes).")
            return True
        else:
            print(f"Validation failed: {local_path} ({local_size} bytes) does not match {remote_path} ({remote_size} bytes).")
            return False

    except ssh_exception.SSHException as e:
        print(f"Validation failed due to SSHException: {e}")
        return False


def main():
    # Set up argument parser
    parser = argparse.ArgumentParser(description="Transfer files recursively to an SFTP server.")
    parser.add_argument("--directory_path", help="Path to the local directory to transfer")
    parser.add_argument("--remote_host", help="Remote SFTP server hostname")
    parser.add_argument("--username", help="Username for SFTP server")
    parser.add_argument("--remote_directory", help="Remote directory path on the SFTP server")

    # Parse arguments
    args = parser.parse_args()

    # Start processing the directory
    process_directory(args.directory_path, args.remote_directory, args.remote_host, args.username)


if __name__ == "__main__":
    main()
