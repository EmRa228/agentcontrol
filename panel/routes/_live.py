"""Live binding helpers for route modules split out of app.py.

Routes were extracted mechanically. They must observe monkeypatches on the `app`
module (tests and runtime reassignments), so function globals are wrapped and a
small set of mutable globals is proxied.
"""

from __future__ import annotations

import functools
import sys
import types

MUTABLE = {
    "SESSIONS",
    "CFG",
    "STORE",
    "METRICS_HISTORY",
    "MONITOR",
    "LOGIN_LIMITER",
    "NOTIFIER",
    "SETTINGS",
    "AUTH_FAILURE_LOG",
    "PROCESS_START",
    "STATE_DIR",
    "STATE_FILE",
    "UPDATE_LOG",
    "UPDATE_LOCK",
    "RECENT_TOUCHES_FILE",
    "WORKER_DATA_ROOT",
    "CONFIG_PROBLEMS",
    "_CADDY_ALLOW_CACHE",
    "_CADDY_ALLOW_TTL",
    "CONFIG_FILE",
    "INSTALL_DIR",
    "HOST_ONLY_FILE",
    "DOCKER_SOCK",
}


class LiveProxy:
    def __init__(self, name: str):
        object.__setattr__(self, "_name", name)

    def _target(self):
        return getattr(sys.modules["app"], object.__getattribute__(self, "_name"))

    def __getattr__(self, item):
        return getattr(self._target(), item)

    def __setattr__(self, item, value):
        setattr(self._target(), item, value)

    def __getitem__(self, item):
        return self._target()[item]

    def __setitem__(self, key, value):
        self._target()[key] = value

    def __iter__(self):
        return iter(self._target())

    def __len__(self):
        return len(self._target())

    def __bool__(self):
        return bool(self._target())

    def __contains__(self, item):
        return item in self._target()

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)

    def __truediv__(self, other):
        return self._target() / other

    def __fspath__(self):
        import os

        return os.fspath(self._target())

    def __str__(self):
        return str(self._target())

    def __hash__(self):
        return hash(self._target())

    def __eq__(self, other):
        return self._target() == other

    def __repr__(self):
        return repr(self._target())


def _wrap(name: str):
    def wrapper(*args, **kwargs):
        return getattr(sys.modules["app"], name)(*args, **kwargs)

    wrapper.__name__ = name
    return wrapper


def install(module_globals: dict, panel_app) -> None:
    sys.modules.setdefault("app", panel_app)
    for name, value in vars(panel_app).items():
        if name.startswith("__"):
            continue
        if name in MUTABLE:
            module_globals[name] = LiveProxy(name)
        elif isinstance(value, types.FunctionType):
            module_globals[name] = functools.update_wrapper(_wrap(name), value)
        else:
            module_globals[name] = value
    module_globals["app"] = panel_app.app
    module_globals["__file__"] = LiveProxy("__file__")
