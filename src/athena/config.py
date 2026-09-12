"""ATHENA AI-BRAIN configuration loading.

Configuration is read from environment variables, with a defaults layer for
anything that has a sane, local-first default (ADR-0001, master spec §14).
No config file parser is introduced yet — `tomllib` (stdlib, 3.11+) is the
natural choice if/when a file-based config is needed, per the "small
composable modules, no dependency beyond what's needed" principle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".local" / "state" / "athena"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AthenaConfig:
    """Resolved ATHENA AI-BRAIN configuration.

    `vault_root` is intentionally `Path | None`: many diagnostics (e.g. the
    doctor command) must run and report usefully even before a vault has
    been configured, rather than failing at import/construction time.
    """

    vault_root: Path | None
    data_dir: Path
    db_path: Path
    huey_db_path: Path
    huey_serializer_secret: str | None
    secret_scanner_block_on_high_confidence: bool
    qdrant_url: str
    log_level: str
    git_auto_commit_enabled: bool
    git_auto_push_enabled: bool
    git_push_interval_minutes: int
    git_command_timeout_s: float

    @property
    def vault_root_configured(self) -> bool:
        return self.vault_root is not None


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def load_config() -> AthenaConfig:
    """Load configuration from the environment.

    Recognized variables:
      ATHENA_VAULT_DIR                 -- path to the Obsidian vault (no default)
      ATHENA_DATA_DIR                  -- ATHENA AI-BRAIN's own state dir (default: see
                                           DEFAULT_DATA_DIR)
      ATHENA_HUEY_SECRET                -- HMAC secret for Huey's SignedSerializer, ADR-0002
      ATHENA_SECRET_SCANNER_BLOCK_HIGH  -- "true" to hard-block high-confidence secret findings
                                              instead of redact-and-flag (default: false, per
                                              docs/design/pre-ingestion-secret-scanning.md §4.2)
      ATHENA_QDRANT_URL                 -- Qdrant server URL (default: http://127.0.0.1:6333,
                                              matching ADR-0006's 127.0.0.1-only binding)
      ATHENA_LOG_LEVEL                  -- Python logging level name (default: INFO)
      ATHENA_GIT_AUTO_COMMIT            -- "false" to disable auto-committing vault mutations
                                              (default: true, per docs/GIT_WORKFLOW.md -- local-only
                                              and non-destructive, so safe to default on)
      ATHENA_GIT_AUTO_PUSH              -- "true" to enable the periodic auto-push job (default:
                                              false -- push touches an external system, per
                                              docs/GIT_WORKFLOW.md's conservative default)
      ATHENA_GIT_PUSH_INTERVAL_MINUTES  -- periodic auto-push cadence in minutes (default: 60)
      ATHENA_GIT_COMMAND_TIMEOUT_S      -- per-subprocess `git` call timeout in seconds
                                              (default: 30.0; `push` uses max(this, 60.0))
    """
    data_dir = _env_path("ATHENA_DATA_DIR") or DEFAULT_DATA_DIR
    return AthenaConfig(
        vault_root=_env_path("ATHENA_VAULT_DIR"),
        data_dir=data_dir,
        db_path=data_dir / "athena.db",
        huey_db_path=data_dir / "huey.db",
        huey_serializer_secret=os.environ.get("ATHENA_HUEY_SECRET"),
        secret_scanner_block_on_high_confidence=_env_bool(
            "ATHENA_SECRET_SCANNER_BLOCK_HIGH", default=False
        ),
        qdrant_url=os.environ.get("ATHENA_QDRANT_URL", "http://127.0.0.1:6333"),
        log_level=os.environ.get("ATHENA_LOG_LEVEL", "INFO"),
        git_auto_commit_enabled=_env_bool("ATHENA_GIT_AUTO_COMMIT", default=True),
        git_auto_push_enabled=_env_bool("ATHENA_GIT_AUTO_PUSH", default=False),
        git_push_interval_minutes=_env_int("ATHENA_GIT_PUSH_INTERVAL_MINUTES", default=60),
        git_command_timeout_s=_env_float("ATHENA_GIT_COMMAND_TIMEOUT_S", default=30.0),
    )
