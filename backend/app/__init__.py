"""Public package API for the Agent backend."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

__version__ = "2.0.0"


def create_app() -> "FastAPI":
    """Create an isolated FastAPI application instance."""
    from .main import create_app as factory

    return factory()


__all__ = ["__version__", "create_app"]
