#!/usr/bin/env python3
# Copyright LLM.build Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the environment.yaml per-step-type config helpers.

These cover slug extraction from a step URI, the `config.steps.<slug>` lookup
(including the Phase-1 rule that the `environment_configs` sibling is NOT part
of the top-level `config` subtree), and the base-seed precedence: the env
per-step defaults are the lowest layer, overridden by the step_default.yaml
config that is merged on top.
"""

import pytest

from gbserver.build.targetstep import (
    _env_step_config,
    _seed_step_config_with_env_defaults,
    _step_slug_from_uri,
)


@pytest.mark.parametrize(
    "step_uri, expected",
    [
        ("space://steps/hfpull", "hfpull"),
        ("space://steps/command", "command"),
        ("space://steps/hfpush/", "hfpush"),  # trailing slash tolerated
        ("hfpull", "hfpull"),                  # bare slug
        ("", ""),
        (None, ""),
    ],
)
def test_step_slug_from_uri(step_uri, expected):
    assert _step_slug_from_uri(step_uri) == expected


def test_env_step_config_returns_matching_entry():
    env_config = {"zone": "normal", "steps": {"hfpull": {"zone": "io"}}}
    assert _env_step_config(env_config, "hfpull") == {"zone": "io"}


def test_env_step_config_absent_type_is_empty():
    env_config = {"zone": "normal", "steps": {"hfpull": {"zone": "io"}}}
    assert _env_step_config(env_config, "command") == {}


def test_env_step_config_no_steps_key_is_empty():
    assert _env_step_config({"zone": "normal"}, "hfpull") == {}


def test_env_step_config_handles_none_and_nonmapping():
    assert _env_step_config(None, "hfpull") == {}
    assert _env_step_config({"steps": None}, "hfpull") == {}


def test_env_step_config_drops_environment_configs_sibling():
    # Phase 1: the environment_configs sibling belongs to subtree-2 (Phase 2)
    # and must NOT leak into the top-level config subtree.
    entry = {"zone": "io", "environment_configs": {"Skypilot": {"launchers": {}}}}
    env_config = {"steps": {"hfpull": entry}}
    assert _env_step_config(env_config, "hfpull") == {"zone": "io"}


def test_seed_env_is_base_step_default_overrides():
    # env per-step default is the base; step_default.yaml config overrides it.
    env_config = {"steps": {"hfpull": {"zone": "io", "keep": "env"}}}
    step_default_config = {"zone": "normal", "extra": "sd"}
    seeded = _seed_step_config_with_env_defaults(
        env_config, "space://steps/hfpull", step_default_config
    )
    assert seeded == {"zone": "normal", "keep": "env", "extra": "sd"}


def test_seed_no_env_entry_returns_step_default_unchanged():
    step_default_config = {"zone": "normal"}
    seeded = _seed_step_config_with_env_defaults(
        {"steps": {}}, "space://steps/command", step_default_config
    )
    assert seeded == {"zone": "normal"}
    # must be a distinct object (no aliasing of the caller's dict)
    assert seeded is not step_default_config
