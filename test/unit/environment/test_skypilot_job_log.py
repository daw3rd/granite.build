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


def _run_wrapped(body, env, phase="run", attempt=0):
    script = "set -eu\n" + skymod._with_job_log(LOG_DIR, body, phase, attempt)
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


class TestWithJobLog:
    def test_no_log_dir_returns_body_unchanged(self):
        assert skymod._with_job_log(None, "hostname", "run", 0) == "hostname"

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
        _run_wrapped("echo from-setup", env, phase="setup")
        _run_wrapped("echo from-run", env, phase="run")
        assert _body_lines(tmp_path / "job.log") == ["from-setup", "from-run"]

    def test_each_phase_starts_with_an_attempt_marker(self, tmp_path):
        """Retries reuse the launch's dir and append (as native LSF does), so
        every phase opens with a marker naming its attempt and phase."""
        env = {"GB_JOB_LOG_DIR": str(tmp_path)}
        _run_wrapped("echo first-setup", env, phase="setup", attempt=0)
        _run_wrapped("echo first-run", env, phase="run", attempt=0)
        _run_wrapped("echo second-run", env, phase="run", attempt=1)
        lines = (tmp_path / "job.log").read_text().splitlines()
        markers = [l for l in lines if l.startswith("=== granite.build:")]
        assert [m.split(" at ")[0] for m in markers] == [
            "=== granite.build: attempt 1 (setup)",
            "=== granite.build: attempt 1 (run)",
            "=== granite.build: attempt 2 (run)",
        ]
        # The marker sits right before its phase's output.
        assert lines.index("second-run") == lines.index(markers[2]) + 1

    def test_marker_is_not_in_the_skypilot_log_stream(self, tmp_path):
        """The marker goes only to job.log, so the artifact/event parsers that
        read SkyPilot's own log see exactly the step's output."""
        proc = _run_wrapped("echo body", {"GB_JOB_LOG_DIR": str(tmp_path)})
        assert proc.stdout == "body\n"

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


