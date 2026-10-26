#!/usr/bin/env python3

# Copyright LLM.build Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Self, Tuple
from urllib.parse import ParseResult, unquote, urlparse

import yaml

from gbcommon.uri.uri import URI
from gbserver.utils.logger import get_logger

GBSPACE_SCHEME = "gb"
SPACE_SCHEME = "space"
STEPS_PREFIX = "steps/"
STEP_FILE_NAME = "step.yaml"

logger = get_logger(__name__)


class SpaceURI(URI):

    _thread_local = threading.local()

    def __new__(self, uri: ParseResult, **kwargs: dict) -> Self:
        if not hasattr(SpaceURI._thread_local, "base_uris"):
            default_base_uris = ["file:"]
            SpaceURI._thread_local.base_uris = default_base_uris
            logger.warning(
                "the space base_uris have not been initialized. Setting it to: %s",
                default_base_uris,
            )
        if not hasattr(SpaceURI._thread_local, "space_secrets"):
            SpaceURI._thread_local.space_secrets = {}
            logger.warning("the space space_secrets have not been initialized.")
        uristr = uri.geturl()
        uri_suffix = uristr
        if uristr.startswith(GBSPACE_SCHEME):
            uri_suffix = uristr.removeprefix(GBSPACE_SCHEME + "://")
        elif uristr.startswith(SPACE_SCHEME):
            uri_suffix = uristr.removeprefix(SPACE_SCHEME + "://")
        SpaceURI._reject_parent_segments(uri_suffix, uristr)
        # Steps: only copies that serve the active env (class, ``subtypes``, and
        # a ``steps/`` dir owned by the env or one of its ancestors, or a base's
        # top-level ``steps/``) are candidates, and base_uris order decides
        # between them. Within one base: the space's root ``steps/<name>``, then
        # the deepest owning dir, fewest ``environment_configs`` keys, then path.
        # See _resolve_step_by_base_order.
        if uri_suffix.startswith(STEPS_PREFIX):
            chosen = SpaceURI._resolve_step_by_base_order(uri_suffix)
            if chosen is not None:
                # The chosen copy is final: a ``<rest>`` it lacks (or that
                # escapes it) is unresolvable, never taken from another copy.
                step = SpaceURI._step_uri_from_dir(*chosen)
                if step is None:
                    raise ValueError(f"Unresolvable space uri : {uristr}")
                return step  # type: ignore[return-value]
        # Fallback: env-agnostic, against the space's own base_uris in order.
        # Existence is checked via the resolved URI's own scheme-aware
        # ``exists()`` (so ``file://`` and ``git+ssh://`` bases both resolve, and
        # the resolved URI — possibly a git URI pulled later — is returned as-is).
        # For a `steps/<name>[/<rest>]` URI, `_fallback_steps_ok` additionally
        # enforces the `<rest>` containment guard and the `subtypes` restriction
        # against the base's local materialization (the local dir, or the reused
        # git clone), so the restriction is never bypassed by falling through to
        # the fallback.  Bases that can't be materialized locally are admitted
        # (as before sub-types existed).
        after = SpaceURI._steps_suffix(uri_suffix)
        for base_uri in SpaceURI._thread_local.base_uris:
            resolved = URI.get_uri(
                base_uri, "file", secrets=SpaceURI._thread_local.space_secrets
            )
            resolved.append_path(uri_suffix)
            if not resolved.exists():
                continue
            if after is None or SpaceURI._fallback_steps_ok(base_uri, after):
                return resolved  # type: ignore[return-value]
        raise ValueError(f"Unresolvable space uri : {uristr}")

    @staticmethod
    def _reject_parent_segments(uri_suffix: str, uristr: str) -> None:
        """Refuse a space URI whose path climbs with ``..``.

        Every lookup joins the suffix onto a base (the env-agnostic fallback
        verbatim, via append_path), so ``space://../../home/gbserver/.kube``
        would leave the space. Checked after percent-decoding, since ``%2E%2E`` is decoded later
        by the URI handlers (e.g. a git ``#subdirectory=`` via parse_qs).

        Args:
            uri_suffix: The URI with its ``space://``/``gb://`` scheme removed.
            uristr: The full URI, for the error message.

        Raises:
            ValueError: If any ``/``-separated segment of the decoded suffix is
                ``..`` (prefixed "Unresolvable space uri", like other failures).
        """
        decoded = unquote(uri_suffix)
        if ".." in decoded.replace("\\", "/").split("/"):
            raise ValueError(
                f"Unresolvable space uri : {uristr} ('..' path segments are not"
                " allowed)"
            )

    @staticmethod
    def _steps_suffix(uri_suffix: str) -> Optional[str]:
        """Return the path after the ``steps/`` prefix, or ``None``.

        This is the "is this a ``steps/`` lookup, and what follows the prefix"
        guard.  The returned ``after`` may span multiple path segments — a step's
        canonical name can be multi-segment (a step authored at
        ``steps/distill/foo`` is named ``distill/foo``), so the split into
        ``(name, rest)`` is deferred to a per-search-root probe
        (:meth:`_longest_step_in_root`) rather than a fixed first-segment split.

        Args:
            uri_suffix: The scheme-stripped URI suffix (e.g. ``steps/digit`` or
                ``steps/distill/foo/helm-charts``).

        Returns:
            The suffix with the leading ``steps/`` stripped, or ``None`` when the
            suffix is not a ``steps/`` lookup or carries no step name.
        """
        if not uri_suffix.startswith(STEPS_PREFIX):
            return None
        after = uri_suffix[len(STEPS_PREFIX) :]
        return after or None

    @staticmethod
    def _iter_name_rest_candidates(after: str) -> Iterator[Tuple[str, str]]:
        """Yield ``(name, rest)`` splits of ``after``, longest name first.

        A step's canonical name is its path relative to the enclosing ``steps/``
        ancestor and may span several segments.  This enumerates every prefix of
        ``after`` as a candidate ``name`` with the remainder as the sub-asset
        ``rest``, from the whole path (``rest=""``) down to the first segment —
        e.g. ``"a/b/c"`` → ``("a/b/c","")``, ``("a/b","c")``, ``("a","b/c")``.

        Callers pick the longest candidate whose directory actually holds a
        ``step.yaml`` (:meth:`_longest_step_in_root`), giving longest-prefix-wins
        identity for ``space://steps/<a>/<b>`` while leaving every existing
        single-segment URI unchanged (its only candidate is the whole suffix).

        Args:
            after: The URI suffix with the leading ``steps/`` already stripped.

        Yields:
            ``(name, rest)`` tuples longest→shortest; ``rest`` is ``""`` for the
            whole-path candidate.  Empty when ``after`` has no segments.
        """
        segments = [s for s in after.split("/") if s != ""]
        for count in range(len(segments), 0, -1):
            name = "/".join(segments[:count])
            rest = "/".join(segments[count:])
            yield name, rest

    @staticmethod
    def _longest_step_in_root(
        steps_root: Path, after: str
    ) -> Optional[Tuple[Path, str]]:
        """Find the longest step under ``steps_root`` prefixing ``after``.

        Walks the ``(name, rest)`` candidates of ``after`` longest→shortest
        (:meth:`_iter_name_rest_candidates`) and returns the first whose
        ``steps_root/<name>/step.yaml`` is a file — i.e. the most-nested step
        whose directory prefixes the URI.  A candidate whose ``name`` contains a
        ``..`` segment is skipped: ``..`` is never part of a canonical step name
        (it belongs to a crafted sub-asset ``rest`` and is caught by the
        containment guard downstream).

        This is a pure *existence* probe; the env gate is applied by the caller
        to the chosen step.  Keeping name selection orthogonal to the env gate
        makes a URI's identity deterministic *within one* ``steps/`` *root*: a
        present-but-env-excluded nested step is still selected here and the
        lookup then misses, rather than being silently demoted to
        ``outer-step + rest``.  This guarantee is per root only.

        Args:
            steps_root: The ``steps/`` directory to resolve names under (e.g.
                ``<space>/steps`` or ``<base>/steps``).
            after: The URI suffix with the leading ``steps/`` stripped.

        Returns:
            ``(step_dir, rest)`` for the longest matching step, or ``None`` when
            no prefix of ``after`` names a step under ``steps_root``.
        """
        for name, rest in SpaceURI._iter_name_rest_candidates(after):
            if ".." in name.split("/"):
                continue
            step_yaml = steps_root / name / STEP_FILE_NAME
            if step_yaml.is_file():
                return step_yaml.parent, rest
        return None

    @staticmethod
    def _step_uri_from_dir(step_dir: Path, rest: str) -> Optional[URI]:
        """Build a resolved file URI for a matched step dir plus ``rest`` suffix.

        The ``rest`` sub-asset path must stay within ``step_dir``: a ``rest`` that
        escapes the step dir (e.g. ``../../secret`` from a
        ``space://steps/<name>/../../secret`` URI) is rejected with ``None``.
        ``space://`` URIs come from trusted config so this is defence-in-depth,
        but the containment check is cheap insurance against a traversal.

        Args:
            step_dir: The directory containing the matched ``step.yaml``.
            rest: Sub-asset path appended to ``step_dir`` (empty for a bare URI).

        Returns:
            The resolved ``URI`` when the target exists and stays under
            ``step_dir``, else ``None``.
        """
        target = step_dir if not rest else step_dir / rest
        if rest:
            base = step_dir.resolve()
            resolved = target.resolve()
            if resolved != base and base not in resolved.parents:
                return None
        if not target.exists():
            return None
        return URI.get_uri(  # type: ignore[return-value]
            f"file://{target}",
            "file",
            secrets=SpaceURI._thread_local.space_secrets,
        )

    @staticmethod
    def _enclosing_base_boundary(env_path: Path) -> Path:
        """Return the deepest base_uri directory enclosing ``env_path``.

        The env dir's path relative to this boundary is what a ``steps/`` dir's
        owner is compared with (:meth:`_env_rel_parts`).  Falls back to
        ``env_path`` itself when no base_uri encloses it.  Git base_uris are
        materialized to their reused local clone via :meth:`_uri_to_local_path`,
        so a git-backed env is bounded correctly.

        Args:
            env_path: Absolute, resolved path of the active env's directory.

        Returns:
            The boundary directory.
        """
        boundary = env_path
        best_len = -1
        for base_uri in SpaceURI._thread_local.base_uris:
            base_path = SpaceURI._uri_to_local_path(base_uri)
            if base_path is None:
                continue
            base_path = base_path.resolve()
            if (env_path == base_path or base_path in env_path.parents) and len(
                str(base_path)
            ) > best_len:
                boundary = base_path
                best_len = len(str(base_path))
        return boundary

    @staticmethod
    def _matching_env_key(configs, env_class: str) -> Optional[str]:
        """Return the ``environment_configs`` key that matches ``env_class``.

        Env class names (``K8s``, ``Skypilot``, ...) and the ``environment_configs``
        keys authored in ``step.yaml`` don't always agree on case (e.g. a step
        keyed ``k8s`` for the ``K8s`` env class), so the match is by
        case-insensitive string equality.  An exact match is preferred; a
        case-insensitive match is the fallback.  The lookup is on the **key**
        only and ignores the entry's value, so a present-but-null entry
        (``{Skypilot:}``) still reports its key.

        Args:
            configs: The ``environment_configs`` mapping (any non-dict value,
                including ``None``, yields ``None``).
            env_class: Active env class name.

        Returns:
            The matching key, or ``None`` when no key matches.
        """
        if not isinstance(configs, dict):
            return None
        if env_class in configs:
            return env_class
        lowered = env_class.lower()
        for key in configs:
            if isinstance(key, str) and key.lower() == lowered:
                return key
        return None

    @staticmethod
    def _env_config_entry(data: dict, env_class: str):
        """Look up the value of ``environment_configs[<env_class>]``.

        Uses :meth:`_matching_env_key` for the case-insensitive key match.

        Args:
            data: Parsed ``step.yaml`` mapping.
            env_class: Active env class name.

        Returns:
            The matching ``environment_configs`` value, or ``None`` when no key
            matches **or the matched key's value is null**.  Because a null value
            is indistinguishable from an absent key here, callers testing whether
            the class is *declared* must use :meth:`_env_class_present`, not an
            ``is None`` check on this result.
        """
        configs = data.get("environment_configs")
        if not isinstance(configs, dict):
            return None
        key = SpaceURI._matching_env_key(configs, env_class)
        return configs[key] if key is not None else None

    @staticmethod
    def _env_class_present(data: dict, env_class: str) -> bool:
        """Return whether ``environment_configs`` *declares* ``env_class`` by key.

        Presence is decided by the key alone, so a class that is present but null
        (``environment_configs: {Skypilot:}`` — declared, hence admissible under
        the active ``Skypilot`` env) is distinguished from one that is genuinely
        absent (the step is scoped to other classes only).

        Args:
            data: Parsed ``step.yaml`` mapping.
            env_class: Active env class name.

        Returns:
            ``True`` when a key matches ``env_class`` (case-insensitively).
        """
        return (
            SpaceURI._matching_env_key(data.get("environment_configs"), env_class)
            is not None
        )

    @staticmethod
    def _subtype_ok(
        data: dict, env_class: Optional[str], env_subtype: Optional[str]
    ) -> bool:
        """Return whether a step's sub-type restriction admits the active env.

        Pure ``environment_configs`` logic (no class-presence requirement — that
        is the caller's concern).  For the active env's class:

        * no class entry, or an **empty** ``subtypes`` list → no restriction
          (universal): returns ``True``;
        * a **non-empty** ``subtypes`` list → returns ``True`` only when
          ``env_subtype`` is one of its entries (exact string equality).  An env
          with no sub-type therefore never satisfies a step that lists sub-types.

        A scalar ``subtypes: kubernetes`` is normalized to a single-element list
        so membership stays an exact match; a value that is neither a list nor a
        string is uninterpretable and treated as no restriction.

        When ``env_class`` is unknown there is no class context to filter on, so
        the step is admitted.

        Args:
            data: Parsed ``step.yaml`` mapping.
            env_class: Active env class name (e.g. ``"Skypilot"``), or ``None``.
            env_subtype: Active env sub-type (e.g. ``"kubernetes"``), or ``None``.
        """
        if not env_class:
            return True
        entry = SpaceURI._env_config_entry(data, env_class)
        if not isinstance(entry, dict):
            return True
        subtypes = entry.get("subtypes") or []
        if isinstance(subtypes, str):
            # A scalar `subtypes: kubernetes` is a single sub-type, not an
            # iterable of characters — wrap it so membership stays an exact
            # string match rather than a substring test (``"k" in "kubernetes"``).
            subtypes = [subtypes]
        if not isinstance(subtypes, list) or not subtypes:
            # Empty, or a non-list we can't interpret as sub-types → universal,
            # matching the module's "can't read the restriction → admit"
            # convention (see :meth:`_step_env_ok`).
            return True
        return env_subtype in subtypes

    @staticmethod
    def _env_ok(
        data: dict, env_class: Optional[str], env_subtype: Optional[str]
    ) -> bool:
        """Return whether a step's ``environment_configs`` admit the active env.

        This is the full env gate used by the space-root and fallback lookups: a
        two-stage check that first matches the env **class**, then (via
        :meth:`_subtype_ok`) the sub-type.

        * ``env_class`` unknown → ``True`` (no class context to filter on, as in
          :meth:`_subtype_ok`).
        * ``environment_configs`` whose keys do **not** include the active class
          → ``False``, including a step that declares no ``environment_configs``
          at all.  The basestep (``step_default.yaml``) is merged into the
          step's working dir at build time, but the launcher is picked from the
          step's own ``environment_configs``
          (``TargetStep._resolve_environment_and_launcher``), so such a step has
          no launcher under any env class, K8s and Lsf included, and the build
          fails with "No usable environment configs".  It is not a match.
        * otherwise (the class **key** is present) → delegate to
          :meth:`_subtype_ok`.

        Presence is decided by the class **key**, not its value: a present-but-null
        entry (``environment_configs: {Skypilot:}``) declares the class and so is
        admitted (:meth:`_subtype_ok` then treats the null entry as unrestricted).
        A value-based ``is None`` check would wrongly skip such a step, which is
        scoped to exactly the active class.

        Args:
            data: Parsed ``step.yaml`` mapping.
            env_class: Active env class name (e.g. ``"Skypilot"``), or ``None``.
            env_subtype: Active env sub-type (e.g. ``"kubernetes"``), or ``None``.
        """
        if not env_class:
            return True
        if not SpaceURI._env_class_present(data, env_class):
            # Declared for other env classes only, or for none → can't run here.
            return False
        return SpaceURI._subtype_ok(data, env_class, env_subtype)

    @staticmethod
    def _step_env_ok(
        step_yaml: Path, env_class: Optional[str], env_subtype: Optional[str]
    ) -> bool:
        """Parse ``step_yaml`` and apply :meth:`_env_ok`.

        Applies the class-presence + sub-type gate: a step's
        ``environment_configs`` must list the active env class (and satisfy its
        sub-type restriction) for it to be admitted.  A file that can't be
        read/parsed (or is absent, e.g. a bare dir under ``steps/``) carries no
        restriction we can evaluate, so it is admitted (directory-only match).

        Args:
            step_yaml: Path to the candidate ``step.yaml``.
            env_class: Active env class name, or ``None``.
            env_subtype: Active env sub-type, or ``None``.
        """
        try:
            with open(step_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return True
        if not isinstance(data, dict):
            return True
        return SpaceURI._env_ok(data, env_class, env_subtype)

    @staticmethod
    def _fallback_steps_ok(base_uri: str, after: str) -> bool:
        """Vet an env-agnostic fallback ``steps/<after>`` hit against ``base_uri``.

        The caller has already confirmed the target exists via the resolved
        URI's scheme-aware ``exists()``.  This applies the two guards that need
        the base materialized as a local directory:

        * **containment** — a ``<rest>`` that escapes ``<base>/steps/<name>``
          (e.g. ``../../secret``) is rejected, matching the ordered search;
        * **env** — the step's own ``step.yaml`` is read and its
          ``environment_configs`` must list the active env class (and satisfy
          its ``subtypes`` restriction, if any) when a class is active.

        The step name (and thus its ``step.yaml``) is the longest prefix of
        ``after`` whose dir under ``<base>/steps`` holds a ``step.yaml``
        (:meth:`_longest_step_in_root`), so a nested step (``steps/distill/foo``)
        is admitted as itself.  When no prefix names a step, this falls back to
        the single-segment split (a bare dir under ``steps/`` with no
        ``step.yaml`` is admitted by the env gate, as before multi-segment names
        existed).  Reading the step's *own* ``step.yaml`` (resolved from the
        selected ``<name>``, not from the possibly ``..``-displaced ``<rest>``)
        keeps the restriction from being bypassed by a crafted sub-asset path.
        The base is resolved to a local root via :meth:`_uri_to_local_path` (a
        ``file://`` dir, or the reused git clone — no re-clone).  A base that
        can't be materialized locally is admitted, preserving pre-sub-type
        behavior.

        Args:
            base_uri: The base URI the target resolved against.
            after: The URI suffix with the leading ``steps/`` stripped
                (e.g. ``digit`` or ``distill/foo/helm-charts``).

        Returns:
            ``True`` when the hit is admitted; ``False`` when ``rest`` escapes
            the step dir or the ``environment_configs`` restriction (class or
            sub-type) excludes the env.
        """
        root = SpaceURI._uri_to_local_path(base_uri)
        if root is None:
            return True
        steps_root = root / STEPS_PREFIX.rstrip("/")
        found = SpaceURI._longest_step_in_root(steps_root, after)
        if found is not None:
            step_dir, rest = found
        else:
            # No step.yaml at any prefix: treat the first segment as the name and
            # the remainder as rest (pre-multi-segment behavior — a bare dir
            # under steps/ is admitted by the env gate on a missing step.yaml).
            name, _, rest = after.partition("/")
            step_dir = steps_root / name
        if rest:
            base = step_dir.resolve()
            resolved = (step_dir / rest).resolve()
            if resolved != base and base not in resolved.parents:
                return False
        env_class = getattr(SpaceURI._thread_local, "current_env_class_name", None)
        env_subtype = getattr(SpaceURI._thread_local, "current_env_subtype", None)
        return SpaceURI._step_env_ok(step_dir / STEP_FILE_NAME, env_class, env_subtype)

    @staticmethod
    def _space_root_step(
        after: str, env_class: Optional[str], env_subtype: Optional[str]
    ) -> Optional[Tuple[Path, str]]:
        """Resolve ``steps/<after>`` against the space's own root (``base_uris[0]``).

        The space directory (the first base_uri — the space's own ``uristr``) is
        the most authoritative step source: a step it ships at
        ``<space>/steps/<name>`` overrides any copy inherited via
        ``base_uris[1:]`` (e.g. a published assets tree).  This lets a step
        being developed in its own space be exercised (by ``make test``) before it
        is published into an inherited tree.  The env gate is applied
        (:meth:`_step_env_ok`), so a space step that
        declares ``environment_configs`` without the active env class — or whose
        ``subtypes`` exclude the active env — is skipped and resolution falls
        through (it must never override a valid step with one that cannot run
        under the active environment).

        The step name is the longest prefix of ``after`` whose dir under
        ``<space>/steps`` holds a ``step.yaml`` (:meth:`_longest_step_in_root`),
        so a nested step (``steps/distill/foo``) the space ships is honored as
        itself.

        Args:
            after: The URI suffix with the leading ``steps/`` stripped.
            env_class: Active env class name (for the sub-type gate), or ``None``.
            env_subtype: Active env sub-type (for the sub-type gate), or ``None``.

        Returns:
            ``(step_dir, rest)`` when the space ships an admitting
            ``steps/<name>``, else ``None``.
        """
        base_uris = getattr(SpaceURI._thread_local, "base_uris", None)
        if not base_uris:
            return None
        space_root = SpaceURI._uri_to_local_path(base_uris[0])
        if space_root is None:
            return None
        found = SpaceURI._longest_step_in_root(space_root.resolve() / "steps", after)
        if found is None:
            return None
        step_dir, _ = found
        if not SpaceURI._step_env_ok(step_dir / STEP_FILE_NAME, env_class, env_subtype):
            return None
        return found

    @staticmethod
    def _resolve_step_by_base_order(uri_suffix: str) -> Optional[Tuple[Path, str]]:
        """Resolve ``space://steps/<after>`` with base_uris order as the priority.

        Only a step whose ``environment_configs`` admit the active env (its class
        and ``subtypes``; see :meth:`_step_env_ok`) is a candidate.  The bases are
        tried in order — the space's own dir (``base_uris[0]``) first — and the
        first base holding a candidate wins.  So a base listed earlier always
        outranks a later one, whatever the later copy's placement or specificity.

        For the space's own dir (with an env dir active) its root
        ``steps/<name>`` comes first (:meth:`_space_root_step`).  Otherwise the
        env-class glob over a base (:meth:`_env_class_matches`) supplies the
        copies that serve the env, ranked: the deepest owning dir first (an env's
        own ``steps/`` over its family's, over the base's top-level ``steps/``),
        then fewest ``environment_configs`` keys, then path.

        The glob's step name is pinned across all bases
        (:meth:`_longest_step_in_bases`) before the env gate, so an env-excluded
        nested step (``distill/foo``) is never demoted to ``distill`` + sub-asset
        ``foo``.  The pin (a search of every base) runs only once a base reaches
        the glob, so a space-root hit costs no base search.

        Args:
            uri_suffix: The scheme-stripped ``steps/<name>[/<rest>]`` suffix.

        Returns:
            ``(step_dir, rest)`` for the chosen copy (the caller resolves
            ``rest`` within it), or ``None`` when no base holds a candidate (the
            caller then falls back).
        """
        after = SpaceURI._steps_suffix(uri_suffix)
        if after is None:
            return None
        tl = SpaceURI._thread_local
        env_class: Optional[str] = getattr(tl, "current_env_class_name", None)
        env_subtype: Optional[str] = getattr(tl, "current_env_subtype", None)
        env_rel = SpaceURI._env_rel_parts()
        if env_rel is not None:
            hit = SpaceURI._space_root_step(after, env_class, env_subtype)
            if hit is not None:
                return hit
        if not env_class:
            return None
        bases = SpaceURI._step_search_bases()
        env_path = SpaceURI._active_env_path()
        pinned = SpaceURI._longest_step_in_bases(after, bases, env_path, env_rel)
        if pinned is None:
            return None
        name, rest = pinned
        for base_uri in bases:
            matches = SpaceURI._env_class_matches(
                base_uri=base_uri,
                name=name,
                env_class=env_class,
                env_subtype=env_subtype,
                env_path=env_path,
                env_rel=env_rel,
            )
            if matches:
                return min(matches)[3].parent, rest
        return None

    @staticmethod
    def _step_search_bases() -> List[str]:
        """Return the bases to search for steps, in priority order.

        That is base_uris, plus the active env's own dir when no base encloses
        it (an env loaded from an absolute ``file://`` path, or from a git repo
        not listed in base_uris).  Such a dir is searched as its own base, right
        after the space's own dir (``base_uris[0]``) and ahead of the listed
        bases and the builtins, so its ``<env-dir>/steps/<name>`` is found and
        overrides inherited copies.  Only the env dir itself is added, not its
        parents.

        Returns:
            The base URIs to search.
        """
        tl = SpaceURI._thread_local
        bases = list(tl.base_uris)
        env_path = SpaceURI._active_env_path()
        # The boundary is the env dir itself only when no base encloses it.
        if env_path is None or SpaceURI._enclosing_base_boundary(env_path) != env_path:
            return bases
        bases.insert(min(1, len(bases)), env_path.as_uri())
        return bases

    @staticmethod
    def _active_env_path() -> Optional[Path]:
        """Return the active env's directory as a resolved local path, or None.

        Reads ``current_env_dir_uri`` (set by :meth:`with_current_env` or
        :meth:`with_current_env_class_name`) and materializes it via
        :meth:`_uri_to_local_path`, so a git-backed env yields its reused clone.
        """
        env_dir_uri = getattr(SpaceURI._thread_local, "current_env_dir_uri", None)
        if not env_dir_uri:
            return None
        env_path = SpaceURI._uri_to_local_path(env_dir_uri)
        return env_path.resolve() if env_path is not None else None

    @staticmethod
    def _longest_step_in_bases(
        after: str,
        bases: List[str],
        env_path: Optional[Path],
        env_rel: Optional[Tuple[str, ...]],
    ) -> Optional[Tuple[str, str]]:
        """Pin the step name by ``step.yaml`` existence across all base_uris.

        The env-class glob's analogue of :meth:`_longest_step_in_root`: the glob
        has no single fixed ``steps/`` root (it recursively globs each base_uri), so
        this walks the ``(name, rest)`` candidates of ``after`` longest→shortest
        and returns the first whose ``steps/.../<name>/step.yaml`` exists under
        *some* base_uri.  The glob is **anchored to the enclosing ``steps/``** — a
        canonical name is a path relative to a ``steps/`` ancestor, so an
        unanchored ``<name>/step.yaml`` (``name`` is now multi-segment) could
        reach into sub-asset territory and let a stray ``step.yaml`` outside any
        ``steps/`` dir (e.g. a test fixture) hijack an existing sub-asset URI.
        The ``**`` between ``steps/`` and the name tolerates env-partition dirs
        there, as builtin steps live at ``steps/<env-class>/<name>/`` (e.g.
        ``src/gbserver/builtins/steps/skypilot/command/``); anchoring straight to
        ``steps/<name>`` made every builtin ``space://steps/<name>`` unresolvable.
        This keeps the pin consistent with the space-root and fallback lookups'
        explicit ``steps/`` root.
        Candidate names containing a ``..`` segment are skipped (``..`` is never
        part of a canonical name — it belongs to a crafted ``rest`` and is caught
        by the containment guard, and this also keeps ``..`` out of the glob).

        Only ``steps/`` folders the env can see count
        (:meth:`_visible_step_yamls`): a nested step that exists only in a
        sibling env's folder doesn't fix the name, so ``distill/foo`` there
        leaves the env's own ``distill`` step (with ``foo`` as a sub-file)
        reachable.  Otherwise this is an *existence* probe: the class gate is
        applied by the caller to the fixed name only, which is what stops a
        visible but class-excluded nested step from being demoted to
        ``outer-step + rest`` — matching the space-root and fallback lookups.

        Args:
            after: The URI suffix with the leading ``steps/`` stripped.
            bases: The bases to probe (:meth:`_step_search_bases`).
            env_path: The active env's dir, or ``None``.
            env_rel: The env dir's path in its innermost base (:meth:`_env_rel_parts`).

        Returns:
            ``(name, rest)`` for the longest prefix of ``after`` that names a
            step (``step.yaml`` present) under any base, else ``None``.
        """
        for name, rest in SpaceURI._iter_name_rest_candidates(after):
            if ".." in name.split("/"):
                continue
            for base_uri in bases:
                for _ in SpaceURI._visible_step_yamls(
                    base_uri, name, env_path, env_rel
                ):
                    return name, rest
        return None

    @staticmethod
    def _visible_step_yamls(
        base_uri: str,
        name: str,
        env_path: Optional[Path],
        env_rel: Optional[Tuple[str, ...]],
    ) -> Iterator[Tuple[Path, Path]]:
        """Yield ``(base_path, step_yaml)`` for each copy of ``name`` the env can see.

        Globs ``<base>/steps/**/<name>/step.yaml``, anchored to the enclosing
        ``steps/`` — a canonical name is relative to a ``steps/`` ancestor, so an
        unanchored ``<name>/step.yaml`` could let a stray ``step.yaml`` outside
        any ``steps/`` (e.g. a test fixture) hijack a sub-asset URI.  The ``**``
        tolerates env-partition dirs *between* ``steps/`` and the name, as
        builtin steps are laid out ``steps/<env-class>/<name>/``.  Each copy's
        ``steps/`` folder must serve the env (:meth:`_steps_dir_admits_env`),
        with the env's path measured from this base when it encloses the env.

        Args:
            base_uri: The base to glob.
            name: Candidate step name (may be multi-segment).
            env_path: The active env's dir, or ``None``.
            env_rel: The env dir's path in its innermost base.
        """
        base_path = SpaceURI._uri_to_local_path(base_uri)
        if base_path is None or not base_path.exists():
            return
        rel = SpaceURI._env_rel_in(base_path, env_path, env_rel)
        for cand in base_path.rglob(f"{STEPS_PREFIX}**/{name}/{STEP_FILE_NAME}"):
            if cand.is_file() and SpaceURI._steps_dir_admits_env(base_path, cand, rel):
                yield base_path, cand

    @staticmethod
    def _env_rel_in(
        base_path: Path, env_path: Optional[Path], env_rel: Optional[Tuple[str, ...]]
    ) -> Optional[Tuple[str, ...]]:
        """Return the env dir's path as seen from ``base_path``.

        When this base encloses the env, the path is measured from it, so nested
        bases agree: with ``[<repo>, <repo>/configurations/assets]`` the outer
        base sees ``configurations/assets/environments/skypilot/slurm`` and its
        ``configurations/assets/environments/skypilot/steps/`` serves the env.
        Otherwise (another repo) the path from the env's innermost base is used,
        so a family's steps in one repo serve that family's env from another.

        Args:
            base_path: The base's local directory.
            env_path: The active env's resolved dir, or ``None``.
            env_rel: The env dir's path in its innermost base, or ``None``.
        """
        if env_path is None:
            return env_rel
        root = base_path.resolve()
        if env_path == root or root in env_path.parents:
            return env_path.relative_to(root).parts
        return env_rel

    @staticmethod
    def _env_rel_parts() -> Optional[Tuple[str, ...]]:
        """Return the active env dir's path relative to the base enclosing it.

        E.g. ``("environments", "skypilot", "slurm", "bluevela")`` for an env
        loaded from ``<gb-test>/environments/skypilot/slurm/bluevela``, used by
        :meth:`_steps_dir_admits_env` to compare a ``steps/`` dir's owner with the
        env by path inside each repo, whichever repo each lives in.

        Returns:
            The relative path's parts, ``()`` when no base encloses the env dir,
            or ``None`` when no env dir is active.
        """
        env_path = SpaceURI._active_env_path()
        if env_path is None:
            return None
        boundary = SpaceURI._enclosing_base_boundary(env_path)
        return env_path.relative_to(boundary).parts

    @staticmethod
    def _steps_dir_admits_env(
        base_path: Path, step_yaml: Path, env_rel: Optional[Tuple[str, ...]]
    ) -> bool:
        """Return whether the ``steps/`` dir holding ``step_yaml`` serves the env.

        The dir that holds a ``steps/`` folder is its owner.  A base's top-level
        ``steps/`` (owner ``()``: the builtins, a generated step space, a shared
        step repo) serves any env.  A deeper one belongs to its owner dir: e.g.
        ``environments/skypilot/aws/steps/`` serves only envs at or below
        ``environments/skypilot/aws``, and ``environments/skypilot/steps/`` every
        skypilot env.  Paths are compared relative to each one's base, so a
        family's shared steps in one repo still serve that family's env from
        another repo, while a sibling env's copy never serves the active env.

        Args:
            base_path: The base's local directory.
            step_yaml: A ``step.yaml`` found under ``base_path/steps/...``.
            env_rel: :meth:`_env_rel_parts`; ``None`` when no env dir is known
                (e.g. the validator couldn't load it, or the env has no
                ``environment_asset``).  Then no env folder can be shown to own
                the env, so only top-level ``steps/`` folders serve: a sibling
                env's copy (``environments/skypilot/aws/steps/``) must never be
                picked just because the env is unknown.

        Returns:
            True when the owner is ``()``, or a prefix of a known ``env_rel``.
        """
        owner = SpaceURI._steps_owner(base_path, step_yaml)
        if not owner:
            return True
        return env_rel is not None and env_rel[: len(owner)] == owner

    @staticmethod
    def _steps_owner(base_path: Path, step_yaml: Path) -> Tuple[str, ...]:
        """Return the dir that owns the ``steps/`` folder holding ``step_yaml``.

        That is the path from ``base_path`` to the first ``steps`` segment, e.g.
        ``("environments", "skypilot", "aws")``, or ``()`` for the base's
        top-level ``steps/``.  The first ``steps`` is the owner: the glob anchors
        there, and a later one would be inside the step's own name or files.

        Args:
            base_path: The base's local directory.
            step_yaml: A ``step.yaml`` found under ``base_path/.../steps/...``.

        Returns:
            The owner's path parts relative to ``base_path``.
        """
        parts = step_yaml.relative_to(base_path).parts
        return parts[: parts.index(STEPS_PREFIX.rstrip("/"))]

    @staticmethod
    def _env_class_matches(
        *,
        base_uri: str,
        name: str,
        env_class: str,
        env_subtype: Optional[str],
        env_path: Optional[Path],
        env_rel: Optional[Tuple[str, ...]],
    ) -> List:
        """Collect env-class-matching ``step.yaml`` candidates for one name.

        Recursively globs one base_uri (a ``file://`` dir, or a git base via its
        reused local clone) for ``steps/.../<name>/step.yaml`` and keeps
        those whose ``environment_configs`` declare ``env_class`` (by key,
        case-insensitively) and whose per-class ``subtypes`` restriction admits
        ``env_subtype``.  The glob is anchored to the enclosing ``steps/`` (the
        canonical-name invariant), matching the name pin in
        :meth:`_longest_step_in_bases` and the space-root and fallback lookups'
        explicit ``steps/`` root; a bare ``<name>/step.yaml`` would admit a stray
        ``step.yaml`` outside any ``steps/`` dir.  The ``**`` tolerates
        env-partition dirs between ``steps/`` and the name — builtin steps live
        at ``steps/<env-class>/<name>/`` and this glob is what resolves them (it
        reads their per-class ``environment_configs``).

        Class-presence is by key, not value: a present-but-null entry
        (``{Skypilot:}``) is still declared for the active class and is kept,
        matching :meth:`_env_ok` (a value-based ``is None`` check would silently
        drop it, leaving the two gates disagreeing on such steps).

        Args:
            base_uri: The base to glob.
            name: Candidate step name (may be multi-segment, e.g. ``distill/foo``).
            env_class: Active env class name.
            env_subtype: Active env sub-type, or ``None``.
            env_path: The active env's dir, or ``None``.
            env_rel: The env dir's path in its innermost base
                (:meth:`_env_rel_parts`), computed once per lookup by the caller.

        Returns:
            ``(-owner_depth, specificity, path-str, path)`` tuples, so the
            smallest ranks first: owner depth is how many dirs deep the owning
            ``steps/`` sits (deeper = closer to the env), specificity the
            env_configs key count (fewer = more env-specific); possibly empty.
        """
        matches: List = []
        for base_path, cand in SpaceURI._visible_step_yamls(
            base_uri, name, env_path, env_rel
        ):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(data, dict):
                continue
            env_keys = list((data.get("environment_configs") or {}).keys())
            if SpaceURI._env_class_present(data, env_class) and SpaceURI._subtype_ok(
                data, env_class, env_subtype
            ):
                depth = SpaceURI._steps_owner(base_path, cand)
                matches.append((-len(depth), len(env_keys), str(cand), cand))
        return matches

    @staticmethod
    def _file_uri_to_path(base_uri: str) -> Optional[Path]:
        """Return the local filesystem path for a `file://` base URI, else None.

        Non-file schemes (git://, http://, etc.) and the bare ``file:`` form
        return None — those bases don't support glob.  Strips a single leading
        slash duplicate when both ``file://`` and an absolute path are present.
        """
        parsed = urlparse(base_uri)
        if parsed.scheme not in ("", "file"):
            return None
        # urlparse turns `file:///abs/path` into netloc='', path='/abs/path' and
        # `file:///` into the same.  `file:` (no slashes) yields path=''.
        path_str = (parsed.netloc or "") + (parsed.path or "")
        if not path_str:
            return None
        p = Path(path_str)
        return p if p.is_absolute() else None

    @staticmethod
    def _uri_to_local_path(uri_str: str) -> Optional[Path]:
        """Return a local filesystem path for a space URI, materializing git.

        Handles both ``base_uris`` entries and the active env's directory URI:

        * Local (``file://``) → its directory (via :meth:`_file_uri_to_path`).
        * Git (``git+ssh``/``git+https``/...) → the **already-cloned** local path
          via ``GitURI.get_path_in_repo_from_cache`` — the same thread-local
          cache the resolved URI's ``exists()``/env-asset load populates, so no
          second clone happens.  A ``#subdirectory=`` fragment is honored (the
          env dir URI carries one), so this returns the repo root for a bare
          repo base and ``<clone>/<subdir>`` for a sub-path.

        This lets the space-root lookup, the env-class-match glob and the
        fallback guards operate on local files for git-backed spaces.  Non-local, non-git URIs (or a git URI whose clone fails) return
        ``None`` — callers treat that as "can't inspect" and skip/admit.

        Args:
            uri_str: A ``base_uris`` entry or the active env's directory URI.

        Returns:
            A local ``Path``, or ``None`` when it can't be materialized locally.
        """
        local = SpaceURI._file_uri_to_path(uri_str)
        if local is not None:
            return local
        secrets = getattr(SpaceURI._thread_local, "space_secrets", None)
        resolved = URI.get_uri(uri_str, "file", secrets=secrets)
        get_path_in_repo = getattr(resolved, "get_path_in_repo_from_cache", None)
        if get_path_in_repo is None:
            return None
        try:
            return get_path_in_repo()
        except Exception:  # pylint: disable=broad-except
            # A clone failure (network/auth) is non-fatal here: fall back to
            # "can't inspect" so resolution degrades to path-existence only.
            return None

    @classmethod
    def set_baseuris(cls, base_uris: List[str], space_secrets: dict):
        cls._thread_local.space_secrets = space_secrets
        cls._thread_local.base_uris = base_uris

    @classmethod
    @contextmanager
    def _scope_thread_local(cls, **values) -> Iterator[None]:
        """Set thread-local attrs for the block, restoring prior values on exit.

        Each keyword sets ``cls._thread_local.<key>`` to its value; a falsy value
        clears the attr instead (treated as "unset").  Prior values are saved on
        enter and restored on exit so nested or sibling scopes in the same
        thread don't leak.

        Args:
            **values: Thread-local attribute names mapped to the value to scope.
        """
        saved = {k: getattr(cls._thread_local, k, None) for k in values}
        for key, value in values.items():
            setattr(cls._thread_local, key, value or None)
        try:
            yield
        finally:
            for key, prev in saved.items():
                if prev is None:
                    if hasattr(cls._thread_local, key):
                        delattr(cls._thread_local, key)
                else:
                    setattr(cls._thread_local, key, prev)

    @staticmethod
    def _read_env_subtype(environment) -> Optional[str]:
        """Return an environment's ``subtype``, or ``None``.

        Reads ``environment.config.subtype`` defensively so stand-in
        environments without a full ``EnvironmentConfig`` (e.g. some unit tests)
        simply contribute no sub-type.

        Args:
            environment: The active target's ``Environment`` instance (or None).
        """
        if environment is None:
            return None
        config = getattr(environment, "config", None)
        return getattr(config, "subtype", None)

    @classmethod
    @contextmanager
    def with_current_env_class_name(
        cls,
        env_class_name: Optional[str],
        env_dir_uri: Optional[str] = None,
        env_subtype: Optional[str] = None,
    ) -> Iterator[None]:
        """Scope the active env's step-discovery context from loose values.

        Used by code paths that resolve ``space://steps/<name>`` URIs without a
        full ``Environment`` instance available — notably the build-creation-time
        validator in :class:`gbserver.build.build.Build`, which knows each
        target's ``environment_uri`` (and can read its ``environment.yaml``) but
        doesn't instantiate the env.  Passing ``env_dir_uri`` and ``env_subtype``
        lets the env-dir ownership check and the sub-type filter run during
        validation, not just the bare env-class-match.

        Saves and restores any previous values so nested or sibling validation
        in the same thread doesn't leak.  Falsy values opt out of that field.

        Args:
            env_class_name: The env's class name (e.g. ``"K8s"``, ``"Skypilot"``).
            env_dir_uri: The env's directory URI, for the env-dir ownership check.
            env_subtype: The env's sub-type for the sub-type filter.
        """
        with cls._scope_thread_local(
            current_env_class_name=env_class_name,
            current_env_dir_uri=env_dir_uri,
            current_env_subtype=env_subtype,
        ):
            yield

    @classmethod
    @contextmanager
    def with_current_env(cls, environment) -> Iterator[None]:
        """Scope the active env's step-discovery context on the thread-local
        for the duration of the ``with`` block.

        Sets three thread-local fields used by ``SpaceURI.__new__`` when
        resolving ``space://steps/<name>`` URIs:

        * ``current_env_dir_uri``     ← ``environment.environment_dir_uri``
        * ``current_env_class_name``  ← ``environment.__class__.__name__``
        * ``current_env_subtype``     ← ``environment.config.subtype``

        Step lookups take, from the earliest base_uri holding one, a copy that
        serves this env: its ``environment_configs`` list the env's class (and
        ``subtypes`` admit its sub-type), and its ``steps/`` dir is the base's
        top-level one or is owned by ``<env-dir>`` or an ancestor of it, the
        deepest owner first.  Failing that, ``<base>/steps/<name>/`` is the
        env-agnostic fallback.

        Prior values are saved on enter and restored on exit so nested or
        sibling target processing in the same thread doesn't leak.

        Args:
            environment: The active target's ``Environment`` instance.  Reads
                ``environment_dir_uri``, the instance class, and the config's
                ``subtype``.
        """
        env_dir = getattr(environment, "environment_dir_uri", None)
        env_class = environment.__class__.__name__ if environment is not None else None
        subtype = SpaceURI._read_env_subtype(environment)
        with cls._scope_thread_local(
            current_env_dir_uri=env_dir,
            current_env_class_name=env_class,
            current_env_subtype=subtype,
        ):
            yield

    @staticmethod
    def get_supported_schemes() -> List[str]:
        """Return supported uri schemes as list"""
        return [GBSPACE_SCHEME, SPACE_SCHEME]

    def exists(self: Self, force: bool = False) -> bool:
        """# TODO: fix this"""
        return True  # TODO: fix this

    def is_accessible(self) -> bool:
        """# TODO: fix this"""
        return True  # TODO: fix this

    def pull(self: Self, dest: Path, force: bool = False) -> bool:
        """# TODO: fix this"""
        return True  # TODO: fix this

    def delete(self: Self) -> bool:
        raise NotImplementedError("SpaceURI delete is not implemented")
