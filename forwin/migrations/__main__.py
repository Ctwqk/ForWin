"""Explicit, forward-only deployment migration entry point."""

from forwin.config import InfrastructureConfig
from forwin.models.base import run_migrations


def main() -> None:
    run_migrations(InfrastructureConfig.from_env().database_url)


if __name__ == "__main__":
    main()
