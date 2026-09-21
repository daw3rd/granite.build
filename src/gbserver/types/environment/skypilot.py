"""Types related to the SkyPilot environment."""

from typing import Optional, Union

from pydantic import Field

from gbserver.types.environment.environment import StepEnvConfig, StepSecretsConfig


class StepSkypilotConfig(StepEnvConfig):
    """Config specific to SkyPilot environments, extracted from step.yaml.

    Mirrors the per-cloud step-config section used by the other environments
    (``config.lsf`` / ``config.k8s``): parsed from the step's ``config.skypilot``
    block. ``secrets`` is the shared, declarative secret->env-var allow-list;
    SkyPilot injects *only* these declared secrets into the launched task (see
    ``Skypilot.get_launch_env_vars``), never the whole secret bag.

    ``time_limit`` is the per-step job wall-clock limit. It accepts an integer
    number of minutes or a duration string (``"90m"``, ``"4h"``, ``"1d"``,
    ``"1d6h30m"``). It is applied only where SkyPilot exposes a per-task runlimit
    override — currently SLURM, where it maps to ``#SBATCH --time`` (see
    ``Skypilot._launch_skypilot_inner`` / ``skypilot._time_limit_overrides``). On
    LSF/aws/kubernetes it is a no-op; for LSF set the limit at the environment
    level via ``cloud_config.lsf...bsub_options.W`` (minutes) instead. When unset
    here it falls back to the environment.yaml ``config.time_limit`` default.
    """

    secrets: StepSecretsConfig = Field(default_factory=StepSecretsConfig)
    resources: dict = Field(default_factory=dict)
    setup: str = ""
    run: str = ""
    envs: dict = Field(default_factory=dict)
    file_mounts: dict = Field(default_factory=dict)
    idle_minutes_to_autostop: int = 10
    image_id: Optional[str] = None
    time_limit: Optional[Union[int, str]] = None
