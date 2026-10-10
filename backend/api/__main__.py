"""Run the Nexus REST API (loopback by default, never 0.0.0.0 implicitly)."""

from __future__ import annotations

from backend.api.settings import ApiSettings


def main() -> None:
    """Serve with uvicorn using the declared environment."""
    import uvicorn

    settings = ApiSettings.from_env()
    uvicorn.run(
        "backend.api.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
