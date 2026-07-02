"""_await_result short-circuits when the runtime process dies (run aborted)."""

import time

import pytest

from venusrpc.connection import Connection, VenusTransportError


class _FakeServer:
    def __init__(self):
        from queue import Queue

        self._result_queue: Queue = Queue()


class _FakeProcess:
    def __init__(self, returncode):
        self._returncode = returncode  # None = still running

    def poll(self):
        return self._returncode

    @property
    def returncode(self):
        return self._returncode


def _conn(process):
    c = object.__new__(Connection)  # skip __init__ (spawns a runtime)
    c._server = _FakeServer()
    c._process = process
    return c


def test_dead_process_fails_fast():
    """Aborted run → fail well inside the (here long) timeout, not after it."""
    c = _conn(_FakeProcess(returncode=1))
    start = time.monotonic()
    with pytest.raises(VenusTransportError, match="aborted or run control"):
        c._await_result(result_timeout=30.0)
    assert time.monotonic() - start < 5.0


def test_result_delivered_while_alive():
    c = _conn(_FakeProcess(returncode=None))
    c._server._result_queue.put({"ok": True})
    assert c._await_result(result_timeout=5.0) == {"ok": True}


def test_timeout_when_alive_but_silent():
    c = _conn(_FakeProcess(returncode=None))
    with pytest.raises(VenusTransportError, match="within"):
        c._await_result(result_timeout=0.6)
