# multi-push

**`multi-push`** is a high-performance, multi-process SFTP directory synchronization tool written in Python. It overcomes standard single-threaded SSH/SFTP speed limits—such as the TCP Bandwidth-Delay Product (BDP) bottleneck on high-latency WAN links—by splitting large files into parallel chunks and uploading multiple files simultaneously.

Designed for reliability and efficiency, `multi-push` includes strict global connection throttling to prevent overwhelming target SSH servers.

---

## Key Features

- **Dynamic Stream Scaling:** Automatically adjusts parallel stream counts based on file size:
  - **Small Files (< 50 MB):** 1 stream (minimizes SSH connection handshake overhead).
  - **Medium Files (50 MB – 1 GB):** 4 parallel chunk streams.
  - **Large Files (> 1 GB):** 16 parallel chunk streams.
- **Global Connection Throttling:** Enforces a configurable ceiling on simultaneous active SSH/SFTP connections (default: 100 max connections) using IPC semaphores.
- **Handshake Pacing & Burst Prevention:** Staggers new SSH connection attempts to prevent target server `sshd` rate-limiting drops (`MaxStartups` drops and `Errno 104 Connection reset by peer`).
- **Skip & Resume Verification:** Queries remote file sizes before transfers to skip existing files and verify integrity post-transfer.
- **Recursive Directory Staging:** Automatically builds missing nested directory structures on the target server (`mkdir -p` behavior).
- **Jittered Backoff & Retry:** Built-in retry mechanism using exponential backoff with randomized timing jitter to handle intermittent network drops cleanly.
- **Clean Terminal Progress UI:** Powered by `tqdm` with non-corrupting terminal updates and deadlock-free multiprocessing queue draining.

---

## Prerequisites

- **Python:** Version 3.8 or higher.
- **Remote Host:** Standard SFTP/SSH access enabled on the target host.

---

## Installation

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/doug-baer/multi-push.git
   cd multi-push
   ```

2. **Create and Activate a Virtual Environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## Usage

Run the script from your terminal by specifying local and remote directory paths and host authentication details:

```bash
python3 multi_push.py \
  --directory_path /path/to/local/data \
  --remote_host sftp.example.com \
  --username your_user \
  --remote_directory /path/to/remote/target
```

### Command Line Arguments

| Argument | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `--directory_path` | **Yes** | — | Local directory path to synchronize. |
| `--remote_host` | **Yes** | — | Hostname or IP address of the target SFTP server. |
| `--username` | **Yes** | — | SSH/SFTP username. |
| `--remote_directory` | **Yes** | — | Remote base target directory path. |
| `--max_connections` | No | `100` | Global maximum simultaneous SSH connections ceiling. |

### Example with Connection Throttling

To limit the tool to a lower connection limit (e.g., 30 max connections on more restrictive servers):

```bash
python3 multi_push.py \
  --directory_path ./my_dataset \
  --remote_host 192.168.1.50 \
  --username admin \
  --remote_directory /backups/dataset \
  --max_connections 30
```

---

## Server Optimization Tip (`sshd_config`)

If you have administrative access on the target host and plan to run high connection counts (`--max_connections 100`), ensure the remote host's OpenSSH daemon is configured to handle concurrent unauthenticated connection bursts.

Edit `/etc/ssh/sshd_config` on the remote server:

```text
MaxStartups 100:30:200
MaxSessions 100
```

Then restart SSH service:
```bash
sudo systemctl restart sshd
```

---

## License

Distributed under the MIT License. See `LICENSE` for details.