def _body_lines(path):
    """job.log lines without the granite.build attempt markers."""
    return [
        l
        for l in path.read_text().splitlines()
        if not l.startswith("=== granite.build:")
    ]


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
        assert "attempt 1 (run)" in task_kwargs["run"]
        assert "attempt 1 (setup)" in task_kwargs["setup"]

    @pytest.mark.asyncio
    async def test_launch_secret_values_resolve_declared_secrets(self):
        """The values masked in the tail are the launch's *declared* secrets,
        resolved from the kwargs stashed at launch (least-privilege)."""
        env = _make_env({"default_cloud": "slurm", "shared_workdir": "/shared"})
        env.secrets = {"MY_DB_PW": "declaredvalue123", "UNRELATED": "othervalue456"}
        mock_sky = _mock_sky()
        with (
            patch("gbserver.environment.skypilot.sky", mock_sky),
            patch("gbserver.environment.skypilot.HAS_SKYPILOT", True),
        ):
            env._get_launch_ready_event("jl-4")
            await env.launch_skypilot(
                launch_id="jl-4",
                launcher_config={"run": "hostname"},
                config={
                    "skypilot": {
                        "secrets": {
                            "secret_names_to_use_as_env_variable": [
                                {"env_name": "DB_PW", "secret_name": "MY_DB_PW"}
                            ]
                        }
                    }
                },
                run_metadata=RUN_METADATA,
                step={"name": "s"},
                setup_config={"skypilot": {"build_workdir": WORKDIR}},
            )
        assert env.get_launch_secret_values("jl-4") == ["declaredvalue123"]

    def test_unknown_launch_has_no_secret_values(self):
        env = _make_env({"default_cloud": "slurm"})
        assert env.get_launch_secret_values("nope") == []

    @pytest.mark.asyncio
    async def test_relaunch_marks_the_next_attempt(self):
        env = _make_env({"default_cloud": "slurm", "shared_workdir": "/shared"})
        env._relaunch_attempts["jl-3"] = 2  # set by retry_workload on the 2nd retry
        mock_sky = _mock_sky()
        with (
            patch("gbserver.environment.skypilot.sky", mock_sky),
            patch("gbserver.environment.skypilot.HAS_SKYPILOT", True),
        ):
            env._get_launch_ready_event("jl-3")
            await env.launch_skypilot(
                launch_id="jl-3",
                launcher_config={"run": "hostname"},
                config={},
                run_metadata=RUN_METADATA,
                step={"name": "s"},
                setup_config={"skypilot": {"build_workdir": WORKDIR}},
            )
        assert "attempt 3 (run)" in mock_sky.Task.call_args[1]["run"]

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
    def test_reads_only_the_end_of_a_large_file(self, tmp_path):
        """A multi-GB log must not be scanned line by line: only a bounded chunk
        from the end is read, however big the file is."""
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        path = job_dir / "run.log"
        with path.open("w") as f:
            f.write("filler line\n" * 200_000)  # ~2.4 MB
            f.write("Traceback: the real error\n")
        reads = []
        real_open = open

        def spy_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            real_read = handle.read

            def read(size=-1):
                data = real_read(size)
                reads.append(len(data))
                return data

            handle.read = read
            return handle

        with (
            patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)),
            patch("builtins.open", spy_open),
        ):
            tail = skymod._read_saved_log_tail("gb-c", 7, max_lines=3)
        assert tail.endswith("Traceback: the real error")
        assert sum(reads) <= skymod._LOG_TAIL_READ_BYTES
        assert path.stat().st_size > 10 * skymod._LOG_TAIL_READ_BYTES

    def test_tail_starts_on_a_line_boundary(self, tmp_path):
        """The chunk read from the end usually starts mid-line; that partial
        first line is dropped rather than shown as a fragment."""
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        # Big enough that the read starts mid-file, mid-line.
        (job_dir / "run.log").write_text(
            "".join(f"line-{i:06d}\n" for i in range(50_000))
        )
        assert (job_dir / "run.log").stat().st_size > skymod._LOG_TAIL_READ_BYTES
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail("gb-c", 7, max_lines=100)
        lines = [l for l in tail.splitlines() if l != "...[truncated]..."]
        assert all(l.startswith("line-") and len(l) == 11 for l in lines)
        assert lines[-1] == "line-049999"

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

    def test_masks_secrets_in_the_tail(self, tmp_path):
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text(
            "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz\nusing declaredvalue123\n"
        )
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail(
                "gb-c", 7, secret_values=["declaredvalue123"]
            )
        assert "hf_abcdefghijklmnopqrstuvwxyz" not in tail
        assert "declaredvalue123" not in tail

    def test_neutralizes_fences(self, tmp_path):
        """The tail is embedded in a ``` fence; a fence in the log can't close it."""
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text("before ``` after\n")
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail("gb-c", 7)
        assert "```" not in tail
        assert "before" in tail and "after" in tail

    def test_caps_total_size_with_truncation_marker(self, tmp_path):
        from gbserver.utils.redaction import LOG_TAIL_MAX_BYTES

        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text("x" * (LOG_TAIL_MAX_BYTES * 2) + "\nlast\n")
        with patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)):
            tail = skymod._read_saved_log_tail("gb-c", 7)
        assert tail.startswith("...[truncated]...")
        assert tail.endswith("last")


def _failed_sky(job_id=7):
    sky = MagicMock()
    failed = MagicMock()
    failed.is_terminal.return_value = True
    failed.__str__ = lambda s: "JobStatus.FAILED"
    sky.job_status = MagicMock(return_value="req")
    sky.get = MagicMock(return_value={job_id: failed})
    return sky


