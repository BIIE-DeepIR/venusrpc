"""HTTP transport between Python and the Venus HSL runtime.

Python is the server (HTTPServer on a loopback port). The HSL host script is
the HTTP client. Python binds a loopback socket on an OS-assigned port, writes
the port to a file the host reads on startup, spawns HxRun.exe with the host
script, then waits for the HSL side to POST /ready.

Outbound payload (Python -> HSL via GET /command response body):
    <command_id>\n\n___SEP___\n

The command body is written to __venusrpc_cmd_<command_id>.hsl in the stamped
command directory; HSL includes that file after reading the command id.

Inbound payload (HSL -> Python via POST /result body):
    JSON object. On error: {"error_id": int, "error_description": str,
                            "error_data": [...] }
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from . import _hsl

_SEPARATOR = "___SEP___"
_SHUTDOWN_HSL = 'AddJsonVariable(___JSON___, 1, "__venusrpc_shutdown__");'


class VenusError(RuntimeError):
    """An HSL-side error reported by the Venus runtime."""

    def __init__(self, error_id: int, description: str, data: list[Any]) -> None:
        super().__init__(f"{description} (id={error_id}, data={data!r})")
        self.error_id = error_id
        self.description = description
        self.data = data


class VenusTransportError(RuntimeError):
    """HTTP, server, or runtime-process problem."""


class _VenusServer(HTTPServer):
    """HTTPServer subclass that carries shared queues and events."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cmd_queue: Queue[str] = Queue()
        self._result_queue: Queue[dict[str, Any]] = Queue()
        self._ready_event: threading.Event = threading.Event()


class _VenusHandler(BaseHTTPRequestHandler):
    """Request handler for the venusrpc HTTP server."""

    server: _VenusServer

    def do_GET(self) -> None:
        if self.path != "/command":
            self._send(404, b"")
            return
        try:
            payload = self.server._cmd_queue.get(timeout=5.0)
        except Empty:
            self._send(204, b"")
            return
        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b""

        if self.path == "/ready":
            self.server._ready_event.set()
            self._send(200, b"")
        elif self.path == "/result":
            try:
                data: dict[str, Any] = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                data = {}
            self.server._result_queue.put(data)
            self._send(200, b"")
        else:
            self._send(404, b"")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence access log
        pass


