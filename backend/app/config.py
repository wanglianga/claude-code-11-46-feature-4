"""Application configuration. All values overridable via environment variables."""
import os


class Settings:
    # Local dev default points at the container-provided PostgreSQL.
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
    )
    # Token validity for the lightweight session auth (seconds).
    SESSION_TTL_SECONDS: int = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
    # CORS origins (comma separated); "*" for local dev.
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")


settings = Settings()
