"""Request-local proof that an owner OS write never started in Apps Script."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class WriteDispatch:
    operation: str
    dispatched: int = 0
    signed_busy: bool = False

    @property
    def not_started(self) -> bool:
        return self.dispatched == 0 or (self.dispatched == 1 and self.signed_busy)

    @contextmanager
    def activate(self) -> Iterator["WriteDispatch"]:
        # Share the mutable request object with run_in_threadpool's copied context.
        token = _WRITE.set(self)
        try:
            yield self
        finally:
            _WRITE.reset(token)


_WRITE: ContextVar[WriteDispatch | None] = ContextVar("mia_owner_write_dispatch", default=None)


def current_write_dispatch() -> WriteDispatch | None:
    return _WRITE.get()
