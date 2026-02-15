#!/usr/bin/env python3
"""Remote benchmark runner using Hetzner Cloud.

Spins up a dedicated-vCPU server, runs cargo bench on two branches,
compares results with critcmp, and tears down the server.

Requires: hcloud CLI configured with an active context, SSH key.
No third-party Python dependencies (stdlib only).
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

SERVER_NAME = "bench-runner"
SERVER_LABEL = "purpose=bench-runner"
CLOUD_INIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud-init.yaml")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
]

# Global for signal-handler cleanup
_cleanup_server_name = None
_keep_server = False


def run_cmd(args, capture=False, check=True, stream=False, timeout=None):
    """Run a subprocess command with error handling."""
    if stream:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output_lines = []
        for line in iter(proc.stdout.readline, b""):
            decoded = line.decode("utf-8", errors="replace")
            sys.stdout.write(decoded)
            sys.stdout.flush()
            output_lines.append(decoded)
        proc.wait()
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, args)
        return "".join(output_lines)

    kwargs = {"capture_output": capture, "text": True, "timeout": timeout}
    if not capture:
        kwargs["stdout"] = None
        kwargs["stderr"] = None
    result = subprocess.run(args, check=check, **kwargs)
    return result.stdout if capture else None


def hcloud_cmd(args, capture=True, check=True):
    """Run an hcloud CLI command."""
    return run_cmd(["hcloud"] + args, capture=capture, check=check)


def hcloud_json(args):
    """Run an hcloud CLI command and parse JSON output."""
    out = hcloud_cmd(args + ["-o", "json"])
    if not out or not out.strip():
        return []
    return json.loads(out)


def ssh_cmd(ip, command, stream=False, check=True, timeout=None):
    """Run a command on the remote server via SSH."""
    args = ["ssh"] + SSH_OPTS + [f"root@{ip}", command]
    if stream:
        return run_cmd(args, stream=True, check=check, timeout=timeout)
    return run_cmd(args, capture=True, check=check, timeout=timeout)


def scp_from(ip, remote_path, local_path):
    """Copy a file from the remote server."""
    run_cmd(["scp"] + SSH_OPTS + [f"root@{ip}:{remote_path}", local_path])


def preflight_checks():
    """Validate that hcloud is installed, a context is active, and SSH key exists."""
    # Check hcloud installed
    try:
        run_cmd(["hcloud", "version"], capture=True)
    except FileNotFoundError:
        sys.exit("Error: hcloud CLI not found. Install with: brew install hcloud")

    # Check active context
    out = run_cmd(["hcloud", "context", "active"], capture=True, check=False)
    if not out or not out.strip():
        sys.exit("Error: No active hcloud context. Run: hcloud context create bench-runner")

    # Check SSH key exists
    home = os.path.expanduser("~")
    ssh_key_paths = [
        os.path.join(home, ".ssh", "id_ed25519"),
        os.path.join(home, ".ssh", "id_rsa"),
    ]
    if not any(os.path.exists(p) for p in ssh_key_paths):
        sys.exit(f"Error: No SSH key found at {' or '.join(ssh_key_paths)}")

    # Check no existing benchmark server (lock)
    servers = hcloud_json(["server", "list", "-l", SERVER_LABEL])
    if servers:
        s = servers[0]
        name = s.get("name", "unknown")
        created = s.get("created", "unknown")
        ip = s.get("public_net", {}).get("ipv4", {}).get("ip", "unknown")
        print(f"Error: A benchmark server already exists.", file=sys.stderr)
        print(f"  Name: {name}", file=sys.stderr)
        print(f"  IP: {ip}", file=sys.stderr)
        print(f"  Created: {created}", file=sys.stderr)
        print(f"Use 'python bench_runner.py destroy' to tear it down.", file=sys.stderr)
        sys.exit(1)


def get_ssh_key_name():
    """Get the name of an SSH key registered with Hetzner."""
    keys = hcloud_json(["ssh-key", "list"])
    if not keys:
        sys.exit(
            "Error: No SSH keys registered with Hetzner Cloud.\n"
            "Add one with: hcloud ssh-key create --name mykey --public-key-from-file ~/.ssh/id_ed25519.pub"
        )
    return keys[0]["name"]


def create_server(server_type, ssh_key_name):
    """Provision a Hetzner server with cloud-init."""
    print(f"Creating server '{SERVER_NAME}' (type: {server_type})...")
    hcloud_cmd([
        "server", "create",
        "--name", SERVER_NAME,
        "--type", server_type,
        "--image", "ubuntu-24.04",
        "--location", "fsn1",
        "--ssh-key", ssh_key_name,
        "--label", SERVER_LABEL,
        "--user-data-from-file", CLOUD_INIT_PATH,
    ], capture=False)

    # Get the server IP
    servers = hcloud_json(["server", "list", "-l", SERVER_LABEL])
    if not servers:
        sys.exit("Error: Server was created but could not be found.")
    ip = servers[0]["public_net"]["ipv4"]["ip"]
    print(f"Server created. IP: {ip}")
    return ip


def wait_for_ssh(ip, timeout=90):
    """Wait until SSH is available on the server."""
    print("Waiting for SSH connectivity...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ssh_cmd(ip, "echo ok", check=True, timeout=10)
            print("SSH is available.")
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(5)
    sys.exit(f"Error: SSH not available after {timeout}s.")


def wait_for_cloud_init(ip, timeout=600):
    """Wait until cloud-init has finished on the server."""
    print("Waiting for cloud-init to complete (this takes ~3-5 minutes)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = ssh_cmd(ip, "test -f /root/.cloud-init-complete && echo done", check=False)
            if result and "done" in result:
                print("Cloud-init complete.")
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(15)
    sys.exit(f"Error: Cloud-init did not complete within {timeout}s.")


def quiesce_system(ip):
    """Stop background services and drop caches before benchmarking."""
    print("Quiescing system...")
    commands = [
        "systemctl stop unattended-upgrades 2>/dev/null; true",
        "systemctl stop snapd 2>/dev/null; true",
        "systemctl stop apt-daily.timer 2>/dev/null; true",
        "systemctl stop apt-daily-upgrade.timer 2>/dev/null; true",
        # Wait for any in-flight apt/dpkg to finish
        "while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 2; done",
        "sync",
        "echo 3 > /proc/sys/vm/drop_caches",
        "sleep 2",
    ]
    for cmd in commands:
        ssh_cmd(ip, cmd, check=False)
    print("System quiesced.")


def run_benchmarks(ip, repo, base, target, bench, bench_filter):
    """Clone the repo and run benchmarks on both branches."""
    print(f"\n{'='*60}")
    print(f"Cloning {repo}...")
    print(f"{'='*60}")
    ssh_cmd(ip, f"git clone --depth=50 --no-single-branch {repo} /root/repo", stream=True)

    # Run base branch benchmarks
    print(f"\n{'='*60}")
    print(f"Benchmarking base branch: {base}")
    print(f"{'='*60}")
    ssh_cmd(ip, f"cd /root/repo && git checkout {base}", stream=True)

    filter_arg = f"'{bench_filter}'" if bench_filter else ""
    bench_cmd_base = (
        f"cd /root/repo && source /root/.cargo/env && "
        f"cargo bench --bench {bench} -- --save-baseline base {filter_arg}"
    )
    ssh_cmd(ip, bench_cmd_base, stream=True)

    # Run target branch benchmarks
    print(f"\n{'='*60}")
    print(f"Benchmarking target branch: {target}")
    print(f"{'='*60}")
    ssh_cmd(ip, f"cd /root/repo && git checkout {target}", stream=True)

    bench_cmd_target = (
        f"cd /root/repo && source /root/.cargo/env && "
        f"cargo bench --bench {bench} -- --save-baseline target {filter_arg}"
    )
    ssh_cmd(ip, bench_cmd_target, stream=True)

    # Compare results with critcmp
    print(f"\n{'='*60}")
    print("Comparing results (critcmp)")
    print(f"{'='*60}")
    comparison = ssh_cmd(
        ip,
        "cd /root/repo && source /root/.cargo/env && critcmp base target",
        stream=True,
    )
    return comparison


def fetch_results(ip):
    """Fetch benchmark result files from the remote server."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    # Export critcmp JSON
    try:
        json_output = ssh_cmd(
            ip,
            "cd /root/repo && source /root/.cargo/env && critcmp base target --export",
        )
        if json_output:
            result_path = os.path.join(RESULTS_DIR, f"comparison-{timestamp}.json")
            with open(result_path, "w") as f:
                f.write(json_output)
            print(f"\nResults saved to {result_path}")
    except subprocess.CalledProcessError:
        print("Warning: Could not export critcmp JSON results.", file=sys.stderr)


