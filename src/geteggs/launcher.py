"""Lightweight launcher for the local Web console.

This module intentionally avoids importing the heavy CLI/FastAPI stack so the
desktop shortcut can decide whether the server is running, show a temporary
loading page, and open the real console without paying for a full application
import.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from geteggs.core.config import get_data_dir, get_ui_state_path

UI_START_TIMEOUT = 20.0
SPLASH_REDIRECT_TIMEOUT = 15.0
SPLASH_ERROR_TIMEOUT = 10.0
SPLASH_TOTAL_TIMEOUT = 30.0

LOADING_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GetEggs 正在启动</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #f4f6f8; color: #111827;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .card {
    width: min(420px, calc(100vw - 48px)); padding: 36px 32px; background: #fff;
    border: 1px solid #e5e7eb; border-radius: 16px;
    box-shadow: 0 12px 40px rgba(17, 24, 39, .08); text-align: center;
  }
  .spinner {
    width: 34px; height: 34px; margin: 0 auto 18px; border: 3px solid #dbeafe;
    border-top-color: #2563eb; border-radius: 50%; animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h1 { margin: 0 0 10px; font-size: 20px; letter-spacing: .02em; }
  p { margin: 0; color: #6b7280; font-size: 14px; line-height: 1.7; }
  #status { min-height: 24px; color: #374151; }
  .hint { margin-top: 12px; font-size: 12px; color: #9ca3af; }
</style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <h1>GetEggs 正在启动</h1>
    <p id="status">正在准备本地服务...</p>
    <p id="hint" class="hint">首次启动可能需要几秒钟，请稍候。</p>
  </div>
<script>
  const nonce = new URLSearchParams(location.search).get('n') || '';
  const statusEl = document.getElementById('status');
  const hintEl = document.getElementById('hint');
  const spinner = document.querySelector('.spinner');
  async function poll() {
    try {
      const resp = await fetch('/ready?n=' + encodeURIComponent(nonce), { cache: 'no-store' });
      const data = await resp.json();
      if (data.ready && data.url) {
        statusEl.textContent = '正在打开控制台...';
        location.replace(data.url);
        return;
      }
      if (data.error) {
        spinner.style.display = 'none';
        statusEl.textContent = data.error;
        hintEl.textContent = '请关闭此页面后重新双击图标。';
        return;
      }
    } catch (err) {}
    setTimeout(poll, 150);
  }
  poll();
</script>
</body>
</html>
"""


class SplashState:
    """Shared state between the launcher thread and the loading page."""

    def __init__(self) -> None:
        self.nonce = secrets.token_urlsafe(16)
        self.started_at = time.monotonic()
        self.lock = threading.Lock()
        self.url: Optional[str] = None
        self.error: Optional[str] = None
        self.redirect_seen = threading.Event()
        self.error_seen = threading.Event()
        self.proc: Optional[subprocess.Popen] = None


