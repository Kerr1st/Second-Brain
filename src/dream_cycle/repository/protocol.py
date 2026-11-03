"""Explicit dependencies for repository composition and isolated fakes."""
from contextlib import AbstractContextManager
from typing import Protocol, Any

class Database(Protocol):
    def get_connection(self) -> AbstractContextManager[Any]: ...
    def get_last_briefing_time(self): ...
