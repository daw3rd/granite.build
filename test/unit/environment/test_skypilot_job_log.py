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

"""Persistent per-launch job logs and failure log tails for SkyPilot steps.

Covers two behaviors:

* the launcher tees each step's setup/run output to ``job.log`` in the native
  LSF layout under ``shared_workdir``
  (``llm-build-<id>/.../launch-<launch_id>/outputs/``), a directory teardown's
  per-run ``rm -rf`` never touches, so the log outlives the run workdir on
  success and failure alike and sits where LSF job logs are collected;
* a non-success terminal status carries the tail of the job log in the FAILED
  event and the raised exception, instead of a bare "Workload failed".
"""

import asyncio
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from libgbtest.environments.skypilot_mocks import _make_env, _mock_sky

from gbserver.environment import skypilot as skymod
from gbserver.types.buildevent import BuildEventType, EntityRunMetadata
from gbserver.types.errors import WorkloadFailedException
from gbserver.types.status import Status

WORKDIR = "/shared/builds/b-1/runs/tr-1"
LOG_DIR = "/shared/llm-build-b-1/target-t/target-run-tr-1/step-s/step-run-sr-1/launch-l-1/outputs"
RUN_METADATA = {
    "build_id": "b-1",
    "target_name": "t",
    "targetrun_id": "tr-1",
    "targetsteprun_id": "sr-1",
}


class TestJobLogDir:
    def test_log_dir_matches_native_lsf_layout(self):
        assert skymod._job_log_dir("/shared", RUN_METADATA, "s", "l-1") == LOG_DIR

    def test_log_dir_is_outside_the_reaped_run_workdir(self):
        log_dir = skymod._job_log_dir("/shared", RUN_METADATA, "s", "l-1")
        assert not log_dir.startswith(WORKDIR)

    def test_no_shared_workdir_means_no_log(self):
        assert skymod._job_log_dir(None, RUN_METADATA, "s", "l-1") is None

    @pytest.mark.parametrize("missing", list(RUN_METADATA))
    def test_missing_run_metadata_means_no_log(self, missing):
        meta = {**RUN_METADATA, missing: ""}
        assert skymod._job_log_dir("/shared", meta, "s", "l-1") is None

    def test_missing_step_name_means_no_log(self):
        assert skymod._job_log_dir("/shared", RUN_METADATA, "", "l-1") is None


def _run_wrapped(body, env):
    script = "set -eu\n" + skymod._with_job_log(LOG_DIR, body)
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


class TestWithJobLog:
    def test_no_log_dir_returns_body_unchanged(self):
        assert skymod._with_job_log(None, "hostname") == "hostname"

    def test_captures_stdout_and_stderr_and_keeps_exit_code(self, tmp_path):
        """Run the wrapped script for real: both streams land in the file, still
        reach stdout (SkyPilot's own log), and the body's exit code is kept."""
        log_dir = tmp_path / "logs" / "tr" / "launch"
        proc = _run_wrapped(
            "echo to-stdout\necho to-stderr >&2\nexit 3",
            {"GB_JOB_LOG_DIR": str(log_dir)},
        )
        assert proc.returncode == 3
        assert "to-stdout" in proc.stdout
        log = (log_dir / "job.log").read_text()
        assert "to-stdout" in log and "to-stderr" in log

    def test_set_e_failure_in_body_fails_the_script(self, tmp_path):
        proc = _run_wrapped(
            "false\necho not-reached", {"GB_JOB_LOG_DIR": str(tmp_path)}
        )
        assert proc.returncode == 1
        assert "not-reached" not in (tmp_path / "job.log").read_text()

    def test_body_heredoc_survives_the_wrap(self, tmp_path):
        body = "cat <<'EOF'\nfrom-heredoc\nEOF"
        proc = _run_wrapped(body, {"GB_JOB_LOG_DIR": str(tmp_path)})
        assert proc.returncode == 0
        assert "from-heredoc" in (tmp_path / "job.log").read_text()

    def test_setup_and_run_share_one_log_in_order(self, tmp_path):
        env = {"GB_JOB_LOG_DIR": str(tmp_path)}
        _run_wrapped("echo from-setup", env)
        _run_wrapped("echo from-run", env)
        assert (tmp_path / "job.log").read_text() == "from-setup\nfrom-run\n"

    def test_non_zero_rank_writes_its_own_file(self, tmp_path):
        _run_wrapped(
            "echo rank-one",
            {"GB_JOB_LOG_DIR": str(tmp_path), "SKYPILOT_NODE_RANK": "1"},
        )
        assert not (tmp_path / "job.log").exists()
        assert "rank-one" in (tmp_path / "job.rank1.log").read_text()

    def test_unwritable_log_dir_does_not_fail_the_step(self, tmp_path):
        """Logging is best-effort: a log dir that can't be created must not
        abort the body under ``set -eu``."""
        blocker = tmp_path / "file"
        blocker.write_text("")
        proc = _run_wrapped("echo body-ran", {"GB_JOB_LOG_DIR": str(blocker / "sub")})
        assert proc.returncode == 0
        assert "body-ran" in proc.stdout
        assert "cannot write the job log" in proc.stderr


