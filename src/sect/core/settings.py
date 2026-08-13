"""Server configuration, read from the environment.

Deliberately hand-rolled rather than using pydantic-settings: it is thirty lines, it
keeps one dependency out of the container image, and it keeps cold start cheap on a
free-tier host that sleeps.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from sect.env import ensure_loaded

#: A master key shorter than this is a liability on a public URL.
MIN_MASTER_KEY_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised at boot when the environment is unusable. Never caught."""


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    master_key: str

    default_lease_seconds: int = 900
    max_lease_seconds: int = 86_400
    default_max_attempts: int = 3
    max_poll_limit: int = 100

    db_pool_min: int = 1
    db_pool_max: int = 5
    #: Set only when DATABASE_URL points at a transaction-mode pooler. Disables
    #: asyncpg's prepared-statement cache, which breaks silently under PgBouncer.
    db_pgbouncer: bool = False

    auto_migrate: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            # Pick up a local .env if there is one. Real environment variables win, so
            # this is a no-op on a host that configures the process properly.
            ensure_loaded()
            env = os.environ

        database_url = env.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigError("DATABASE_URL is required.")

        master_key = env.get("SECT_MASTER_KEY", "").strip()
        if not master_key:
            raise ConfigError("SECT_MASTER_KEY is required.")
        if len(master_key) < MIN_MASTER_KEY_LENGTH:
            raise ConfigError(
                f"SECT_MASTER_KEY must be at least {MIN_MASTER_KEY_LENGTH} characters; "
                'generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )

        settings = cls(
            database_url=database_url,
            master_key=master_key,
            default_lease_seconds=_env_int(env, "SECT_DEFAULT_LEASE_SECONDS", 900),
            max_lease_seconds=_env_int(env, "SECT_MAX_LEASE_SECONDS", 86_400),
            default_max_attempts=_env_int(env, "SECT_DEFAULT_MAX_ATTEMPTS", 3),
            max_poll_limit=_env_int(env, "SECT_MAX_POLL_LIMIT", 100),
            db_pool_min=_env_int(env, "SECT_DB_POOL_MIN", 1),
            db_pool_max=_env_int(env, "SECT_DB_POOL_MAX", 5),
            db_pgbouncer=_env_bool(env, "SECT_DB_PGBOUNCER", False),
            auto_migrate=_env_bool(env, "SECT_AUTO_MIGRATE", True),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )

        if settings.default_lease_seconds > settings.max_lease_seconds:
            raise ConfigError(
                "SECT_DEFAULT_LEASE_SECONDS cannot exceed SECT_MAX_LEASE_SECONDS "
                f"({settings.default_lease_seconds} > {settings.max_lease_seconds})."
            )
        if settings.db_pool_min > settings.db_pool_max:
            raise ConfigError("SECT_DB_POOL_MIN cannot exceed SECT_DB_POOL_MAX.")

        return settings