class Connection:
    """HTTP session against a single Venus runtime instance.

    Not thread-safe. One Connection serves a single caller.
    """

    DEFAULT_RUN_ENVIRONMENT = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"
    _DEVICE_PLACEHOLDER = 'device __DEVICE__("", "", hslTrue);'
    _COMMAND_PREFIX_PLACEHOLDER = '"__VENUSRPC_COMMAND_PREFIX__"'

    def __init__(
        self,
        run_environment: str | os.PathLike[str] = DEFAULT_RUN_ENVIRONMENT,
        *,
        layout_file: str | os.PathLike[str] | None = None,
        instrument_name: str = "ML_STAR",
        start_minimized: bool = False,
        host: str = "127.0.0.1",
        port: int = 0,
        accept_timeout: float = 30.0,
        spawn: bool = True,
        keep_command_files: bool = False,
        command_dir: str | os.PathLike[str] | None = None,
        simulation: bool | None = None,
        simulation_config_dir: str | os.PathLike[str] | None = None,
        cleanup_timeout: float = 180.0,
    ) -> None:
        self._hsl_dir = Path(__file__).resolve().parent / "hsl"
        self._hsl_template = self._hsl_dir / "venusrpc_host.hsl"
        self._port_file = self._hsl_dir / "venusrpc.port"
        self._cmd_dir = (
            Path(command_dir).resolve()
            if command_dir
            else Path(r"C:\Program Files (x86)\HAMILTON\LogFiles")
        )
        self._cmd_dir.mkdir(parents=True, exist_ok=True)
        self._keep_command_files = keep_command_files
        self._command_files: set[Path] = set()
        self._command_counter = 0
        self._process: subprocess.Popen[Any] | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._result_timeout: float = 30.0
        # How long to let HxRun.exe finish the Venus "End method" cleanup
        # (park iSWAP, move channels/96-head up) before force-killing it.
        self._cleanup_timeout: float = cleanup_timeout

        if simulation is not None:
            from .simulation import set_simulation

            sim_dir = (
                Path(simulation_config_dir)
                if simulation_config_dir is not None
                else None
            )
            try:
                set_simulation(simulation, sim_dir)
            except PermissionError as exc:
                raise VenusTransportError(
                    f"cannot write Venus simulation flag (need admin?): {exc}"
                ) from exc
        self.simulation = simulation

        lay = Path(layout_file).resolve() if layout_file else None
        self.layout_file = lay
        self._host_script = self._stamp_host_script(lay, instrument_name)
        self._port_file = self._host_script.parent / "venusrpc.port"

        self._server = _VenusServer((host, port), _VenusHandler)
        bound_port: int = self._server.server_address[1]
        self._write_port_file(bound_port)

        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._server_thread.start()

        if spawn:
            self._spawn_runtime(run_environment, start_minimized)

        if not self._server._ready_event.wait(accept_timeout):
            self._cleanup_on_failure()
            raise VenusTransportError(
                f"HSL host did not connect within {accept_timeout}s "
                f"(listening on {host}:{bound_port})"
            )

        atexit.register(self.close)

    def _stamp_host_script(
        self, layout_file: Path | None, instrument_name: str
    ) -> Path:
        import uuid as _uuid

        template = self._hsl_template.read_text(encoding="utf-8")
        command_prefix = str(self._cmd_dir / "__venusrpc_cmd_")
        content = template.replace(
            self._COMMAND_PREFIX_PLACEHOLDER,
            _hsl.literal(command_prefix),
            1,
        )
        if layout_file:
            device_line = (
                f"device __DEVICE__({_hsl.literal(str(layout_file))}, "
                f"{_hsl.literal(instrument_name)}, hslTrue);"
            )
            content = content.replace(self._DEVICE_PLACEHOLDER, device_line, 1)
        out = self._cmd_dir / f"venusrpc_host_{_uuid.uuid4().hex}.hsl"
        out.write_text(content, encoding="utf-8")
        return out

    def _remove_host_script(self) -> None:
        script = getattr(self, "_host_script", None)
        template = getattr(self, "_hsl_template", None)
        if script is None or script == template:
            return
        try:
            script.unlink()
        except OSError:
            pass

    @property
    def host_script(self) -> Path:
        return self._host_script

    def execute(self, hsl_code: str = "", definitions: str = "") -> dict[str, Any]:
        """Send HSL code to the runtime and return the parsed JSON response.

        Args:
            hsl_code: HSL statements executed inside __EvalExpr__().
            definitions: HSL global definitions (variables, includes, devices).

        Returns:
            Parsed JSON object returned by the runtime. Empty dict if nothing
            was attached to ``___JSON___`` HSL side.
        """
        with self._lock:
            self._guard()
            self._command_counter += 1
            cmd_id = self._command_counter

            cmd_path = self._write_command_file(definitions, hsl_code)

            payload = f"{cmd_id}\n\n{_SEPARATOR}\n"
            self._server._cmd_queue.put(payload)
            try:
                data = self._server._result_queue.get(timeout=self._result_timeout)
            except Empty as exc:
                raise VenusTransportError(
                    f"runtime did not return a result within {self._result_timeout}s"
                ) from exc
            finally:
                if not self._keep_command_files:
                    self._remove_command_file(cmd_path)

        if "error_id" in data:
            raise VenusError(
                error_id=int(data.get("error_id", -1)),
                description=str(data.get("error_description", "")),
                data=list(data.get("error_data", []) or []),
            )
        return data

    def _write_command_file(self, definitions: str, hsl_code: str) -> Path:
        """Write the command HSL file that the runtime will include."""
        content = f"{definitions}\nfunction __EvalExpr__()\n{{\n{hsl_code}\n}}\n"
        cmd_path = self._cmd_dir / f"__venusrpc_cmd_{self._command_counter}.hsl"
        tmp_path = cmd_path.with_suffix(".hsltmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, cmd_path)
        self._command_files.add(cmd_path)
        return cmd_path

    def _remove_command_file(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self._command_files.discard(path)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)

        # Send shutdown command and wait briefly for a result.
        try:
            self._command_counter += 1
            shutdown_path = self._write_command_file("", _SHUTDOWN_HSL)
            shutdown_payload = f"{self._command_counter}\n\n{_SEPARATOR}\n"
            self._server._cmd_queue.put(shutdown_payload)
            self._server._result_queue.get(timeout=10.0)
        except Exception:
            pass
        else:
            if not self._keep_command_files:
                self._remove_command_file(shutdown_path)

        self._server.shutdown()
        self._server_thread.join(5.0)
        self._server.server_close()

        if self._process is not None:
            try:
                # Wait for the runtime to run its end-of-method instrument
                # cleanup and exit on its own; only kill as a last resort.
                self._process.wait(timeout=self._cleanup_timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None

        self._remove_port_file()
        self._remove_host_script()
        if not self._keep_command_files:
            for path in list(self._command_files):
                self._remove_command_file(path)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _spawn_runtime(self, exe: str | os.PathLike[str], minimized: bool) -> None:
        if not self._host_script.exists():
            raise VenusTransportError(f"HSL host script missing: {self._host_script}")
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
            if minimized:
                startupinfo.dwFlags = subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
                startupinfo.wShowWindow = 6
        cmd = [os.fspath(exe), "-t", str(self._host_script)]
        try:
            self._process = subprocess.Popen(
                cmd,
                universal_newlines=True,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            self._cleanup_on_failure()
            raise VenusTransportError(f"could not start Venus runtime: {exc}") from exc

    def _write_port_file(self, port: int) -> None:
        self._port_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._port_file.with_suffix(".port.tmp")
        tmp.write_text(str(port), encoding="ascii")
        os.replace(tmp, self._port_file)

    def _remove_port_file(self) -> None:
        try:
            self._port_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _guard(self) -> None:
        if self._closed:
            raise VenusTransportError("connection is closed; create a new Connection")
        if self._process is not None and self._process.poll() is not None:
            raise VenusTransportError(
                f"Venus runtime exited with code {self._process.returncode}"
            )

    def _cleanup_on_failure(self) -> None:
        self._server.shutdown()
        self._server_thread.join(5.0)
        self._server.server_close()
        if self._process is not None:
            try:
                self._process.kill()
            except OSError:
                pass
            self._process = None
        self._remove_port_file()
        self._remove_host_script()
        if not self._keep_command_files:
            for path in list(getattr(self, "_command_files", ())):
                self._remove_command_file(path)
