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

"""Unit tests for libgbtest.abort_cleanup: cleanup on abnormal test aborts."""

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from libgbtest import abort_cleanup
from libgbtest.abort_cleanup import (
    CleanupWatchdog,
    OneShotCleanup,
    TimeoutDeadline,
    cap_to_timeout_deadline,
)

# --- OneShotCleanup ---------------------------------------------------------


def test_one_shot_cleanup_runs_once():
    calls = []
    cleanup = OneShotCleanup(lambda: calls.append(1))
    assert cleanup.run("teardown") is True
    assert cleanup.run("watchdog") is False
    assert calls == [1]


def test_one_shot_cleanup_marks_done_even_when_fn_raises():
    def boom():
        raise RuntimeError("boom")

    cleanup = OneShotCleanup(boom)
    with pytest.raises(RuntimeError):
        cleanup.run("teardown")
    assert cleanup.run("watchdog") is False


# --- cap_to_timeout_deadline ---------------------------------------------------


def _deadline(remaining: float) -> TimeoutDeadline:
    return TimeoutDeadline(
        kill_at=time.monotonic() + remaining,
        method="thread",
        disable_debugger_detection=False,
    )


def test_cap_without_deadline_returns_requested():
    assert cap_to_timeout_deadline(1800, None, margin_seconds=120) == 1800


def test_cap_leaves_short_timeout_alone():
    assert cap_to_timeout_deadline(600, _deadline(1200), margin_seconds=120) == 600


def test_cap_shortens_timeout_past_the_kill():
    capped = cap_to_timeout_deadline(1800, _deadline(1200), margin_seconds=120)
    assert 1070 <= capped <= 1080


def test_cap_never_goes_below_the_floor():
    capped = cap_to_timeout_deadline(1800, _deadline(100), margin_seconds=120)
    assert capped == abort_cleanup.MIN_BUILD_TIMEOUT_SECONDS


# --- CleanupWatchdog --------------------------------------------------------


def test_watchdog_fires_before_the_kill():
    fired = threading.Event()
    cleanup = OneShotCleanup(fired.set)
    watchdog = CleanupWatchdog(
        _deadline(0.3), lambda: cleanup.run("watchdog"), lead_seconds=0.2
    )
    assert watchdog.start() is True
    assert fired.wait(2)
    assert cleanup.run("teardown") is False


def test_watchdog_cancel_prevents_firing():
    fired = threading.Event()
    watchdog = CleanupWatchdog(_deadline(0.3), fired.set, lead_seconds=0.2)
    watchdog.start()
    watchdog.cancel()
    assert not fired.wait(0.5)


def test_watchdog_not_armed_for_signal_method():
    deadline = TimeoutDeadline(
        kill_at=time.monotonic() + 10, method="signal", disable_debugger_detection=False
    )
    watchdog = CleanupWatchdog(deadline, lambda: None, lead_seconds=1)
    assert watchdog.start() is False


def test_watchdog_not_armed_without_deadline():
    watchdog = CleanupWatchdog(None, lambda: None, lead_seconds=1)
    assert watchdog.start() is False


def test_watchdog_skips_cleanup_under_debugger(monkeypatch):
    # pytest-timeout does not kill a test being debugged, so neither do we clean up.
    monkeypatch.setattr(abort_cleanup, "_is_debugging", lambda: True)
    fired = threading.Event()
    watchdog = CleanupWatchdog(_deadline(0.2), fired.set, lead_seconds=0.1)
    watchdog.start()
    assert not fired.wait(0.5)


# --- termination signals ----------------------------------------------------


def test_map_and_restore_signal_handlers():
    before = {sig: signal.getsignal(sig) for sig in abort_cleanup.TERMINATION_SIGNALS}
    previous = abort_cleanup.map_termination_signals_to_sigint()
    try:
        for sig in abort_cleanup.TERMINATION_SIGNALS:
            assert signal.getsignal(sig) is signal.default_int_handler
    finally:
        abort_cleanup.restore_signal_handlers(previous)
    assert {sig: signal.getsignal(sig) for sig in before} == before


_SIGNAL_CONFTEST = """\
from libgbtest import abort_cleanup

def pytest_configure(config):
    config._prev_handlers = abort_cleanup.map_termination_signals_to_sigint()

def pytest_unconfigure(config):
    abort_cleanup.restore_signal_handlers(config._prev_handlers)
"""

