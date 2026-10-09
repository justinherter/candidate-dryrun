"""In-memory storage. Stands in for a real database in this exercise."""

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.models import Application, ApplicationIn, Status


class ApplicationStore:
    def __init__(self) -> None:
        self._apps: dict[str, Application] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def locked(self, app_id: str) -> Iterator[None]:
        """Serialise work on a single application.

        Stands in for a row lock (`SELECT ... FOR UPDATE`) in a real database.
        Without it, two concurrent requests can both read an application, both
        decide it is safe to act, and then both write - so a guard like
        "only submit a draft" silently stops holding.

        Scoped per application id, so unrelated applications never wait on
        each other.
        """
        with self._locks_guard:
            lock = self._locks.setdefault(app_id, threading.Lock())
        with lock:
            yield

    def create(self, data: ApplicationIn) -> Application:
        app = Application(
            id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            **data.model_dump(),
        )
        self._apps[app.id] = app
        return app

    def get(self, app_id: str) -> Application | None:
        return self._apps.get(app_id)

    def find_by_lender_reference(self, reference: str) -> Application | None:
        return next((a for a in self._apps.values() if a.lender_reference == reference), None)

    def list(self, status: Status | None = None) -> list[Application]:
        apps = sorted(self._apps.values(), key=lambda a: a.created_at)
        if status is not None:
            apps = [a for a in apps if a.status == status]
        return apps
