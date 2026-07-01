# Config files

> **Audience:** operators running the server or the watchers. For env vars see
> [environment-variables.md](environment-variables.md); for the CLI options see the
> [`gbserver` CLI reference](../cli/gbserver-cli-reference.md).

Two optional YAML config files supplement the environment variables: a **server runtime config**
(per-environment defaults, global to `gbserver`) and a **watcher config** (which spaces the build/PR
watchers poll).

## Server runtime config (`--server-runtime-config`)

The global option `gbserver --server-runtime-config <path>` registers **per-environment defaults** from a
YAML file, loaded by [`gbserverenvconfig.py`](../../src/gbserver/types/gbserverenvconfig.py) into a
`GBEnvConfig`. It lets you define (or override) a deployment environment's defaults — the cluster,
namespace, SQL schema, Lakehouse binding, space git URI, feature flags, etc. — without editing code.

```yaml
env: MYENV                      # environment name this config defines
lakehouse_environment: STAGING  # Lakehouse env it binds to
default_sql_schema: my_schema
default_pod_namespace: my-namespace
public_space_git_uri: https://github.com/my-org/my-space
feature_flags:
  gbserver_build_events: true
```

These are **defaults**: an explicit environment variable still wins (precedence in the
[overview](README.md#how-configuration-works)). Select the environment with `GB_ENVIRONMENT` (see
[gb-environment.md](gb-environment.md)); the built-in DEV/STAGING/PROD/STANDALONE configs use the same
`GBEnvConfig` shape.

## Watcher config (`build-watch` / `pr-watch` `--config`)

`gbserver build-watch --config <path>` and `gbserver pr-watch --config <path>` take a YAML file
describing **which spaces to watch** and polling behaviour. The file is reloaded when it changes (unless
`--no-watch`).

**`build-watch`** — [`BuildWatcherConfig`](../../src/gbserver/types/buildwatcherconfig.py):

```yaml
spaces:                       # spaces to watch; empty/omitted = all spaces
  - name: public
  - name: my-space
monitoring_interval: 5        # poll/monitor interval, seconds (floored at the minimum)
lh_max_retries: 3             # Lakehouse retry attempts
buildrunner_type: job         # thread | process | job (defaults to GBSERVER_DEFAULT_BUILDRUNNER_TYPE)
gh_api_endpoint: https://api.github.com
workspace_dir: gbserverworkspace
watcher_workspace_dir: gbserverworkspace/gbserver-buildwatcher-workspace
```

**`pr-watch`** — [`PrWatcherConfig`](../../src/gbserver/types/prwatcherconfig.py): the same `spaces`,
`monitoring_interval`, and `lh_max_retries`, plus `validate_inputs_are_registered: true`.

An empty/omitted `spaces` list means **watch all registered spaces**. Space names refer to spaces
registered in gbserver (see [Spaces](../spaces/README.md#registering-and-referencing-a-space-by-name)).

### Asset stores directory

`build-watch --asset_stores_dir <dir>` loads asset-store definitions from a directory of
[`store.yaml`](../asset-stores/README.md#store-configuration-storeyaml) files
([`AssetStoreConfig`](../../src/gbserver/types/assetstoreconfig.py)).

## See also

- [Configuration overview](README.md) · [Environment variables](environment-variables.md) · [Deployment environments](gb-environment.md)
- [`gbserver` CLI reference](../cli/gbserver-cli-reference.md)
