"""Per-step job time-limit for SkyPilot launches.

Covers the two pure helpers (`_parse_duration_to_minutes`,
`_time_limit_overrides`) and the end-to-end resolution/precedence that lands
the limit in `sky.Resources(_cluster_config_overrides=...)`.
"""

import asyncio
import logging

import pytest

from gbserver.environment.skypilot import (
    Skypilot,
    _time_limit_overrides,
    _parse_duration_to_minutes,
)
from gbserver.types.environmentconfig import EnvironmentConfig

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Pure helper: _parse_duration_to_minutes
# ---------------------------------------------------------------------------
class TestParseDurationToMinutes:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (120, 120),
            ("240", 240),
            ("90m", 90),
            ("4h", 240),
            ("1d", 1440),
            ("1d6h", 1800),
            ("1d6h30m", 1830),
            ("  2h ", 120),
            ("2H", 120),  # case-insensitive
        ],
    )
    def test_valid_values_normalize_to_minutes(self, value, expected):
        assert _parse_duration_to_minutes(value) == expected

    @pytest.mark.parametrize(
        "value",
        [0, -5, "0", "-5", "", "   ", "abc", "1x", "1h30", "h", True, "1.5h"],
    )
    def test_invalid_values_raise(self, value):
        with pytest.raises(ValueError):
            _parse_duration_to_minutes(value)


# ---------------------------------------------------------------------------
# Pure helper: _time_limit_overrides
# ---------------------------------------------------------------------------
class TestTimeLimitOverrides:
    def test_slurm_emits_sbatch_time(self):
        assert _time_limit_overrides("slurm", 240) == {
            "slurm": {"sbatch_options": {"time": "240"}}
        }

    def test_none_minutes_is_empty(self):
        assert _time_limit_overrides("slurm", None) == {}

    @pytest.mark.parametrize("cloud", ["lsf", "aws", "kubernetes", "gcp"])
    def test_non_slurm_is_empty_and_warns(self, cloud, caplog):
        with caplog.at_level(logging.WARNING):
            assert _time_limit_overrides(cloud, 60) == {}
        assert any("no per-task time-limit" in r.message for r in caplog.records)

    def test_non_slurm_none_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _time_limit_overrides("lsf", None) == {}
        assert not caplog.records


# ---------------------------------------------------------------------------
# End-to-end resolution + precedence through the launch path
# ---------------------------------------------------------------------------
def _mock_sky():
    mock = MagicMock()
    mock.Resources = MagicMock(return_value=MagicMock())
    mock.Task = MagicMock(return_value=MagicMock())
    mock.launch = MagicMock(return_value="req-tl")
    mock.stream_and_get = MagicMock(return_value=(1, MagicMock()))
    return mock


def _make_env(config: dict) -> Skypilot:
    """Build a Skypilot env from a raw env-config dict."""
    return Skypilot(
        event_q=asyncio.Queue(),
        environment_config=EnvironmentConfig(
            name="test-tl", type="Skypilot", config=config
        ),
    )


async def _overrides_for(env: Skypilot, launch_id: str, **launch_kwargs):
    """Launch under mocked sky; return the _cluster_config_overrides kwarg."""
    mock_sky = _mock_sky()
    with (
        patch("gbserver.environment.skypilot.sky", mock_sky),
        patch("gbserver.environment.skypilot.HAS_SKYPILOT", True),
    ):
        env._get_launch_ready_event(launch_id)
        await env.launch_skypilot(launch_id=launch_id, **launch_kwargs)
    return mock_sky.Resources.call_args[1]["_cluster_config_overrides"]


class TestTimeLimitResolution:
    @pytest.mark.asyncio
    async def test_step_level_time_limit_reaches_sbatch(self):
        env = _make_env({"default_cloud": "slurm"})
        overrides = await _overrides_for(
            env,
            "tl-step",
            launcher_config={"run": "hostname", "resources": {}, "time_limit": "4h"},
            config={},
        )
        assert overrides["slurm"]["sbatch_options"]["time"] == "240"

    @pytest.mark.asyncio
    async def test_env_default_applies_when_step_unset(self):
        env = _make_env({"default_cloud": "slurm", "time_limit": 90})
        overrides = await _overrides_for(
            env,
            "tl-env",
            launcher_config={"run": "hostname", "resources": {}},
            config={},
        )
        assert overrides["slurm"]["sbatch_options"]["time"] == "90"

    @pytest.mark.asyncio
    async def test_build_config_beats_step_and_env(self):
        env = _make_env({"default_cloud": "slurm", "time_limit": "1d"})
        overrides = await _overrides_for(
            env,
            "tl-prec",
            launcher_config={"run": "hostname", "resources": {}, "time_limit": "4h"},
            config={"launcher_config": {"time_limit": "30m"}},
        )
        # build.yaml step config ("30m") wins over step.yaml ("4h") and env ("1d")
        assert overrides["slurm"]["sbatch_options"]["time"] == "30"

    @pytest.mark.asyncio
    async def test_no_time_limit_leaves_overrides_unset(self):
        env = _make_env({"default_cloud": "slurm"})
        overrides = await _overrides_for(
            env,
            "tl-none",
            launcher_config={"run": "hostname", "resources": {}},
            config={},
        )
        # No docker, no time limit -> no cluster_config_overrides at all.
        assert overrides is None

    @pytest.mark.asyncio
    async def test_lsf_time_limit_is_noop_no_override(self):
        env = _make_env({"default_cloud": "lsf", "cluster": "bluevela"})
        overrides = await _overrides_for(
            env,
            "tl-lsf",
            launcher_config={"run": "hostname", "resources": {}, "time_limit": "2h"},
            config={},
        )
        # LSF has no per-task channel: the field is a documented no-op.
        assert overrides is None

    @pytest.mark.asyncio
    async def test_time_limit_merges_with_docker_override(self):
        env = _make_env({"default_cloud": "slurm"})
        overrides = await _overrides_for(
            env,
            "tl-docker",
            launcher_config={
                "run": "hostname",
                "resources": {},
                "time_limit": "1h",
                "docker": {"run_options": ["--shm-size=1g"]},
            },
            config={},
        )
        assert overrides["slurm"]["sbatch_options"]["time"] == "60"
        assert overrides["docker"] == {"run_options": ["--shm-size=1g"]}

    @pytest.mark.asyncio
    async def test_malformed_time_limit_fails_launch(self):
        env = _make_env({"default_cloud": "slurm"})
        with pytest.raises(ValueError):
            await _overrides_for(
                env,
                "tl-bad",
                launcher_config={
                    "run": "hostname",
                    "resources": {},
                    "time_limit": "later",
                },
                config={},
            )
