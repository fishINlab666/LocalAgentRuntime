"""In-process coordination between state activity and maintenance."""

from contextlib import contextmanager
import threading
from typing import Iterator


class StateBusy(RuntimeError):
    def __init__(self):
        super().__init__("STATE_BUSY")
        self.code = "STATE_BUSY"


class ActivityLease:
    def __init__(self, gate: "StateMaintenanceGate", key: tuple[str, str]):
        self._gate = gate
        self._key = key
        self._closed = False

    def close(self) -> None:
        with self._gate._lock:
            if self._closed:
                return
            self._gate._active.remove(self._key)
            self._closed = True

    def __enter__(self) -> "ActivityLease":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


class StateMaintenanceGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._active: set[tuple[str, str]] = set()
        self._maintenance = False

    def start(self, kind: str, identity: str) -> ActivityLease:
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if not isinstance(identity, str) or not identity:
            raise ValueError("identity must be a non-empty string")
        key = (kind, identity)
        with self._lock:
            if self._maintenance or key in self._active:
                raise StateBusy()
            self._active.add(key)
        return ActivityLease(self, key)

    @contextmanager
    def maintenance(self) -> Iterator[None]:
        with self._lock:
            if self._maintenance or self._active:
                raise StateBusy()
            self._maintenance = True
        try:
            yield
        finally:
            with self._lock:
                self._maintenance = False
