# `gbserver` CLI reference

Quick reference for the `gbserver` command — the server-side CLI that runs the REST API, the build and
PR watchers, the build runner, the all-in-one standalone server, and admin tasks.

> **Audience:** operators running gbserver. For the client CLI that talks to a running server, see the
> [`gb` CLI reference](gb-cli-reference.md).

> Run `gbserver --help` or `gbserver <command> --help` for the exhaustive list of options. This page is
> the cheat sheet — it lists what's there and points at the source for the details.

## Top-level options

Global options on the `gbserver` group (before the subcommand):

| Option | Notes |
|--------|-------|
| `--log-level` | `debug`, `info` (default), `warning`, `error`, `critical`. |
| `--log-file <path>` | Also write logs to this file. |
| `--gb-admin-table-prefix <prefix>` | Prefix for the admin metadata table names (mainly for testing). |
| `--server-runtime-config <path>` | Path to a server runtime config file. |

Behaviour is driven largely by environment variables (see [below](#key-environment-variables)).

## Running the server

| Command | Purpose |
|---------|---------|
| `gbserver standalone [--host 0.0.0.0] [--port 8080] [--space-dir <dir>]` | All-in-one local server: REST API + BuildWatcher in one process. Forces `GB_ENVIRONMENT=STANDALONE` and applies standalone-friendly defaults (SQLite storage, thread build runner, API-key auth, embedded NATS). `--space-dir` defaults to the in-repo `configurations/spaces/local`. |
| `gbserver rest-server [--port 8080]` | Start just the REST API server (`/api/v1`). |
| `gbserver build-watch [--gh-token <t>] [--config <f>] [--watch/--no-watch]` | Watch for pending builds (from PRs or a config) and dispatch build runners. |
| `gbserver pr-watch [--gh-token <t>] [--config <f>] [--watch/--no-watch]` | Watch PRs for build configurations. Requires a GitHub token. |
| `gbserver build-runner (--build-id <id> \| --build-dir <dir>) [...]` | Execute a single build — either a `PENDING` build from storage (`--build-id`) or one loaded from a directory (`--build-dir`). `--build-id` and `--build-dir` are mutually exclusive. |

`build-runner` extras (directory mode): `--space-name`, `--space-config-uri`, `--target/-t` (repeatable),
`--username`, `--workspace-dir`, `--monitoring-interval`, `--create-pr`, `--enable-resume`, `--dry-run`.
The runner backend is chosen by `GBSERVER_DEFAULT_BUILDRUNNER_TYPE` (`job` / `process` / `thread`).

## Local builds

Run a build directly from a directory, without the watcher:

| Command | Purpose |
|---------|---------|
| `gbserver build run [BUILD_DIR] [--space-name <n> \| --space-config-uri <uri>] [-t TARGET]... [--dry-run]` | Run a build from `BUILD_DIR` (defaults to the current directory). |
| `gbserver build run-and-monitor [BUILD_DIR] [...]` | Same as `build run`, but streams all emitted events. Does not exit on its own — Ctrl+C to stop. |

Both accept `--cancel_on_error` and `--user-name`.

## Administration

| Command | Purpose |
|---------|---------|
| `gbserver add-users <users_file>` | Add users to spaces from a YAML file (`spaces: <name>: [{username, role}]`). |
| `gbserver create-spaces [--spaces-path <f>] [--clear] [--replace] [--force]` | Create the predefined spaces (registers them in the `gb_spaces` table). |
| `gbserver admin-tables --operation <op> [--dry-run]` | Repair admin metadata: `fail-zombie-builds`, `fix-zombie-targets`, `fix-zombie-steps`, `fail-pending-without-pr`. Prompts for confirmation. |

`rest-server-worker` is an internal pseudo-command used when running the REST server with multiple
workers; it is not invoked directly.

## Key environment variables

`gbserver` reads most of its configuration from `GBSERVER_*` env vars. For the full grouped reference,
config files, and the `GB_ENVIRONMENT` mechanism see [Configuration](../configuration/README.md); the
exhaustive source of truth is [`src/gbserver/types/constants.py`](../../src/gbserver/types/constants.py).
The most common:

| Variable | Purpose |
|----------|---------|
| `GB_ENVIRONMENT` | `DEV` / `STAGING` / `PROD` / `STANDALONE` — selects cluster, namespace, storage, and standalone defaults. |
| `GBSERVER_METADATA_STORAGE` | `sql` (default) or `sqlite` (standalone default). |
| `GBSERVER_DEFAULT_BUILDRUNNER_TYPE` | `job` (k8s), `process`, or `thread`. Set `thread` for local dev. |
| `GBSERVER_AUTH_MODE` | `github` (default), `apikey`, `ibmid`, or `multi`. Standalone defaults to `apikey`. |
| `GBSERVER_API_KEY` | Shared secret for API-key auth (standalone/remote). |
| `GBSERVER_GITHUB_TOKEN` | GitHub Enterprise token used by the watchers/runner. |
| `GBSERVER_NATS_EMBEDDED` | Start an embedded NATS server (standalone default `true`). |

See the [REST API docs](../rest-api/README.md#authentication) for the auth modes,
[Secrets](../secrets/README.md) for credential resolution, and [Spaces](../spaces/README.md) for how a
space is registered and referenced by name.

## Where commands live

`gbserver`'s root ([`src/gbserver/cli.py`](../../src/gbserver/cli.py)) discovers subcommands dynamically
from [`src/gbserver/commands/`](../../src/gbserver/commands/): a file `command_<name>.py` becomes the
`gbserver <name>` command (underscores → hyphens), exporting a `cli` Click command.

## See also

- [CLIs overview](README.md) — how `gb` and `gbserver` relate
- [`gb` CLI reference](gb-cli-reference.md) — the client CLI
- [Environment setup](../environments/setup/) · [Troubleshooting](../help/troubleshooting.md)
