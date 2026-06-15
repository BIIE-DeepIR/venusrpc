"""HTTP transport and Connection round-trip tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from venusrpc.connection import (
    Connection,
    VenusError,
    VenusTransportError,
    _SEPARATOR,
    _VenusHandler,
    _VenusServer,
)


# ---------------------------------------------------------------------------
# Payload parsing helper (mirrors ParseFrame in HSL)
# ---------------------------------------------------------------------------

def _parse_payload(payload: str) -> tuple[int, str, str]:
    """Parse ``{cmd_id}\\n{defs}\\n___SEP___\\n{code}`` into its three parts."""
    header, _, code = payload.partition(f"\n{_SEPARATOR}\n")
    cmd_line, _, defs = header.partition("\n")
    return int(cmd_line), defs, code


def _split_cmd_file(content: str) -> tuple[str, str]:
    """Split a command ``.hsl`` file into ``(definitions, hsl_code)``.

    Mirrors ``Connection._write_command_file``'s format:
    ``{defs}\\nfunction __EvalExpr__()\\n{{\\n{code}\\n}}\\n``.
    """
    head, _, body = content.partition("function __EvalExpr__()\n{\n")
    defs = head.rstrip("\n")
    if body.endswith("}\n"):
        body = body[:-2]
    return defs, body.rstrip("\n")


# ---------------------------------------------------------------------------
# Fake HSL client (HTTP)
# ---------------------------------------------------------------------------

class _FakeHslClient(threading.Thread):
    """Stand-in for the HSL HTTP client. Reads port from file, talks HTTP."""

    def __init__(self, host: str, port: int, responder: Callable, cmd_dir: Path) -> None:
        super().__init__(daemon=True)
        self._host = host
        self._port = port
        self._responder = responder
        self._cmd_dir = cmd_dir
        self.received: list[tuple[int, str]] = []  # (cmd_id, file_content)
        self.exception: BaseException | None = None
        self._should_stop = False

    def _read_cmd_file(self, cmd_id: int) -> str:
        path = self._cmd_dir / f"__venusrpc_cmd_{cmd_id}.hsl"
        for _ in range(20):
            if path.exists():
                return path.read_text(encoding="ascii")
            time.sleep(0.01)
        return ""

    def run(self) -> None:
        import http.client

        try:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=10)

            # Signal ready.
            conn.request("POST", "/ready", body=b"", headers={"Content-Length": "0"})
            conn.getresponse().read()

            while not self._should_stop:
                conn.request("GET", "/command")
                resp = conn.getresponse()
                body = resp.read().decode("utf-8")

                if resp.status == 204:
                    # No command yet; retry.
                    continue

                if resp.status != 200:
                    return

                cmd_id, _, _ = _parse_payload(body)
                file_content = self._read_cmd_file(cmd_id)

                if "__venusrpc_shutdown__" in file_content:
                    # Acknowledge shutdown and exit.
                    result_body = json.dumps({}).encode("utf-8")
                    conn.request(
                        "POST",
                        "/result",
                        body=result_body,
                        headers={
                            "Content-Type": "application/json",
                            "Content-Length": str(len(result_body)),
                        },
                    )
                    conn.getresponse().read()
                    return

                defs, code = _split_cmd_file(file_content)
                self.received.append((cmd_id, defs, code))

                reply = self._responder(cmd_id, defs, code)
                if reply is None:
                    # Simulate disconnect without posting result.
                    return

                result_body = reply.encode("utf-8") if isinstance(reply, str) else reply
                conn.request(
                    "POST",
                    "/result",
                    body=result_body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(result_body)),
                    },
                )
                conn.getresponse().read()

        except BaseException as exc:  # noqa: BLE001
            self.exception = exc

    def stop(self) -> None:
        self._should_stop = True


# ---------------------------------------------------------------------------
# Helper: build a Connection backed by a fake HSL client
# ---------------------------------------------------------------------------

def _make_connection(
    tmp_path: Path, responder: Callable
) -> tuple[Connection, _FakeHslClient]:
    """Create a Connection without spawning a real Venus process.

    Uses ``Connection.__new__`` to bypass ``__init__``, then manually wires up
    the HTTP server and a fake HSL client thread.
    """
    c = Connection.__new__(Connection)

    c._hsl_dir = tmp_path
    c._host_script = tmp_path / "venusrpc_host.hsl"
    c._port_file = tmp_path / "venusrpc.port"
    c._cmd_dir = tmp_path
    c._command_counter = 0
    c._closed = False
    c._lock = threading.Lock()
    c._process = None
    c._result_timeout = 5.0
    c._keep_command_files = False
    c._command_files = set()

    c._server = _VenusServer(("127.0.0.1", 0), _VenusHandler)
    bound_port: int = c._server.server_address[1]
    c._write_port_file(bound_port)

    c._server_thread = threading.Thread(target=c._server.serve_forever, daemon=True)
    c._server_thread.start()

    fake = _FakeHslClient("127.0.0.1", bound_port, responder, tmp_path)
    fake.start()

    if not c._server._ready_event.wait(timeout=5.0):
        raise RuntimeError("fake HSL client did not post /ready in time")

    return c, fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_connection_execute_roundtrip(tmp_path: Path) -> None:
    def responder(cmd_id: int, defs: str, code: str) -> str:
        return json.dumps({"value": 42, "echo_code": code, "echo_defs": defs})

    conn, fake = _make_connection(tmp_path, responder)
    try:
        result = conn.execute("foo();", "variable bar(1);")
        assert result == {
            "value": 42,
            "echo_code": "foo();",
            "echo_defs": "variable bar(1);",
        }

        assert fake.received[0][1] == "variable bar(1);"
        assert fake.received[0][2] == "foo();"

        result2 = conn.execute("baz();")
        assert result2["echo_code"] == "baz();"
        assert fake.received[1][0] == 2  # command counter incremented
    finally:
        conn.close()
        fake.join(timeout=2)


def test_connection_surfaces_hsl_error(tmp_path: Path) -> None:
    def responder(cmd_id: int, defs: str, code: str) -> str:
        return json.dumps({
            "error_id": 12345,
            "error_description": "boom",
            "error_data": ["x", 1],
        })

    conn, fake = _make_connection(tmp_path, responder)
    try:
        with pytest.raises(VenusError) as ei:
            conn.execute("fail();")
        assert ei.value.error_id == 12345
        assert ei.value.description == "boom"
        assert ei.value.data == ["x", 1]
    finally:
        conn.close()
        fake.join(timeout=2)


def test_connection_raises_on_runtime_disconnect(tmp_path: Path) -> None:
    """If the fake client stops without posting a result, execute() must raise."""

    def responder(cmd_id: int, defs: str, code: str) -> None:  # type: ignore[return]
        return None  # signal disconnect

    conn, fake = _make_connection(tmp_path, responder)
    conn._result_timeout = 2.0
    try:
        with pytest.raises(VenusTransportError):
            conn.execute("x();")
    finally:
        conn._closed = True  # skip shutdown handshake — fake is gone
        conn._server.shutdown()
        conn._server_thread.join(5.0)
        conn._server.server_close()
        conn._remove_port_file()
        fake.join(timeout=2)


def test_connection_context_manager(tmp_path: Path) -> None:
    def responder(cmd_id: int, defs: str, code: str) -> str:
        return json.dumps({"ok": 1})

    c, fake = _make_connection(tmp_path, responder)
    with c:
        assert c.execute("foo();") == {"ok": 1}
    assert c._closed
    with pytest.raises(VenusTransportError):
        c.execute("bar();")
    fake.join(timeout=2)


def test_connection_close_is_idempotent(tmp_path: Path) -> None:
    def responder(cmd_id: int, defs: str, code: str) -> str:
        return json.dumps({})

    c, fake = _make_connection(tmp_path, responder)
    c.close()
    c.close()  # second close must not raise
    fake.join(timeout=2)


def test_execute_raises_after_close(tmp_path: Path) -> None:
    def responder(cmd_id: int, defs: str, code: str) -> str:
        return json.dumps({})

    c, fake = _make_connection(tmp_path, responder)
    c.close()
    fake.join(timeout=2)
    with pytest.raises(VenusTransportError):
        c.execute("anything();")


def test_no_transport_error_without_spawn(tmp_path: Path) -> None:
    """Connection with spawn=False must start the HTTP server without raising."""
    # We can't write a real venusrpc_host.hsl to tmp_path so we use spawn=False
    # which skips both the process launch and the ready-event wait.
    # Manually set _hsl_dir to tmp_path to avoid port file collision.
    c = Connection.__new__(Connection)
    c._hsl_dir = tmp_path
    c._host_script = tmp_path / "venusrpc_host.hsl"
    c._port_file = tmp_path / "venusrpc.port"
    c._command_counter = 0
    c._closed = False
    c._lock = threading.Lock()
    c._process = None
    c._result_timeout = 5.0
    c._keep_command_files = False
    c._command_files = set()

    c._server = _VenusServer(("127.0.0.1", 0), _VenusHandler)
    c._write_port_file(c._server.server_address[1])
    c._server_thread = threading.Thread(target=c._server.serve_forever, daemon=True)
    c._server_thread.start()

    # Server is up; no VenusTransportError was raised.
    assert not c._closed
    assert c._server.server_address[1] > 0

    # Clean up without going through the full close() handshake.
    c._server.shutdown()
    c._server_thread.join(5.0)
    c._server.server_close()
    c._remove_port_file()


def _skeleton_for_ready(tmp_path: Path, poll_value: int | None) -> Connection:
    """Minimal Connection with a live HTTP server and a fake runtime process."""
    c = Connection.__new__(Connection)
    c._hsl_dir = tmp_path
    c._host_script = tmp_path / "venusrpc_host_test.hsl"
    c._port_file = tmp_path / "venusrpc.port"
    c._cmd_dir = tmp_path
    c._command_counter = 0
    c._closed = False
    c._lock = threading.Lock()
    c._keep_command_files = False
    c._command_files = set()

    class _FakeProc:
        def poll(self) -> int | None:
            return poll_value

        def kill(self) -> None:
            pass

        def wait(self, *a: object, **k: object) -> int:
            return 0

    c._process = _FakeProc()
    c._server = _VenusServer(("127.0.0.1", 0), _VenusHandler)
    c._write_port_file(c._server.server_address[1])
    c._server_thread = threading.Thread(target=c._server.serve_forever, daemon=True)
    c._server_thread.start()
    return c


def test_await_ready_reports_runtime_exit(tmp_path: Path) -> None:
    """If the runtime exits before /ready, fail fast with an actionable message."""
    # A host trace with an analyze error, so diagnostics can be surfaced.
    (tmp_path / "venusrpc_host_test_abc_Trace.trc").write_text(
        "SYSTEM : Analyze method - start;\n"
        "SYSTEM : Analyze method - complete with error;\n",
        encoding="utf-8",
    )
    c = _skeleton_for_ready(tmp_path, poll_value=1)
    try:
        with pytest.raises(VenusTransportError) as ei:
            c._await_ready(accept_timeout=2.0, host="127.0.0.1", bound_port=9999)
        msg = str(ei.value)
        assert "exited during startup" in msg
        assert "instrument is powered off or unreachable" in msg
        assert "Analyze method - complete with error" in msg  # trace detail
    finally:
        c._server_thread.join(5.0)


def test_await_ready_times_out_when_runtime_alive(tmp_path: Path) -> None:
    """If the runtime stays up but never readies, time out with a hint."""
    c = _skeleton_for_ready(tmp_path, poll_value=None)  # still running
    try:
        with pytest.raises(VenusTransportError) as ei:
            c._await_ready(accept_timeout=0.5, host="127.0.0.1", bound_port=9999)
        assert "did not connect within 0.5s" in str(ei.value)
    finally:
        c._server_thread.join(5.0)


def test_startup_diagnostics_missing_trace_is_empty(tmp_path: Path) -> None:
    """No trace file -> empty diagnostics, never raises."""
    c = _skeleton_for_ready(tmp_path, poll_value=1)
    try:
        assert c._startup_diagnostics() == ""
    finally:
        c._server.shutdown()
        c._server_thread.join(5.0)
        c._server.server_close()
