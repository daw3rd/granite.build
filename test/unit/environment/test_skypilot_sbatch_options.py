"""Per-step SLURM ``sbatch_options`` passthrough for SkyPilot launches.

Covers the layered resolver (``Skypilot._resolve_sbatch_options``) and the
end-to-end merge that lands the directives in
``sky.Resources(_cluster_config_overrides={"slurm": {"sbatch_options": ...}})``,
including the SLURM-only gating (a no-op WARNING on other clouds) and
coexistence with a ``docker`` override.
"""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from gbserver.environment.skypilot import Skypilot
from gbserver.types.environmentconfig import EnvironmentConfig


def _mock_sky():
    """Build a MagicMock standing in for the ``sky`` module during a launch."""
    mock = MagicMock()
    mock.Resources = MagicMock(return_value=MagicMock())
    mock.Task = MagicMock(return_value=MagicMock())
    mock.launch = MagicMock(return_value="req-sbatch")
    mock.stream_and_get = MagicMock(return_value=(1, MagicMock()))
    return mock


def _make_env(config: dict) -> Skypilot:
    """Build a Skypilot environment from a raw environment.yaml ``config`` dict."""
    return Skypilot(
        event_q=asyncio.Queue(),
        environment_config=EnvironmentConfig(
            name="test-sbatch", type="Skypilot", config=config
        ),
    )


async def _overrides_for(env: Skypilot, launch_id: str, **launch_kwargs):
    """Launch under a mocked ``sky`` and return the ``_cluster_config_overrides``
    kwarg passed to ``sky.Resources`` (``None`` when no overrides applied)."""
    mock_sky = _mock_sky()
    with (
        patch("gbserver.environment.skypilot.sky", mock_sky),
        patch("gbserver.environment.skypilot.HAS_SKYPILOT", True),
    ):
        env._get_launch_ready_event(launch_id)
        await env.launch_skypilot(launch_id=launch_id, **launch_kwargs)
    return mock_sky.Resources.call_args[1]["_cluster_config_overrides"]


# ---------------------------------------------------------------------------
# Pure resolver: _resolve_sbatch_options (env < step < build, merged per key)
# ---------------------------------------------------------------------------
class TestResolveSbatchOptions:
    def test_empty_when_no_layer_sets_it(self):
        env = _make_env({"default_cloud": "slurm"})
        assert env._resolve_sbatch_options({}, {}) == {}

    def test_env_default_used_when_step_and_build_unset(self):
        env = _make_env({"default_cloud": "slurm", "sbatch_options": {"qos": "normal"}})
        assert env._resolve_sbatch_options({}, {}) == {"qos": "normal"}

    def test_per_key_merge_across_all_three_layers(self):
        env = _make_env({"default_cloud": "slurm", "sbatch_options": {"qos": "normal"}})
        merged = env._resolve_sbatch_options(
            {"sbatch_options": {"time": 60, "qos": "high"}},  # step.yaml
            {"launcher_config": {"sbatch_options": {"time": 30}}},  # build.yaml
        )
        # build wins on `time`; step's `qos` survives (build didn't set it);
        # env's `qos` is overridden by the step.
        assert merged == {"time": 30, "qos": "high"}

    def test_null_keys_coerced_to_empty(self):
        # A bare (present-but-null) YAML key parses to None; every layer must
        # tolerate it rather than crash the merge with a None operand.
        env = _make_env({"default_cloud": "slurm", "sbatch_options": None})
        assert (
            env._resolve_sbatch_options(
                {"sbatch_options": None},  # bare `sbatch_options:` in step.yaml
                {"launcher_config": None},  # bare `launcher_config:` in build.yaml
            )
            == {}
        )


# ---------------------------------------------------------------------------
# End-to-end merge into sky.Resources(_cluster_config_overrides=...)
# ---------------------------------------------------------------------------
class TestSbatchOptionsLaunch:
    @pytest.mark.asyncio
    async def test_step_level_reaches_sbatch_options(self):
        env = _make_env({"default_cloud": "slurm"})
        overrides = await _overrides_for(
            env,
            "sb-step",
            launcher_config={
                "run": "hostname",
                "resources": {},
                "sbatch_options": {"time": 240, "gres": "gpu:2"},
            },
            config={},
        )
        assert overrides["slurm"]["sbatch_options"] == {"time": 240, "gres": "gpu:2"}

    @pytest.mark.asyncio
    async def test_env_default_applies_when_step_unset(self):
        env = _make_env({"default_cloud": "slurm", "sbatch_options": {"qos": "normal"}})
        overrides = await _overrides_for(
            env,
            "sb-env",
            launcher_config={"run": "hostname", "resources": {}},
            config={},
        )
        assert overrides["slurm"]["sbatch_options"] == {"qos": "normal"}

    @pytest.mark.asyncio
    async def test_build_beats_step_beats_env_per_key(self):
        env = _make_env({"default_cloud": "slurm", "sbatch_options": {"qos": "normal"}})
        overrides = await _overrides_for(
            env,
            "sb-prec",
            launcher_config={
                "run": "hostname",
                "resources": {},
                "sbatch_options": {"time": 60, "qos": "high"},
            },
            config={"launcher_config": {"sbatch_options": {"time": 30}}},
        )
        assert overrides["slurm"]["sbatch_options"] == {"time": 30, "qos": "high"}

    @pytest.mark.asyncio
    async def test_merges_with_docker_without_clobbering(self):
        env = _make_env({"default_cloud": "slurm"})
        overrides = await _overrides_for(
            env,
            "sb-docker",
            launcher_config={
                "run": "hostname",
                "resources": {},
                "sbatch_options": {"time": 60},
                "docker": {"run_options": ["--shm-size=1g"]},
            },
            config={},
        )
        assert overrides["slurm"]["sbatch_options"] == {"time": 60}
        assert overrides["docker"] == {"run_options": ["--shm-size=1g"]}

    @pytest.mark.asyncio
    async def test_non_slurm_cloud_is_noop_and_warns(self, caplog):
        env = _make_env({"default_cloud": "kubernetes"})
        with caplog.at_level(logging.WARNING):
            overrides = await _overrides_for(
                env,
                "sb-k8s",
                launcher_config={
                    "run": "hostname",
                    "resources": {},
                    "sbatch_options": {"time": 60},
                },
                config={},
            )
        # No per-task channel off SLURM: the field is a documented no-op.
        assert overrides is None
        assert any("not SLURM" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_overrides_when_nothing_set(self):
        env = _make_env({"default_cloud": "slurm"})
        overrides = await _overrides_for(
            env,
            "sb-none",
            launcher_config={"run": "hostname", "resources": {}},
            config={},
        )
        # Neither docker nor sbatch_options -> no cluster_config_overrides at all.
        assert overrides is None
