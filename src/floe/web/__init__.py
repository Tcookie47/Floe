"""Floe's local web app (SPEC §15): FastAPI server over `floe.core`. No Qt imports."""

from floe.web.app import create_app

__all__ = ["create_app"]
