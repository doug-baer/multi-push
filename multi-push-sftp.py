#!/usr/bin/env python3
import os
import argparse
import time
from paramiko import SSHClient, AutoAddPolicy, ssh_exception, SFTPError
from tqdm import tqdm
from multiprocessing import Process, Semaphore, Manager, current_process

# Configuration
SSH_PORT = 22
PARTS_PER_FILE = 4 # Number of processes to handle file parts
MAX_RETRIES = 5 # Maximum number of retries for failed SSH/SFTP connections
RETRY_DELAY = 60 # Delay in seconds between retries
MAX_PROCESSES = 2 # Limit on concurrent file-processing processes

# Semaphores to limit the number of concurrent processes
file_semaphore = Semaphore(1)
process_semaphore = Semaphore(MAX_PROCESSES)

# Track whether the remote file has been created
created = False

# TODO: Track total transfer success
successful_transfer = True

def split_file_into_parts(file_path, num_parts):
  """Yield parts of a file, dividing it by the number of threads."""
  file_size = os.path.getsize(file_path)
  part_size = file_size // num_parts
  for i in range(num_parts):
    offset = i * part_size
    if i == num_parts - 1:
      part_size = file_size - offset # Last part gets the remainder
    yield i, offset, part_size


def validate_file_size(remote_host, username, remote_path, local_path):
  """
  Validates that the remote file size matches the local file size.
  """
  local_size = os.path.getsize(local_path)
  try:
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(remote_host, port=SSH_PORT, username=username)

    sftp = ssh.open_sftp()
    remote_size = sftp.stat(remote_path).st_size

    if local_size == remote_size:
      #print(f"Validation successful: {local_path} ({local_size} bytes) matches {remote_path} ({remote_size} bytes).")
      return True
    else:
      print(f"Validation failed: {local_path} ({local_size} bytes) does not match {remote_path} ({remote_size} bytes).")
      return False

  except ssh_exception.SSHException as e:
    print(f"Validation failed due to SSHException: {e}")
    return False
  
  finally:
   sftp.close()
   ssh.close()


def remote_file_exists_and_matches_size(remote_host, username, remote_path, local_path):
  """
  Checks if a file exists on the remote server and matches the size of the local file.
  (duplicates function of validate_file_size ... can we collapse them into one function?)

  Args:
    remote_host (str): The hostname or IP address of the remote server.
    username (str): The username for the remote server.
    remote_path (str): The full path of the file on the remote server.
    local_path (str): The full path of the local file.

  Returns:
    bool: True if the file exists and matches the size, False otherwise.
  """
  local_size = os.path.getsize(local_path)
  try:
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(remote_host, port=22, username=username)

    # Open SFTP session
    sftp = ssh.open_sftp()

    # Check remote file size
    remote_size = sftp.stat(remote_path).st_size

    if remote_size == local_size:
      print(f"Remote file matches size: {remote_path} ({remote_size} bytes). Skipping transfer.")
      return True
    else:
      print(f"Remote file exists but size differs: {remote_path} ({remote_size} bytes vs {local_size} bytes).")
      return False

  except SFTPError:
    # print(f"Remote file does not exist: {remote_path}")
    return False
  except Exception as e:
    # print(f"Error while checking remote file: {e}")
    return False
  finally:
    sftp.close()
    ssh.close()


def upload_part(remote_host, username, remote_path, local_path, num, offset, part_size, progress_queue):
  """
  Uploads a specific part of the file to the remote host using SFTP.
  Opens a new SSH session for the part (reusing is not threadsafe)
  Includes retry logic for SSH/SFTP connection failures.
  """
  global created
  attempt = 0

  while attempt < MAX_RETRIES:
    try:
      # print(f"Process {num} attempting SSH connection (Attempt {attempt + 1})")
      ssh = SSHClient()
      ssh.set_missing_host_key_policy(AutoAddPolicy())
      ssh.connect(remote_host, port=SSH_PORT, username=username)

      sftp = ssh.open_sftp()
      with open(local_path, "rb") as local_file:
        local_file.seek(offset) # Seek to the correct file offset in local file
        if not created:
          with file_semaphore:
            if not created: # check whether the file exists
              mode = "w"
              created = True
            else:
              mode = "r+"
        else:
          mode = "r+"

        with sftp.open(remote_path, mode) as remote_file:
          remote_file.seek(offset) # Seek to the correct file offset on remote
          remote_file.set_pipelined(True) # Enable pipelining for performance

          size_uploaded = 0
          while size_uploaded < part_size:
            buffer_size = min(32768, part_size - size_uploaded) # Limit buffer size
            data = local_file.read(buffer_size)
            if not data:
              break # End if no data is left to read

            remote_file.write(data) # Write the buffer to remote file
            size_uploaded += len(data)

            # Update progress
            progress_queue.put(len(data))

    except ssh_exception.SSHException as e:
      attempt += 1
      print(f"Process {num} failed (Attempt {attempt}/{MAX_RETRIES}). Error: {e}")
      time.sleep(RETRY_DELAY)

    else:
      #print(f"Process {num} completed successfully.")
      return # Exit on success
          
    finally:
      sftp.close()
      ssh.close() # always close the connection

  print(f"Process {num} failed after {MAX_RETRIES} attempts.")


