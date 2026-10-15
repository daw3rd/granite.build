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

"""Run test cleanup even when a test run is aborted abnormally.

A test's teardown is what deletes the remote resources it created (pushed hf://
datasets, buildrunner pods, ...). Two kinds of abort skip it:

* **Termination signals.** SIGTERM / SIGHUP (``kill``, a CI cancel, a closed
  terminal) terminate Python immediately by default. pytest only tears down on
  ``KeyboardInterrupt`` (SIGINT), so :func:`map_termination_signals_to_sigint`
  handles them the way SIGINT is handled.
* **pytest-timeout's thread method.** On timeout it dumps stacks and calls
  ``os._exit(1)``: no ``finally``, no teardown. conftest records each test's kill
  time (:func:`record_timeout_deadline`) so a test can keep its own wait under it
  (:func:`cap_to_timeout_deadline`), and arm a :class:`CleanupWatchdog` that runs
  its cleanup shortly before the kill, for a test hung where it cannot fail on its
  own (e.g. a call blocked with no read timeout).

SIGKILL cannot be handled in-process. This module must not depend on gbserver: the
signal tests run it in a bare pytest subprocess.
"""

import logging
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pytest

logger = logging.getLogger(__name__)

TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
"""Signals handled like SIGINT, so pytest runs teardown."""

MIN_BUILD_TIMEOUT_SECONDS = 60.0
"""Floor for a capped wait, so a nearly spent budget still gets a real attempt."""


def map_termination_signals_to_sigint() -> dict[int, Any]:
    """Handle SIGTERM / SIGHUP the way SIGINT is handled, so teardown runs.

    Maps them to Python's SIGINT handler (raise ``KeyboardInterrupt``), which
    pytest answers by tearing down and exiting interrupted. Call it from
    ``pytest_configure``, not a session fixture: under xdist the controller runs
    no tests, so a session fixture never reaches it, and a signalled controller
    would die at once and take its workers down without teardown.

    Uses ``signal.default_int_handler`` itself, not SIGINT's current handler: a
    process started in the background by a non-interactive shell inherits SIGINT
    as ignored, and copying that would make SIGTERM ignored too. A no-op off the
    main thread, where Python cannot set handlers.

    :returns: The previous handlers, for :func:`restore_signal_handlers`.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {
        signum: signal.signal(signum, signal.default_int_handler)
        for signum in TERMINATION_SIGNALS
    }


def restore_signal_handlers(previous: dict[int, Any]) -> None:
    """Reinstall handlers saved by :func:`map_termination_signals_to_sigint`.

    :param previous: Signal number to the handler to put back.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in previous.items():
        signal.signal(signum, handler)


@dataclass(frozen=True)
class TimeoutDeadline:
    """When pytest-timeout will act on the running test."""

    kill_at: float
    """``time.monotonic()`` value at which the timeout fires."""
    method: str
    """pytest-timeout method: ``thread`` (``os._exit``) or ``signal`` (raises)."""
    disable_debugger_detection: bool
    """If False, pytest-timeout does not fire while a debugger is attached."""

    def remaining(self) -> float:
        """Seconds until the timeout fires (negative once past)."""
        return self.kill_at - time.monotonic()


TIMEOUT_DEADLINE_KEY = pytest.StashKey[TimeoutDeadline]()
"""``item.stash`` key under which conftest records the test's deadline."""


def record_timeout_deadline(item: pytest.Item, settings: Any) -> None:
    """Record when pytest-timeout will fire for ``item``.

    Called from conftest's ``pytest_timeout_set_timer`` hook, which pytest-timeout
    calls as it starts the item's timer.

    :param item: The test item being timed.
    :param settings: pytest-timeout's settings for the item.
    """
    item.stash[TIMEOUT_DEADLINE_KEY] = TimeoutDeadline(
        kill_at=time.monotonic() + settings.timeout,
        method=settings.method,
        disable_debugger_detection=bool(settings.disable_debugger_detection),
    )


def get_timeout_deadline(item: pytest.Item) -> Optional[TimeoutDeadline]:
    """The deadline recorded for ``item``, or None when it has no timeout."""
    return item.stash.get(TIMEOUT_DEADLINE_KEY, None)