_SIGNAL_TEST = """\
import os, time
from pathlib import Path

MARKER = Path(os.environ["MARKER_DIR"])

class TestSleeps:
    def teardown_method(self, method):
        (MARKER / "teardown").write_text("ran")
        if os.environ.get("HANG_TEARDOWN"):
            time.sleep(60)

    def test_sleep(self):
        (MARKER / "started").write_text("yes")
        time.sleep(60)
"""


def _run_and_signal(
    tmp_path: Path, signum: int, extra_args: list[str], hang_teardown: bool = False
) -> int:
    """Run a sleeping pytest session, signal it once started, return its rc.

    When ``hang_teardown`` is set, a second ``signum`` is sent after teardown
    starts, to check that it still ends the run.
    """
    (tmp_path / "conftest.py").write_text(_SIGNAL_CONFTEST)
    (tmp_path / "test_sleeps.py").write_text(_SIGNAL_TEST)
    libgbtest_root = str(Path(abort_cleanup.__file__).resolve().parents[1])
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": libgbtest_root,
        "MARKER_DIR": str(tmp_path),
        **({"HANG_TEARDOWN": "1"} if hang_teardown else {}),
    }
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    cmd += ["-p", "no:randomly", "--rootdir", str(tmp_path), *extra_args]
    proc = subprocess.Popen(
        [*cmd, str(tmp_path / "test_sleeps.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(tmp_path / "started", proc)
        proc.send_signal(signum)
        if hang_teardown:
            _wait_for(tmp_path / "teardown", proc)
            proc.send_signal(signum)
        return proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()


def _wait_for(path: Path, proc: subprocess.Popen, timeout: float = 30) -> None:
    """Wait for ``path`` to exist while ``proc`` is still running."""
    end = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, f"pytest exited before {path.name} appeared"
        assert time.monotonic() < end, f"timed out waiting for {path.name}"
        time.sleep(0.1)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_signal_runs_teardown(tmp_path, signum):
    rc = _run_and_signal(tmp_path, signum, [])
    assert (tmp_path / "teardown").exists()
    assert rc == pytest.ExitCode.INTERRUPTED


def test_signal_to_xdist_controller_runs_worker_teardown(tmp_path):
    pytest.importorskip("xdist")
    _run_and_signal(tmp_path, signal.SIGTERM, ["-n", "1"])
    assert (tmp_path / "teardown").exists()


def test_second_signal_ends_hung_teardown(tmp_path):
    # The second signal interrupts the hung teardown, so the run still exits.
    rc = _run_and_signal(tmp_path, signal.SIGTERM, [], hang_teardown=True)
    # An interrupt escaping pytest makes Python exit by re-raising SIGINT.
    assert rc in (pytest.ExitCode.INTERRUPTED, -signal.SIGINT)


def test_signal_tests_are_self_contained():
    # Guard: the subprocess tests above depend only on libgbtest.abort_cleanup
    # importing without the rest of the test tree (no gbserver imports).
    src = Path(abort_cleanup.__file__).read_text()
    assert "from gbserver" not in src and "import gbserver" not in src


_TIMEOUT_CONFTEST = """\
import pytest
from libgbtest import abort_cleanup

@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_timeout_set_timer(item, settings):
    abort_cleanup.record_timeout_deadline(item, settings)
"""

_TIMEOUT_TEST = """\
import os, time
from pathlib import Path
import pytest
from libgbtest.abort_cleanup import CleanupWatchdog, get_timeout_deadline

MARKER = Path(os.environ["MARKER_DIR"])

@pytest.fixture(autouse=True)
def watchdog(request):
    w = CleanupWatchdog(
        get_timeout_deadline(request.node),
        lambda: (MARKER / "cleaned").write_text("yes"),
        lead_seconds=1.5,
    )
    w.start()
    yield
    w.cancel()

def test_hangs():
    time.sleep(60)
"""


def test_watchdog_cleans_up_before_pytest_timeout_kill(tmp_path):
    pytest.importorskip("pytest_timeout")
    (tmp_path / "conftest.py").write_text(_TIMEOUT_CONFTEST)
    (tmp_path / "test_hangs.py").write_text(_TIMEOUT_TEST)
    libgbtest_root = str(Path(abort_cleanup.__file__).resolve().parents[1])
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": libgbtest_root,
        "MARKER_DIR": str(tmp_path),
    }
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    cmd += ["--rootdir", str(tmp_path), "--timeout=3", "--timeout-method=thread"]
    proc = subprocess.run(
        [*cmd, str(tmp_path / "test_hangs.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert b"Timeout" in proc.stdout + proc.stderr  # pytest-timeout did kill it
    assert (tmp_path / "cleaned").exists()