def process_file(file_path, remote_directory, remote_host, username, progress_queue, position, status_dict):
  """
  Processes a single file by dividing it into parts, transferring each part concurrently,
  and validating the file size after transfer. Skips files that already exist and match the local size.
  Records the status of the transfer in a shared dictionary.
  """
  file_name = os.path.basename(file_path)
  remote_file_path = os.path.join(remote_directory, file_name).replace("\\", "/")
  total_size = os.path.getsize(file_path)

  # Check if the file exists and matches size on the remote server
  if remote_file_exists_and_matches_size(remote_host, username, remote_file_path, file_path):
    status_dict[file_name] = "Skipped"
    print(f"{file_name} already exists at remote path and size matches. Skipping.")
    return # Skip transfer if the file already exists and matches size

  # Initialize a progress bar: each file gets a new bar
  with tqdm(
    total=total_size,
    desc=f"Pushing {file_name}",
    unit="B",
    unit_scale=True,
    position=position,
    leave=True,
  ) as progress_bar:
    processes = []

    # Create processes for each part of the file
    try:
      for num, offset, part_size in split_file_into_parts(file_path, PARTS_PER_FILE):
        args = (remote_host, username, remote_file_path, file_path, num, offset, part_size, progress_queue)
        process = Process(target=upload_part, args=args)
        processes.append(process)
        process.start()

      # Update progress bar based on queue
      transferred_bytes = 0
      while any(process.is_alive() for process in processes):
        while not progress_queue.empty():
          bytes_transferred = progress_queue.get()
          transferred_bytes += bytes_transferred
          progress_bar.update(bytes_transferred)
        time.sleep(0.1) # Small delay to reduce CPU usage

      # Wait for all processes to finish
      for process in processes:
        process.join()

      # Ensure the progress bar reaches 100%
      if transferred_bytes < total_size:
        progress_bar.update(total_size - transferred_bytes)

      # Validate file size
      if validate_file_size(remote_host, username, remote_file_path, file_path):
        status_dict[file_name] = "Transferred"
        print(f"File {file_name} successfully transferred and validated.")
      else:
        status_dict[file_name] = "Failed"
        print(f"File {file_name} transfer validation failed.")

    except Exception as e:
      status_dict[file_name] = "Failed"
      print(f"Error processing file {file_name}: {e}")


def process_file_WORKS(file_path, remote_directory, remote_host, username, progress_queue, position):
  """
  Processes a single file by dividing it into parts, transferring each part concurrently,
  and validating the file size after transfer.
  """
  file_name = os.path.basename(file_path)
  remote_file_path = os.path.join(remote_directory, file_name).replace("\\", "/")
  total_size = os.path.getsize(file_path)

  # Check if the file exists and matches size on the remote server
  if remote_file_exists_and_matches_size(remote_host, username, remote_file_path, file_path):
    print(f"{file_name} already exists at remote path and size matches. Skipping.")
    return  # Skip transfer if the file already exists and matches size

  # Initialize a progress bar: each file gets a new bar
  with tqdm(
    total=total_size,
    desc=f"Pushing {file_name}",
    unit="B",
    unit_scale=True,
    position=position,
    leave=True,
  ) as progress_bar:
    processes = []

    # Create processes for each part of the file
    for num, offset, part_size in split_file_into_parts(file_path, PARTS_PER_FILE):
      args = (remote_host, username, remote_file_path, file_path, num, offset, part_size, progress_queue)
      process = Process(target=upload_part, args=args)
      processes.append(process)
      process.start()

    # Update progress bar based on queue
    transferred_bytes = 0
    while any(process.is_alive() for process in processes):
      while not progress_queue.empty():
        bytes_transferred = progress_queue.get()
        transferred_bytes += bytes_transferred
        progress_bar.update(bytes_transferred)
      time.sleep(0.1) # Small delay to reduce CPU usage

    # Wait for all processes to finish
    for process in processes:
      process.join()

    # Ensure the progress bar reaches 100% ? (DB - Not sure this works, especially for small files)
    if transferred_bytes < total_size:
      progress_bar.update(total_size - transferred_bytes)

  # Validate file size
  if not validate_file_size(remote_host, username, remote_file_path, file_path):
    print(f"File {file_name} transfer validation failed.")
  #else:
  #  TODO: update the "files_transerred" table with the result (also, create the files_transferred table based on directory contents)
  #  print(f"File {file_name} successfully transferred and validated.")


def process_directory(directory_path, remote_directory, remote_host, username):
  """Recursively processes all files in the specified directory with a limited number of concurrent processes."""
  manager = Manager()
  progress_queue = manager.Queue()
  status_dict = manager.dict() # Shared dictionary for transfer statuses

  def process_file_wrapper(file_path, remote_directory, remote_host, username, progress_queue, position, status_dict):
    """Wrapper to acquire and release semaphore for file processing."""
    with process_semaphore: # Limit the number of concurrent processes
      process_file(file_path, remote_directory, remote_host, username, progress_queue, position, status_dict)

  file_processes = []
  position = 0 # Initialize position for progress bars

  for root, _, files in os.walk(directory_path):
    # Compute the remote directory path based on the current directory structure
    relative_path = os.path.relpath(root, directory_path)
    current_remote_dir = os.path.join(remote_directory, relative_path).replace("\\", "/")

    print(f'Current directory: {current_remote_dir}')

    for file_name in files:
      file_path = os.path.join(root, file_name)
      if os.path.isfile(file_path):
        # Start a process with a semaphore-wrapped function
        process = Process(
          target=process_file_wrapper,
          args=(file_path, current_remote_dir, remote_host, username, progress_queue, position, status_dict),
        )
        file_processes.append(process)
        process.start()
        position += 1 # Increment position for the next file's progress bar

  # Wait for all file processes to finish
  for process in file_processes:
    process.join()

  print("Completed transferring all files in directory.")
  summarize_transfer_status(status_dict)


def summarize_transfer_status(status_dict):
    """
    Prints a summary of the file transfer statuses.

    Args:
        status_dict (dict): A dictionary containing the status of each file transfer.
    """
    print("\nTransfer Summary:")
    print("-" * 50)
    for file_name, status in status_dict.items():
        print(f"{file_name}: {status}")
    print("-" * 50)


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
