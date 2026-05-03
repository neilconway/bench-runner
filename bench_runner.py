#!/usr/bin/env python3
"""Remote benchmark runner using Hetzner Cloud.

Spins up a cloud server, runs cargo bench on two branches,
compares results with critcmp, and tears down the server.

Requires: hcloud CLI configured with an active context, SSH key.
No third-party Python dependencies (stdlib only).
"""

import argparse
import base64
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time

SERVER_NAME_PREFIX = "bench-runner"
SERVER_LABEL = "purpose=bench-runner"
CLOUD_INIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud-init.yaml")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DEFAULT_SSH_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "hetzner-bench")
SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=20",
    "-o", "TCPKeepAlive=yes",
]

# Global for signal-handler cleanup
_cleanup_server_name = None
_keep_server = False
_ssh_key_path = None


_stage_num = 0
_stage_start = None
_run_start = None


def _format_elapsed(seconds):
    """Format elapsed seconds as a human-readable string."""
    minutes, secs = divmod(int(seconds), 60)
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def stage(msg):
    """Print a prominent stage banner with elapsed time for previous stage."""
    global _stage_num, _stage_start, _run_start
    now = time.time()
    if _stage_start is not None:
        elapsed = _format_elapsed(now - _stage_start)
        print(f"\n  [stage completed in {elapsed}]")
    _stage_num += 1
    if _run_start is None:
        _run_start = now
    _stage_start = now
    print(f"\n{'='*60}")
    print(f"  [{_stage_num}] {msg}")
    print(f"{'='*60}\n")


def ssh_opts():
    """Build SSH options list, including the identity file if set."""
    opts = list(SSH_BASE_OPTS)
    if _ssh_key_path:
        opts = ["-i", _ssh_key_path] + opts
    return opts


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
    args = ["ssh"] + ssh_opts() + [f"root@{ip}", command]
    if stream:
        return run_cmd(args, stream=True, check=check, timeout=timeout)
    return run_cmd(args, capture=True, check=check, timeout=timeout)


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
    if not os.path.exists(_ssh_key_path):
        sys.exit(f"Error: SSH key not found at {_ssh_key_path}")