def destroy_server(name=SERVER_NAME):
    """Tear down the benchmark server."""
    print(f"Destroying server '{name}'...")
    try:
        hcloud_cmd(["server", "delete", name], capture=False, check=True)
        print("Server destroyed.")
    except subprocess.CalledProcessError:
        print(f"Warning: Failed to destroy server '{name}'.", file=sys.stderr)


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM by cleaning up the server."""
    global _cleanup_server_name, _keep_server
    sig_name = signal.Signals(signum).name
    print(f"\nReceived {sig_name}. Cleaning up...")
    if _cleanup_server_name and not _keep_server:
        destroy_server(_cleanup_server_name)
    sys.exit(1)


# --- CLI Commands ---

def cmd_run(args):
    """Run benchmarks on a remote server."""
    global _cleanup_server_name, _keep_server
    _keep_server = args.keep

    preflight_checks()

    ssh_key_name = get_ssh_key_name()
    ip = create_server(args.server_type, ssh_key_name)
    _cleanup_server_name = SERVER_NAME

    try:
        wait_for_ssh(ip)
        wait_for_cloud_init(ip)
        quiesce_system(ip)
        run_benchmarks(ip, args.repo, args.base, args.target, args.bench, args.filter)
        fetch_results(ip)
    finally:
        if args.keep:
            print(f"\n--keep specified. Server '{SERVER_NAME}' left running at {ip}.")
            print(f"Destroy it manually with: python bench_runner.py destroy")
        else:
            destroy_server(SERVER_NAME)
            _cleanup_server_name = None


def cmd_status(args):
    """Check if a benchmark server is currently running."""
    servers = hcloud_json(["server", "list", "-l", SERVER_LABEL])
    if not servers:
        print("No benchmark server is currently running.")
        return

    s = servers[0]
    name = s.get("name", "unknown")
    status = s.get("status", "unknown")
    created = s.get("created", "unknown")
    server_type = s.get("server_type", {}).get("name", "unknown")
    ip = s.get("public_net", {}).get("ipv4", {}).get("ip", "unknown")

    print(f"Benchmark server is running:")
    print(f"  Name:    {name}")
    print(f"  Status:  {status}")
    print(f"  Type:    {server_type}")
    print(f"  IP:      {ip}")
    print(f"  Created: {created}")


def cmd_destroy(args):
    """Manually tear down the benchmark server."""
    servers = hcloud_json(["server", "list", "-l", SERVER_LABEL])
    if not servers:
        print("No benchmark server found to destroy.")
        return
    destroy_server(servers[0]["name"])


def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    parser = argparse.ArgumentParser(
        description="Remote benchmark runner using Hetzner Cloud"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Run benchmarks on a remote server")
    run_parser.add_argument(
        "--repo",
        default=os.environ.get("BENCH_RUNNER_REPO"),
        help="Git repo URL (default: $BENCH_RUNNER_REPO)",
    )
    run_parser.add_argument("--base", default="main", help="Base branch (default: main)")
    run_parser.add_argument("--target", required=True, help="Target branch to compare")
    run_parser.add_argument("--bench", required=True, help="Benchmark name for cargo bench --bench")
    run_parser.add_argument("--filter", default=None, help="Criterion filter pattern")
    run_parser.add_argument("--server-type", default="ccx13", help="Hetzner server type (default: ccx13)")
    run_parser.add_argument("--keep", action="store_true", help="Don't destroy server after run")
    run_parser.set_defaults(func=cmd_run)

    # status
    status_parser = subparsers.add_parser("status", help="Check benchmark server status")
    status_parser.set_defaults(func=cmd_status)

    # destroy
    destroy_parser = subparsers.add_parser("destroy", help="Tear down benchmark server")
    destroy_parser.set_defaults(func=cmd_destroy)

    args = parser.parse_args()
    if args.command == "run" and not args.repo:
        run_parser.error("--repo is required (or set BENCH_RUNNER_REPO)")
    args.func(args)


if __name__ == "__main__":
    main()