class TestLaunchWiresJobLog:
    @pytest.mark.asyncio
    async def test_launch_exports_log_dir_and_tees_setup_and_run(self):
        env = _make_env({"default_cloud": "slurm", "shared_workdir": "/shared"})
        mock_sky = _mock_sky()
        with (
            patch("gbserver.environment.skypilot.sky", mock_sky),
            patch("gbserver.environment.skypilot.HAS_SKYPILOT", True),
        ):
            env._get_launch_ready_event("jl-1")
            await env.launch_skypilot(
                launch_id="jl-1",
                launcher_config={"run": "hostname", "setup": "echo prep"},
                config={},
                run_metadata=RUN_METADATA,
                step={"name": "s"},
                setup_config={"skypilot": {"build_workdir": WORKDIR}},
            )
        task_kwargs = mock_sky.Task.call_args[1]
        expected_dir = LOG_DIR.replace("launch-l-1", "launch-jl-1")
        assert task_kwargs["envs"]["GB_JOB_LOG_DIR"] == expected_dir
        assert "job.log" in task_kwargs["run"]
        assert "\nhostname\n" in task_kwargs["run"]
        assert "job.log" in task_kwargs["setup"]
        assert "\necho prep\n" in task_kwargs["setup"]
        assert env._job_log_dirs["jl-1"] == expected_dir

    @pytest.mark.asyncio
    async def test_launch_without_shared_workdir_has_no_log_dir(self):
        env = _make_env({"default_cloud": "slurm"})
        mock_sky = _mock_sky()
        with (
            patch("gbserver.environment.skypilot.sky", mock_sky),
            patch("gbserver.environment.skypilot.HAS_SKYPILOT", True),
        ):
            env._get_launch_ready_event("jl-2")
            await env.launch_skypilot(
                launch_id="jl-2", launcher_config={"run": "hostname"}, config={}
            )
        task_kwargs = mock_sky.Task.call_args[1]
        assert "GB_JOB_LOG_DIR" not in (task_kwargs["envs"] or {})
        assert task_kwargs["run"] == "set -eu\nhostname"


class TestReadLogTail:
    def test_reads_last_lines_of_downloaded_copy(self, tmp_path):
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text("".join(f"line {i}\n" for i in range(100)))
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail("gb-c", 7, max_lines=3)
        assert tail == "line 97\nline 98\nline 99"

    def test_falls_back_to_stream_file(self, tmp_path):
        (tmp_path / "gb-c").mkdir()
        (tmp_path / "gb-c" / "job-7.log").write_text("a\nb\n")
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            assert skymod._read_saved_log_tail("gb-c", 7) == "a\nb"

    def test_missing_log_returns_empty(self, tmp_path):
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            assert skymod._read_saved_log_tail("gb-c", 7) == ""

    def test_scrubs_url_credentials(self, tmp_path):
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text(
            "cloning https://x-access-token:ghp_secret@github.com/o/r\n"
        )
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail("gb-c", 7)
        assert "ghp_secret" not in tail

    def test_caps_total_characters(self, tmp_path):
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text("x" * 50_000 + "\nlast\n")
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail("gb-c", 7, max_chars=100)
        assert len(tail) <= 100
        assert tail.endswith("last")


def _failed_sky():
    sky = MagicMock()
    failed = MagicMock()
    failed.is_terminal.return_value = True
    failed.__str__ = lambda s: "JobStatus.FAILED"
    sky.job_status = MagicMock(return_value="req")
    sky.get = MagicMock(return_value={7: failed})
    return sky


class TestFailureCarriesLogTail:
    def _env(self, tmp_path):
        env = _make_env({"default_cloud": "slurm"})
        env._cluster_names["lf-1"] = "gb-c"
        env._job_ids["lf-1"] = 7
        env._job_log_dirs["lf-1"] = LOG_DIR
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text("starting\nTraceback: boom\n")
        return env

    @pytest.mark.asyncio
    async def test_failed_event_and_exception_include_log_tail(self, tmp_path):
        env = self._env(tmp_path)
        event_q: asyncio.Queue = asyncio.Queue()
        with (
            patch.object(skymod, "sky", _failed_sky()),
            patch.object(skymod, "HAS_SKYPILOT", True),
            patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)),
        ):
            with pytest.raises(WorkloadFailedException) as exc:
                await env._poll_skypilot_job(
                    launch_id="lf-1",
                    event_q=event_q,
                    entityrun_metadata=EntityRunMetadata(build_id="b"),
                    poll_interval=0,
                )
        assert "Traceback: boom" in str(exc.value)
        assert f"{LOG_DIR}/job.log" in str(exc.value)
        events = []
        while not event_q.empty():
            events.append(event_q.get_nowait())
        [fail] = [e for e in events if e.type == BuildEventType.WORKLOAD_STATUS_EVENT]
        assert fail.payload.status == Status.FAILED
        assert "Traceback: boom" in fail.payload.data["error"]