def cap_to_timeout_deadline(
    requested_seconds: float,
    deadline: Optional[TimeoutDeadline],
    margin_seconds: float,
) -> float:
    """Shorten a wait so it ends ``margin_seconds`` before pytest-timeout fires.

    A test whose own wait outlasts pytest-timeout never fails on its own: it is
    killed first, skipping its teardown. Capping lets the wait fail normally.

    :param requested_seconds: The wait the test asked for.
    :param deadline: The test's pytest-timeout deadline, or None if untimed.
    :param margin_seconds: Time to leave for stopping the work and tearing down.
    :returns: The wait to use, never below :data:`MIN_BUILD_TIMEOUT_SECONDS`.
    """
    if deadline is None:
        return requested_seconds
    budget = deadline.remaining() - margin_seconds
    if requested_seconds <= budget:
        return requested_seconds
    capped = max(budget, MIN_BUILD_TIMEOUT_SECONDS)
    logger.warning(
        "Capping the %.0fs build wait to %.0fs to finish before the pytest "
        "timeout; raise it with @pytest.mark.timeout if the build needs longer",
        requested_seconds,
        capped,
    )
    return capped


class OneShotCleanup:
    """Thread-safe wrapper that runs a cleanup function at most once."""

    def __init__(self, fn: Callable[[], None]):
        """:param fn: The cleanup to run."""
        self._fn = fn
        self._lock = threading.Lock()
        self._started = False

    def run(self, caller: str) -> bool:
        """Run the cleanup unless it has already started.

        :param caller: Who is running it, for the log.
        :returns: True if this call ran it, False if it had already started.
        :raises Exception: Whatever the cleanup raises (it still counts as run).
        """
        with self._lock:
            if self._started:
                logger.info("Cleanup already started; skipping it for %s", caller)
                return False
            self._started = True
        logger.info("Running test cleanup from %s", caller)
        self._fn()
        return True


def _is_debugging() -> bool:
    """Whether a debugger is attached, by pytest-timeout's own test."""
    import pytest_timeout

    return pytest_timeout.is_debugging()


class CleanupWatchdog:
    """Runs a cleanup shortly before pytest-timeout's thread method kills the test.

    The thread method ends the process with ``os._exit``, so the test's teardown
    never runs; this daemon timer runs the cleanup ``lead_seconds`` earlier.
    """

    def __init__(
        self,
        deadline: Optional[TimeoutDeadline],
        cleanup: Callable[[], Any],
        lead_seconds: float,
    ):
        """
        :param deadline: The test's pytest-timeout deadline, or None if untimed.
        :param cleanup: The cleanup to run; share a :class:`OneShotCleanup` with
            the test's teardown so the two never both run it.
        :param lead_seconds: How long before the kill to run the cleanup.
        """
        self._deadline = deadline
        self._cleanup = cleanup
        self._lead_seconds = lead_seconds
        self._timer: Optional[threading.Timer] = None

    def start(self) -> bool:
        """Arm the timer, if pytest-timeout would kill the process.

        Not armed for an untimed test or the signal method, whose timeout raises
        in the test and so already reaches teardown.

        :returns: True if armed.
        """
        if self._deadline is None or self._deadline.method != "thread":
            return False
        delay = max(self._deadline.remaining() - self._lead_seconds, 0.0)
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.name = "abort-cleanup-watchdog"
        self._timer.start()
        return True

    def cancel(self) -> None:
        """Disarm the timer (a no-op if it was never armed or already fired)."""
        if self._timer is not None:
            self._timer.cancel()

    def _fire(self) -> None:
        """Timer callback: clean up, as pytest-timeout is about to kill us."""
        assert self._deadline is not None
        if not self._deadline.disable_debugger_detection and _is_debugging():
            return  # pytest-timeout spares a debugged test, so keep its resources.
        logger.warning(
            "pytest-timeout fires in %.0fs and will skip teardown; cleaning up now",
            self._deadline.remaining(),
        )
        try:
            self._cleanup()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Watchdog cleanup failed")
