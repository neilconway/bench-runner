# bench-runner

Remote benchmark runner for comparing Rust benchmark results between two git branches.
Provisions an ephemeral Hetzner Cloud server, runs `cargo bench` on both branches, compares
results with `critcmp`, and tears down the server.

## Project structure

- `bench_runner.py` — Single-file Python script, stdlib only (no third-party deps)
- `cloud-init.yaml` — Server bootstrap template (Rust toolchain, build deps, critcmp).
  Contains a `{{RUST_TOOLCHAIN}}` placeholder replaced at runtime.
- `results/` — Local directory where `critcmp` comparison output is saved as
  timestamped `.txt` files

## Usage

### Primary workflow: compare a feature branch against main

```sh
python bench_runner.py run --target <branch> --bench <bench-name>
```

The `--repo` and `--toolchain` args come from environment variables (see below).

### Typical invocation for DataFusion

This tool is used to benchmark DataFusion feature branches (typically `neilc/optimize-*`)
against `main` on the `neilconway/datafusion` fork. Example:

```sh
python bench_runner.py run \
  --target neilc/optimize-array-has \
  --bench array_has
```

Other benchmarks that have been run locally and may be run remotely:
`trim`, `replace`, `initcap`, `reverse`, `ascii`, `pad`, `array_position`.
Bench names correspond to `[[bench]]` targets in the DataFusion workspace
(e.g., in `datafusion/datafusion-functions/` and `datafusion/datafusion-functions-nested/`).

### Other commands

```sh
python bench_runner.py status    # List running bench-runner servers
python bench_runner.py destroy   # Tear down all bench-runner servers
```

### Key CLI flags

| Flag | Default | Notes |
|------|---------|-------|
| `--repo` | `$BENCH_RUNNER_REPO` | Git repo URL (required) |
| `--base` | `main` | Base branch for comparison |
| `--target` | (required) | Feature branch to benchmark |
| `--bench` | (required) | Benchmark name (`cargo bench --bench <name>`) |
| `--filter` | none | Criterion filter to run a subset of benchmarks |
| `--toolchain` | `$BENCH_RUNNER_TOOLCHAIN` or `stable` | Rust toolchain version |
| `--ssh-key` | `$BENCH_RUNNER_SSH_KEY` or `~/.ssh/hetzner-bench` | SSH private key |
| `--server-type` | `cax41` | Hetzner server type (arm64 Ampere) |
| `--keep` | false | Leave server running after benchmarks |

## Environment variables

These should be set in the shell profile (e.g., `~/.zshenv`):

```sh
export BENCH_RUNNER_REPO=https://github.com/neilconway/datafusion
export BENCH_RUNNER_TOOLCHAIN=1.92.0
```

Optional: `BENCH_RUNNER_SSH_KEY` (defaults to `~/.ssh/hetzner-bench`).

## Prerequisites

- `hcloud` CLI installed and configured with an active context
- SSH keypair at `~/.ssh/hetzner-bench` (or path set via `--ssh-key`/`$BENCH_RUNNER_SSH_KEY`),
  with the public key registered in Hetzner Cloud
- Python 3 (no pip dependencies)

## Infrastructure

- **Cloud provider**: Hetzner Cloud
- **Default server type**: `cax31` (arm64 Ampere, EU/Nuremberg `nbg1`)
- **OS image**: Ubuntu 24.04
- **Server naming**: `bench-runner-<random-hex>` (supports concurrent runs)
- **Server label**: `purpose=bench-runner` (used by `status` and `destroy` commands)
- Servers are automatically destroyed on completion, Ctrl-C, or SIGTERM unless `--keep` is set

## Presenting results

When presenting benchmark results to the user, always include the full `critcmp` output rather than summarizing or abbreviating it.

## Design conventions

- **Zero Python dependencies**: stdlib only, shells out to `hcloud` and `ssh`
- **Idempotent cleanup**: signal handlers and try/finally ensure servers are destroyed
- **System quiescing**: stops background services and drops caches before benchmarking
- **Shallow clone**: uses `git clone --depth=50 --no-single-branch` for speed
- Keep changes minimal and focused; this is a small utility script
