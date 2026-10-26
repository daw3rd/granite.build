# Step resolution: routing steps to environments

When a target runs, every `space://steps/<name>` URI is resolved against the active environment.  The rule in one line: **only step copies that match the active environment are candidates, and `base_uris` order decides between them** — the space's own directory first, then the builtins, then the space's `base_uris` in the order listed.  The sections below describe where a step can live and how copies inside one base are ranked; [Resolution order](#resolution-order) puts it together.

## Space-root steps (highest priority)

The **space's own directory** — the first entry in `base_uris`, i.e. the space's own `uristr` — is the most authoritative step source.  A step the space ships at `<space>/steps/<name>/step.yaml` **overrides** any copy in a later base, including one co-located with the environment (the ancestor-walk below) or inherited from a published `configurations/assets` tree.

This is what lets a step be **developed and tested inside its own space before it is published** into an inherited assets tree.  A step's `space/` dir is re-rendered on every `make test`, so the co-located test exercises that freshly-rendered local copy — not the already-published copy reachable via `base_uris` — and publishing (`make publish-step`) is only needed once the test passes.

The override honors the same environment match (class and `subtypes`, below) as every other copy: a space-root step that doesn't match the active env is skipped, and resolution moves on to the next base.

> Practically this only changes resolution when the space itself ships a `steps/<name>`.  A consuming space (e.g. `configurations/spaces/local`) that ships no steps is unaffected — its steps still resolve through the ancestor-walk / env-class-match against the inherited assets tree.

> **Environments and asset stores follow the same order.**  `space://environments/<name>` and `space://assetstores/<name>` resolve directly against the `base_uris` chain in order, and the space's own directory is `base_uris[0]`, so a space's own copy overrides any inherited one.  See [Spaces and `space.yaml`](../spaces/README.md#base_uris-and-space-resolution).

## Co-located steps and the ancestor-walk

A step impl placed inside the env's own directory is auto-discovered whenever that env is the active target's env.  No `base_uris` change is needed.  The resolver then **walks up** parent directories, checking `<dir>/steps/<name>/step.yaml` at each level, and stops at (and including) the deepest `base_uri` that encloses the env dir — it never escapes above the base_uri subtree.  The match is **nearest-wins**: the env's own dir beats a parent, which beats a grandparent.

The walk ranks copies **within the base that holds the environment**: there it beats the env-class match (below), so a BlueVela env gets its own copy of a step even when a sibling env's copy sorts first.  It does not outrank a base listed earlier: a step in an earlier base wins over a co-located one.

This lets sibling environments under a common family directory share one step implementation without duplicating it into each env dir:

```
configurations/assets/environments/skypilot/
├── steps/
│   └── digit/step.yaml          # shared by ALL skypilot envs below
├── kubernetes/
│   ├── environment.yaml         # resolves ../steps/digit
│   └── steps/
│       └── digit/step.yaml      # OPTIONAL override — wins over ../steps/digit
└── slurm/
    └── environment.yaml         # also resolves ../steps/digit
```

Place a step at the level of the family whose members should share it (`skypilot/steps/` for all skypilot envs, `skypilot/lsf/steps/` for just the lsf endpoints).  Keep a per-endpoint override in the env's own `steps/` dir when one endpoint genuinely diverges.

## Restricting a shared step by sub-type (`subtype` / `subtypes`)

The ancestor-walk shares a step with **every** environment under its directory, and env-class-match (below) matches **every** environment of the same class.  When environments differ only in compute endpoint they often share one class (all skypilot endpoints are class `Skypilot`), so neither mechanism can, on its own, restrict a shared step to a subset of them.  An optional **sub-type** provides that discriminator:

- An `environment.yaml` may declare an optional `subtype` — any free-form string (no predefined set; the skypilot endpoints use `kubernetes`/`slurm`/`aws`/`lsf` by convention, mirroring `config.default_cloud`).
- A step's `environment_configs.<Class>` may declare an optional `subtypes` list.  **Empty (the default) = universal**, matching any env of that class (so builtins and general shared steps keep resolving everywhere).  A **non-empty** list restricts the step to environments whose `subtype` is one of the listed values (exact string match); an env with no `subtype` does not match such a step.

```yaml
# environments/skypilot/steps/digit/step.yaml — shared by kubernetes + slurm only
environment_configs:
  Skypilot:
    subtypes: [kubernetes, slurm]   # aws / lsf endpoints are excluded
    ...
```

So a single `skypilot/steps/digit` is resolved by `skypilot/kubernetes` and `skypilot/slurm` but is unresolvable for `skypilot/aws` and `skypilot/lsf/...`.  The filter is applied wherever a copy is found (space root, ancestor-walk, env-class match, and the env-agnostic fallback), so a copy excluded by sub-type is never a candidate, whichever base it is in.

## Env-class matching against existing `environment_configs`

The resolver can also pick a step variant based on the env's class name (`Bash`, `Docker`, `K8s`, `Lsf`, `Runpod`, `Skypilot`, ...) by reading each candidate `step.yaml`'s existing `environment_configs` keys.  No new field on `step.yaml` is required.

The resolver scans recursively under each base for any file at `<base>/steps/**/<name>/step.yaml`, parses each one, and keeps the candidates whose `environment_configs` contain the active env's class name.  The class-name match is **case-insensitive** (env class `K8s` matches an `environment_configs` key `k8s`).  Subdirectory naming is **conventional only** — the match is by file content, so step variants can live anywhere (the builtins use `steps/<env-class-lowercase>/<name>/`):

```
src/gbserver/builtins/steps/
├── k8s/s3push/step.yaml      # only environment_configs.K8s
├── lsf/s3push/step.yaml      # only environment_configs.Lsf
├── skypilot/s3push/step.yaml # only environment_configs.Skypilot
└── ...
```

When the active env class is `K8s`, the resolver picks `steps/k8s/s3push/step.yaml`; when `Lsf`, `steps/lsf/s3push/step.yaml`.  Bases are scanned in order and the first base with a match wins.  Within that base, the candidate with FEWER `environment_configs` keys wins (the most env-specific file beats a multi-env file that also lists the env), and lexicographic path is the secondary tie-break.

## Resolution order

For `space://steps/<name>` and an env of class `K8s` loaded from `<env-dir>`:

The bases are, in order: the space's own dir (`base_uris[0]`), the builtins (`src/gbserver/builtins`), then the space's `base_uris` as listed.  Only copies that match the active env — class `K8s` (case-insensitively) and the `subtypes` restriction — are candidates.  The resolver tries each base in that order and returns the first candidate it finds there:

1. In the space's own dir: its root `steps/<name>/step.yaml` (when an env is active).
2. In the base that encloses `<env-dir>`: walk `<env-dir>` → parents up to that base, nearest `steps/<name>/step.yaml` wins (when an env is active).
3. In any base: the recursive glob `<base>/steps/**/<name>/step.yaml`, most env-specific first, then by path (when the env class is known).

So a base listed earlier always outranks a later one, whatever the later copy's placement or specificity.  If no base has a candidate:

4. `<base>/steps/<name>/step.yaml` — env-agnostic fallback, bases in order.  Existence is checked via the resolved URI's own scheme-aware `exists()`, and the resolved (git or file) URI is returned as-is; the environment match and `<rest>` containment are still enforced against the base's local materialization.
5. unresolvable → `ValueError`.

**Testing a step against a space that has its own copy.** To make sure a locally rendered step wins over a published copy, list the space holding it ahead of the published tree in `base_uris`.

### Git-backed spaces

`base_uris` may be git URIs (`git+ssh://…`), not just local `file://` dirs.  All step lookups operate off a repo's **already-cloned** local copy (the thread-local git clone cache, reused — no re-clone), via a git-aware local-path resolver:

- The env-class-match glob and the env-agnostic fallback scan/inspect any git base's clone.
- The ancestor-walk resolves the env dir to its clone and walks within it, so co-located steps in the **same repo** (base_uri) as the environment resolve.  Steps in a *different* repo from the env live in a separate clone and are not reachable by the walk — use env-class-match or the fallback for those.
- The space-root check resolves `base_uris[0]` to its clone as well, so a git-backed space's own `steps/<name>` still takes priority.

A base that can't be materialized locally (unsupported scheme, or a clone failure) is treated as "can't inspect": the ordered search (steps 1–3) skips it and the fallback (step 4) admits by path existence only, matching pre-`subtypes` behavior.

Use the ancestor-walk (a shared `steps/` dir at the family level) for impls shared by environments under a common directory; add a `subtypes` list to restrict a shared step to specific endpoints of the same class; use env-class-match for splitting a multi-env step.yaml into per-env files (the builtins approach).

## Manual override via `base_uris`

A `space.yaml` can also explicitly `base_uri` into a specific env directory if you want its steps available regardless of which target runs:

```yaml
name: my-space
base_uris:
  - file://./environments/skypilot/kubernetes   # always check this env's steps
  - file://./../assets                          # plus the shared assets
```

Auto-discovery of the active env's dir still happens on top of this — listing it manually is rarely necessary.

## See also

- [Spaces and `space.yaml`](../spaces/README.md) — the `base_uris` chain these lookups walk.
- [Environments overview](README.md) — `environment.yaml` / `step.yaml` reference and the per-type pages.
- [`src/gbcommon/uri/space.py`](../../src/gbcommon/uri/space.py) — the `SpaceURI` resolver (`_resolve_step_by_base_order` and the fallback).
- [`src/gbserver/build/targetstep.py`](../../src/gbserver/build/targetstep.py) — scopes the active env on the resolver thread-local during step assimilation.
