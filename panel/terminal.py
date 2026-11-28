"""Browser PTY terminal (admin only)."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import termios
import threading
import time

try:
    import simple_websocket
except Exception:  # pragma: no cover
    simple_websocket = None


def _resize(fd: int, cols: int, rows: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass


def serve(environ: dict, *, shell: str = "/bin/bash", cwd: str = "/", max_seconds: int = 3600, logger=None) -> int:
    if simple_websocket is None:
        return 503
    try:
        ws = simple_websocket.Server.accept(environ, ping_interval=20, max_message_size=1024 * 1024)
    except Exception:
        return 400

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        try:
            os.chdir(cwd if os.path.isdir(cwd) else "/")
            os.execvpe(shell, [shell, "-l"], {**os.environ, "TERM": "xterm-256color"})
        finally:
            os._exit(127)

    stop = threading.Event()

    def pump() -> None:
        try:
            while not stop.is_set():
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                ws.send(data)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass

    reader = threading.Thread(target=pump, name="terminal-output", daemon=True)
    reader.start()
    started = time.time()
    try:
        while ws.connected and not stop.is_set():
            if time.time() - started > max_seconds:
                break
            if reader.is_alive() is False:
                break
            payload = ws.receive(timeout=1)
            if payload is None:
                continue
            if isinstance(payload, bytes):
                os.write(fd, payload)
                continue
            text = payload
            if text.startswith("{"):
                try:
                    message = json.loads(text)
                except ValueError:
                    message = {}
                kind = message.get("t")
                if kind == "r":
                    _resize(fd, int(message.get("c") or 80), int(message.get("r") or 24))
                    continue
                if kind == "i":
                    os.write(fd, str(message.get("d") or "").encode("utf-8"))
                    continue
            os.write(fd, text.encode("utf-8"))
    except Exception:
        pass
    finally:
        stop.set()
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            ws.close()
        except Exception:
            pass
        if logger:
            try:
                logger(f"terminal closed pid={pid}")
            except Exception:
                pass
    return 204
