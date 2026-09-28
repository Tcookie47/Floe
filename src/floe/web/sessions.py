"""One `FloeSession` per profile for the web app.

Sessions are created lazily on first use, always from a worker thread (FastAPI runs the
sync endpoints and jobs that call `get()` in a thread pool), so the keyring read for the
profile's secrets never blocks the event loop. Saving, renaming or deleting a profile
drops its session: in-flight queries are interrupted and the next request builds a fresh
session from the saved profile (like `MainWindow._activate_profile`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from floe.core.context import FloeSession
from floe.core.errors import FloeError
from floe.core.profiles import SECRET_FIELDS, Profile, ProfileStore

log = logging.getLogger("floe.web.sessions")

SessionFactory = Callable[[Profile], FloeSession]


class ProfileNotFound(FloeError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"No such profile: {name}")

    def user_message(self) -> str:
        return f"No such profile: {self.name}"


def default_session_factory(store: ProfileStore) -> SessionFactory:
    """Build a `FloeSession` with the profile's keyring secrets (call from a worker)."""

    def make(profile: Profile) -> FloeSession:
        secrets: dict[str, str | None] = {}
        if profile.mode != "local":
            for field_name in SECRET_FIELDS:
                secrets[field_name] = store.get_secret(profile.name, field_name)
        return FloeSession(profile, secrets)

    return make


class SessionManager:
    def __init__(self, store: ProfileStore, factory: SessionFactory | None = None) -> None:
        self._store = store
        self._factory = factory or default_session_factory(store)
        self._lock = threading.Lock()
        self._sessions: dict[str, FloeSession] = {}
        self._build_locks: dict[str, threading.Lock] = {}
        self._generations: dict[str, int] = {}

    def get(self, name: str) -> FloeSession:
        """The profile's session, building it (in the calling worker thread) if needed."""
        with self._lock:
            session = self._sessions.get(name)
            if session is not None:
                return session
            build_lock = self._build_locks.setdefault(name, threading.Lock())
        with build_lock:
            with self._lock:
                session = self._sessions.get(name)
                if session is not None:
                    return session
                generation = self._generations.get(name, 0)
            profile = self._store.get(name)
            if profile is None:
                raise ProfileNotFound(name)
            session = self._factory(profile)
            with self._lock:
                if self._generations.get(name, 0) == generation:
                    self._sessions[name] = session
            log.info("Session opened for a profile (mode=%s)", profile.mode)
            return session

    def peek(self, name: str) -> FloeSession | None:
        with self._lock:
            return self._sessions.get(name)

    def drop(self, name: str) -> None:
        """Forget the profile's session and interrupt its running queries."""
        with self._lock:
            self._generations[name] = self._generations.get(name, 0) + 1
            session = self._sessions.pop(name, None)
        if session is not None:
            session.interrupt_all()

    def drop_all(self) -> None:
        with self._lock:
            names = list(self._sessions)
        for name in names:
            self.drop(name)