class _SplashHandler(BaseHTTPRequestHandler):
    state: SplashState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        nonce = (parse_qs(parsed.query).get("n") or [""])[0]
        if not secrets.compare_digest(nonce, self.state.nonce):
            self._send(403, "text/plain; charset=utf-8", b"Forbidden")
            return
        if parsed.path in ("/", "/loading"):
            self._send(200, "text/html; charset=utf-8", LOADING_HTML.encode("utf-8"))
            return
        if parsed.path == "/ready":
            payload = self._ready_payload()
            self._send_json(payload)
            if payload.get("ready"):
                self.state.redirect_seen.set()
            elif payload.get("error"):
                self.state.error_seen.set()
            return
        self._send(404, "text/plain; charset=utf-8", b"Not Found")

    def _ready_payload(self) -> dict:
        state = self.state
        with state.lock:
            if state.url:
                return {"ready": True, "url": state.url}
            if state.error:
                return {"ready": False, "error": state.error}
            proc = state.proc
            started_at = state.started_at
        existing = existing_ui_url()
        if existing:
            with state.lock:
                state.url = existing
            return {"ready": True, "url": existing}
        if (
            proc is not None
            and proc.poll() is not None
            and time.monotonic() - started_at > 5.0
        ):
            msg = (
                "GetEggs Web 服务启动失败，请查看日志："
                f"{get_data_dir() / 'geteggs-ui.log'}"
            )
            with state.lock:
                state.error = msg
            return {"ready": False, "error": msg}
        if time.monotonic() - started_at > SPLASH_TOTAL_TIMEOUT:
            msg = (
                "GetEggs Web 服务启动超时，请查看日志："
                f"{get_data_dir() / 'geteggs-ui.log'}"
            )
            with state.lock:
                state.error = msg
            return {"ready": False, "error": msg}
        return {"ready": False}

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)

    def _send(self, status_code: int, content_type: str, body: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def start_splash_server(state: SplashState) -> tuple[ThreadingHTTPServer, str]:
    """Serve a tiny loading page and return its local URL."""
    handler = type("GetEggsSplashHandler", (_SplashHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    return server, f"http://127.0.0.1:{port}/loading?n={state.nonce}"


def read_ui_state(timeout: float = 1.0) -> dict:
    deadline = time.monotonic() + timeout
    path = get_ui_state_path()
    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
        if time.monotonic() >= deadline:
            return {}
        time.sleep(0.05)


def ui_endpoint(state: dict) -> Optional[tuple[str, int]]:
    """Return a validated local console URL and port from UI state."""
    url = state.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return None
    try:
        port = int(state.get("port") or parsed.port or 0)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return url, port


def port_open(port: int) -> bool:
    """Return True when a local TCP listener is accepting connections.

    A non-blocking connect plus a short ``select`` keeps readiness polling fast
    even when the OS delays refusals for closed loopback ports.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        code = sock.connect_ex(("127.0.0.1", port))
        if code == 0:
            return True
        pending = {
            errno.EINPROGRESS,
            errno.EWOULDBLOCK,
            getattr(errno, "WSAEWOULDBLOCK", 10035),
            10035,
            10036,
        }
        if code not in pending:
            return False
        _, writable, _ = select.select([], [sock], [], 0.05)
        if not writable:
            return False
        return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _pid_alive(pid: object) -> bool:
    """Return True when the recorded process still exists.

    Legacy state files without a pid fall back to the port probe.
    """
    try:
        value = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    if value <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(0x1000, False, value)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return True
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def _terminate_process(proc: Optional[subprocess.Popen]) -> None:
    """Best-effort cleanup for a server process that never became ready."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        terminate = getattr(proc, "terminate", None)
        if callable(terminate):
            terminate()
        wait = getattr(proc, "wait", None)
        if callable(wait):
            try:
                wait(timeout=3)
            except (subprocess.TimeoutExpired, TypeError):
                kill = getattr(proc, "kill", None)
                if callable(kill):
                    kill()
    except OSError:
        pass


def existing_ui_url() -> Optional[str]:
    """Return the running console URL, or None when no instance responds."""
    state = read_ui_state(timeout=0.05)
    endpoint = ui_endpoint(state)
    if (
        endpoint
        and _pid_alive(state.get("pid"))
        and port_open(endpoint[1])
    ):
        return endpoint[0]
    return None


def wait_for_state(
    proc: subprocess.Popen, timeout: float = UI_START_TIMEOUT
) -> bool:
    """Wait until the spawned server is reachable or has exited."""
    deadline = time.monotonic() + timeout
    while True:
        if existing_ui_url():
            return True
        if proc.poll() is not None:
            # The venv trampoline may exit just before the real interpreter
            # starts listening; give it a short grace period.
            grace_deadline = time.monotonic() + 2.0
            while time.monotonic() < grace_deadline:
                if existing_ui_url():
                    return True
                time.sleep(0.05)
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def ui_server_command(open_when_ready: bool = True) -> list[str]:
    args = ["-m", "geteggs", "ui"]
    if open_when_ready:
        args.append("--open-when-ready")
    else:
        args.append("--no-browser")
    args.append("--quiet")
    exe = Path(sys.executable)
    for name in ("pythonw.exe", "python.exe"):
        candidate = exe.with_name(name)
        if candidate.is_file():
            return [str(candidate), *args]
    return [str(exe), *args]


def spawn_ui_server(open_when_ready: bool = True) -> subprocess.Popen:
    """Start the local console in the background without a terminal window."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return subprocess.Popen(
        ui_server_command(open_when_ready),
        cwd=str(get_data_dir()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def open_url(url: str) -> bool:
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            os.startfile(url)
            return True
        except OSError:
            pass
    return False


def notify_failure(message: str) -> None:
    log_path = get_data_dir() / "geteggs-ui.log"
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass
    if os.environ.get("GETEGGS_HIDDEN") == "1" and os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "GetEggs", 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _fallback_start() -> None:
    """Start the server with its own browser opener when splash is unavailable."""
    try:
        proc = spawn_ui_server(open_when_ready=True)
    except OSError as exc:
        notify_failure(f"启动 GetEggs Web 服务失败：{exc}")
        return
    if not wait_for_state(proc):
        _terminate_process(proc)
        notify_failure(
            f"GetEggs Web 服务启动失败或超时，请查看日志："
            f"{get_data_dir() / 'geteggs-ui.log'}"
        )


def main() -> None:
    """Open the console, showing a loading page while the server starts."""
    existing = existing_ui_url()
    if existing:
        if not open_url(existing):
            notify_failure(
                "GetEggs 服务已在运行，但浏览器打开失败，请检查默认浏览器设置。"
            )
        return

    state = SplashState()
    splash_server: Optional[ThreadingHTTPServer] = None
    splash_url = ""
    try:
        splash_server, splash_url = start_splash_server(state)
    except OSError:
        splash_server = None

    if splash_server is not None:
        if not open_url(splash_url):
            splash_server.shutdown()
            splash_server.server_close()
            splash_server = None

    if splash_server is None:
        _fallback_start()
        return

    try:
        proc = spawn_ui_server(open_when_ready=False)
    except OSError as exc:
        with state.lock:
            state.error = f"启动 GetEggs Web 服务失败：{exc}"
    else:
        with state.lock:
            state.proc = proc
        if wait_for_state(proc):
            url = existing_ui_url()
            with state.lock:
                if url:
                    state.url = url
                else:
                    state.error = "GetEggs Web 服务已启动，但无法读取控制台地址。"
        else:
            _terminate_process(proc)
            with state.lock:
                state.error = (
                    "GetEggs Web 服务启动失败或超时，请查看日志："
                    f"{get_data_dir() / 'geteggs-ui.log'}"
                )

    with state.lock:
        has_url = bool(state.url)
    if has_url:
        state.redirect_seen.wait(SPLASH_REDIRECT_TIMEOUT)
    else:
        state.error_seen.wait(SPLASH_ERROR_TIMEOUT)

    splash_server.shutdown()
    splash_server.server_close()


if __name__ == "__main__":
    main()