def verify_branches_exist(repo, branches):
    """Check that each branch exists on the remote before provisioning."""
    missing = []
    for branch in branches:
        try:
            out = run_cmd(
                ["git", "ls-remote", "--heads", repo, branch],
                capture=True, check=True, timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            sys.exit(f"Error: Failed to query {repo} for branch '{branch}'.")
        if not out or not out.strip():
            missing.append(branch)
    if missing:
        sys.exit(
            f"Error: Branch(es) not found on {repo}: {', '.join(missing)}.\n"
            f"Push the branch to the remote (e.g. `git push <remote> {missing[0]}`) and retry."
        )


def get_ssh_key_name():
    """Get the name of an SSH key registered with Hetzner."""
    keys = hcloud_json(["ssh-key", "list"])
    if not keys:
        sys.exit(
            "Error: No SSH keys registered with Hetzner Cloud.\n"
            "Add one with: hcloud ssh-key create --name mykey --public-key-from-file ~/.ssh/id_ed25519.pub"
        )
    return keys[0]["name"]


def create_server(server_name, server_type, ssh_key_name, toolchain, location="nbg1"):
    """Provision a Hetzner server with cloud-init."""
    stage(f"Provisioning server ({server_type}, toolchain {toolchain})")

    # Render cloud-init template with the chosen toolchain
    with open(CLOUD_INIT_PATH) as f:
        user_data = f.read().replace("{{RUST_TOOLCHAIN}}", toolchain)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(user_data)
        tmp_path = tmp.name

    try:
        hcloud_cmd([
            "server", "create",
            "--name", server_name,
            "--type", server_type,
            "--image", "ubuntu-24.04",
            "--location", location,
            "--ssh-key", ssh_key_name,
            "--label", SERVER_LABEL,
            "--user-data-from-file", tmp_path,
        ], capture=False)
    finally:
        os.unlink(tmp_path)

    # Get the server IP by name
    servers = hcloud_json(["server", "list"])
    for s in servers:
        if s["name"] == server_name:
            ip = s["public_net"]["ipv4"]["ip"]
            print(f"Server created. IP: {ip}")
            return ip
    sys.exit("Error: Server was created but could not be found.")


def wait_for_ssh(ip, timeout=90):
    """Wait until SSH is available on the server."""
    stage("Waiting for server to become reachable")
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
    stage("Installing toolchain via cloud-init (~3-5 min)")
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
    stage("Quiescing system for benchmarking")
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


def run_remote_detached(ip, remote_cmd, name, poll_interval=10, max_consecutive_failures=20):
    """Run a long-running command on the server detached from the SSH session.

    The command is started via nohup with stdout+stderr to a log file and
    exit status to a done file. We then poll both files over short-lived SSH
    connections, so the remote process survives SSH drops, sshd crashes, or
    brief network outages. Streams new log output to local stdout.

    Raises subprocess.CalledProcessError if the remote command exits non-zero.
    """
    log_file = f"/root/{name}.log"
    done_file = f"/root/{name}.done"
    MARK = "__BENCHRUNNER_MARK__"
    DONE = "__BENCHRUNNER_DONE__"

    encoded = base64.b64encode(remote_cmd.encode()).decode()
    starter = (
        f"rm -f {log_file} {done_file}; "
        f"nohup bash -c 'bash -c \"$(echo {encoded} | base64 -d)\"; "
        f"echo $? > {done_file}' > {log_file} 2>&1 < /dev/null & disown"
    )
    ssh_cmd(ip, starter, check=True, timeout=30)

    offset = 0
    consecutive_failures = 0

    while True:
        probe = (
            f"tail -c +{offset + 1} {log_file} 2>/dev/null; "
            f"printf '\\n{MARK}\\n'; "
            f"wc -c < {log_file} 2>/dev/null | tr -d ' '; "
            f"printf '{DONE}\\n'; "
            f"test -f {done_file} && cat {done_file} || echo running"
        )
        try:
            out = ssh_cmd(ip, probe, check=True, timeout=60)
            consecutive_failures = 0
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                raise RuntimeError(
                    f"SSH polling failed {consecutive_failures} times in a row for {name}"
                )
            print(
                f"Warning: SSH poll {consecutive_failures}/{max_consecutive_failures} failed; retrying...",
                file=sys.stderr,
            )
            time.sleep(poll_interval * 2)
            continue

        mark_idx = out.find("\n" + MARK + "\n")
        done_idx = out.find(DONE + "\n")
        if mark_idx < 0 or done_idx < 0:
            time.sleep(poll_interval)
            continue

        new_log = out[:mark_idx]
        size_str = out[mark_idx + len("\n" + MARK + "\n"):done_idx].strip()
        status_str = out[done_idx + len(DONE + "\n"):].strip()

        if new_log:
            sys.stdout.write(new_log)
            sys.stdout.flush()

        try:
            offset = int(size_str)
        except ValueError:
            pass

        if status_str != "running":
            # Flush any bytes written between our tail and the done-file check
            try:
                final = ssh_cmd(
                    ip,
                    f"tail -c +{offset + 1} {log_file} 2>/dev/null || true",
                    check=False, timeout=30,
                )
                if final:
                    sys.stdout.write(final)
                    sys.stdout.flush()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass

            try:
                exit_code = int(status_str)
            except ValueError:
                exit_code = 1
            if exit_code != 0:
                raise subprocess.CalledProcessError(exit_code, f"remote:{name}")
            return

        time.sleep(poll_interval)


def run_benchmarks(ip, repo, base, target, bench, bench_filter, clickbench_data,
                   measurement_time=None, warm_up_time=None):
    """Clone the repo and run benchmarks on both branches."""
    stage(f"Cloning {repo}")
    ssh_cmd(ip, f"git clone --depth=50 --no-single-branch {repo} /root/repo", stream=True)

    if clickbench_data:
        stage("Downloading ClickBench partitioned dataset (~14 GB)")
        ssh_cmd(
            ip,
            "cd /root/repo && ./benchmarks/bench.sh data clickbench_partitioned",
            stream=True,
            timeout=3600,
        )

    filter_arg = f"'{bench_filter}'" if bench_filter else ""
    criterion_args = ""
    if measurement_time is not None:
        criterion_args += f" --measurement-time {measurement_time}"
    if warm_up_time is not None:
        criterion_args += f" --warm-up-time {warm_up_time}"

    # Run base branch benchmarks
    stage(f"Benchmarking base branch: {base}")
    ssh_cmd(ip, f"cd /root/repo && git checkout {base}", stream=True)
    bench_cmd_base = (
        f"cd /root/repo && source /root/.cargo/env && "
        f"cargo bench --bench {bench} -- --save-baseline base{criterion_args} {filter_arg}"
    )
    run_remote_detached(ip, bench_cmd_base, "bench-base")

    # Run target branch benchmarks
    stage(f"Benchmarking target branch: {target}")
    ssh_cmd(ip, f"cd /root/repo && git checkout {target}", stream=True)
    bench_cmd_target = (
        f"cd /root/repo && source /root/.cargo/env && "
        f"cargo bench --bench {bench} -- --save-baseline target{criterion_args} {filter_arg}"
    )
    run_remote_detached(ip, bench_cmd_target, "bench-target")

    # Compare results
    stage("Comparing results (critcmp)")
    comparison = ssh_cmd(
        ip,
        "cd /root/repo && source /root/.cargo/env && critcmp base target",
        stream=True,
    )
    return comparison


def fetch_results(ip, comparison_output):
    """Save benchmark comparison results locally."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    if comparison_output:
        result_path = os.path.join(RESULTS_DIR, f"comparison-{timestamp}.txt")
        with open(result_path, "w") as f:
            f.write(comparison_output)
        print(f"\nResults saved to {result_path}")


def destroy_server(name):
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
    global _cleanup_server_name, _keep_server, _ssh_key_path, _stage_num, _stage_start, _run_start
    _keep_server = args.keep
    _ssh_key_path = args.ssh_key
    _stage_num = 0
    _stage_start = None
    _run_start = None

    preflight_checks()
    verify_branches_exist(args.repo, [args.base, args.target])

    server_name = f"{SERVER_NAME_PREFIX}-{secrets.token_hex(4)}"
    _cleanup_server_name = server_name
    ssh_key_name = get_ssh_key_name()
    ip = create_server(server_name, args.server_type, ssh_key_name, args.toolchain, args.location)

    try:
        wait_for_ssh(ip)
        wait_for_cloud_init(ip)
        quiesce_system(ip)
        comparison = run_benchmarks(
            ip, args.repo, args.base, args.target, args.bench, args.filter, args.clickbench_data,
            measurement_time=args.measurement_time, warm_up_time=args.warm_up_time,
        )
        fetch_results(ip, comparison)
    finally:
        if args.keep:
            stage("Done (server kept)")
            print(f"Server '{server_name}' left running at {ip}.")
            print(f"Destroy manually with: python bench_runner.py destroy")
        else:
            stage("Tearing down server")
            destroy_server(server_name)
            _cleanup_server_name = None

    if _run_start is not None:
        total = _format_elapsed(time.time() - _run_start)
        print(f"\nTotal elapsed time: {total}")


def cmd_status(args):
    """Check if any benchmark servers are currently running."""
    servers = hcloud_json(["server", "list", "-l", SERVER_LABEL])
    if not servers:
        print("No benchmark servers are currently running.")
        return

    print(f"{len(servers)} benchmark server(s) running:")
    for s in servers:
        name = s.get("name", "unknown")
        status = s.get("status", "unknown")
        created = s.get("created", "unknown")
        server_type = s.get("server_type", {}).get("name", "unknown")
        ip = s.get("public_net", {}).get("ipv4", {}).get("ip", "unknown")
        print(f"\n  Name:    {name}")
        print(f"  Status:  {status}")
        print(f"  Type:    {server_type}")
        print(f"  IP:      {ip}")
        print(f"  Created: {created}")


def cmd_destroy(args):
    """Manually tear down benchmark servers."""
    servers = hcloud_json(["server", "list", "-l", SERVER_LABEL])
    if not servers:
        print("No benchmark servers found to destroy.")
        return
    for s in servers:
        destroy_server(s["name"])


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
    run_parser.add_argument(
        "--measurement-time", type=float, default=None,
        help="Criterion measurement time in seconds (default: 5)",
    )
    run_parser.add_argument(
        "--warm-up-time", type=float, default=None,
        help="Criterion warm-up time in seconds (default: 3)",
    )
    run_parser.add_argument(
        "--toolchain",
        default=os.environ.get("BENCH_RUNNER_TOOLCHAIN", "stable"),
        help="Rust toolchain (default: $BENCH_RUNNER_TOOLCHAIN or stable, e.g. nightly, 1.82.0)",
    )
    run_parser.add_argument(
        "--ssh-key",
        default=os.environ.get("BENCH_RUNNER_SSH_KEY", DEFAULT_SSH_KEY),
        help="Path to SSH private key (default: $BENCH_RUNNER_SSH_KEY or ~/.ssh/hetzner-bench)",
    )
    run_parser.add_argument("--server-type", default="cax41", help="Hetzner server type (default: cax41)")
    run_parser.add_argument("--location", default="nbg1", help="Hetzner location (default: nbg1)")
    run_parser.add_argument("--keep", action="store_true", help="Don't destroy server after run")
    run_parser.add_argument(
        "--clickbench-data",
        action="store_true",
        help="Download ClickBench partitioned dataset before benchmarking (required for sql_planner)",
    )
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
