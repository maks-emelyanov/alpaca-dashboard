"""Reusable Alpaca paper dashboard; importing never starts background polling."""

from .client import AlpacaReadOnlyClient
from .service import DashboardService

__all__ = ["AlpacaReadOnlyClient", "DashboardService", "create_app"]


def create_app(service: DashboardService):
    """Create a Dash app without starting its caller-owned service or web server."""
    from .ui import create_app as factory

    return factory(service)