class TestFailureCarriesLogTail:
    def _env(self, tmp_path):
        env = _make_env({"default_cloud": "slurm"})
        env._cluster_names["lf-1"] = "gb-c"
        env._job_ids["lf-1"] = 7
        env._job_log_dirs["lf-1"] = LOG_DIR
        env.secrets = {"DB_PW": "declaredvalue123"}
        env._launch_kwargs["lf-1"] = {
            "config": {
                "skypilot": {
                    "secrets": {
                        "secret_names_to_use_as_env_variable": [{"env_name": "DB_PW"}]
                    }
                }
            }
        }
        job_dir = tmp_path / "gb-c" / "job-7"
        job_dir.mkdir(parents=True)
        (job_dir / "run.log").write_text(
            "starting with declaredvalue123\nTraceback: boom\n"
        )
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
        assert "declaredvalue123" not in str(exc.value)
        # The tail is SkyPilot's log of this attempt, not job.log: say so.
        assert "Last lines of SkyPilot's log for this attempt" in str(exc.value)
        assert f"{LOG_DIR}/job.log" in str(exc.value)
        events = []
        while not event_q.empty():
            events.append(event_q.get_nowait())
        [fail] = [e for e in events if e.type == BuildEventType.WORKLOAD_STATUS_EVENT]
        assert fail.payload.status == Status.FAILED
        assert "Traceback: boom" in fail.payload.data["error"]
        assert "declaredvalue123" not in fail.payload.data["error"]

    @pytest.mark.asyncio
    async def test_step_without_log_parsers_still_gets_a_tail(self, tmp_path):
        """No event parsers means no log pull during the run, so on failure the
        log is downloaded just for the tail."""
        env = _make_env({"default_cloud": "slurm"})
        env._cluster_names["np-1"] = "gb-np"
        env._job_ids["np-1"] = 9
        downloaded = tmp_path / "sky_logs" / "job-9"
        downloaded.mkdir(parents=True)
        (downloaded / "run.log").write_text("starting\nValueError: no parsers\n")
        local_root = tmp_path / "local"
        with (
            patch.object(skymod, "sky", _failed_sky(job_id=9)),
            patch.object(skymod, "HAS_SKYPILOT", True),
            patch.object(skymod, "_LOCAL_LOG_ROOT", str(local_root)),
            patch.object(
                skymod, "_download_logs_with_retry", return_value=str(downloaded)
            ) as download,
        ):
            with pytest.raises(WorkloadFailedException) as exc:
                await env._poll_skypilot_job(
                    launch_id="np-1",
                    event_q=asyncio.Queue(),
                    entityrun_metadata=EntityRunMetadata(build_id="b"),
                    poll_interval=0,
                )  # no event_configs
        download.assert_called_once_with("gb-np", 9)
        assert "ValueError: no parsers" in str(exc.value)
        assert (local_root / "gb-np" / "job-9" / "run.log").exists()

    @pytest.mark.asyncio
    async def test_existing_local_copy_is_not_downloaded_again(self, tmp_path):
        env = self._env(tmp_path)  # local copy already saved
        with (
            patch.object(skymod, "sky", _failed_sky()),
            patch.object(skymod, "HAS_SKYPILOT", True),
            patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)),
            patch.object(skymod, "_download_logs_with_retry") as download,
        ):
            with pytest.raises(WorkloadFailedException):
                await env._poll_skypilot_job(
                    launch_id="lf-1",
                    event_q=asyncio.Queue(),
                    entityrun_metadata=EntityRunMetadata(build_id="b"),
                    poll_interval=0,
                )
        download.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_hung_download_does_not_delay_the_failure(self, tmp_path):
        """The tail is best-effort: a download stuck in SkyPilot's retries is
        abandoned after the timeout and the failure is reported without it."""
        import threading

        env = _make_env({"default_cloud": "slurm"})
        env._cluster_names["np-2"] = "gb-np"
        env._job_ids["np-2"] = 9
        release = threading.Event()
        with (
            patch.object(skymod, "sky", _failed_sky(job_id=9)),
            patch.object(skymod, "HAS_SKYPILOT", True),
            patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)),
            patch.object(skymod, "_FAILURE_LOG_DOWNLOAD_TIMEOUT_S", 0.1),
            patch.object(
                skymod,
                "_download_logs_with_retry",
                side_effect=lambda *_: release.wait(10),
            ),
        ):
            try:
                with pytest.raises(WorkloadFailedException) as exc:
                    await asyncio.wait_for(
                        env._poll_skypilot_job(
                            launch_id="np-2",
                            event_q=asyncio.Queue(),
                            entityrun_metadata=EntityRunMetadata(build_id="b"),
                            poll_interval=0,
                        ),
                        timeout=5,
                    )
            finally:
                release.set()  # let the abandoned worker thread finish
        assert "terminated with status" in str(exc.value)
        assert "Last lines" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_tail_is_read_off_the_event_loop(self, tmp_path):
        """The file read runs in a worker thread, so a slow or huge log can't
        stall every other launch's polling on the shared event loop."""
        import threading

        env = self._env(tmp_path)
        loop_thread = threading.get_ident()
        read_threads = []
        real_read = skymod._read_saved_log_tail

        def spy(*args, **kwargs):
            read_threads.append(threading.get_ident())
            return real_read(*args, **kwargs)

        with (
            patch.object(skymod, "sky", _failed_sky()),
            patch.object(skymod, "HAS_SKYPILOT", True),
            patch.object(skymod, "_LOCAL_LOG_ROOT", str(tmp_path)),
            patch.object(skymod, "_read_saved_log_tail", spy),
        ):
            with pytest.raises(WorkloadFailedException):
                await env._poll_skypilot_job(
                    launch_id="lf-1",
                    event_q=asyncio.Queue(),
                    entityrun_metadata=EntityRunMetadata(build_id="b"),
                    poll_interval=0,
                )
        assert read_threads and loop_thread not in read_threads
