"""
Purpose: Persist one canonical local CLI authentication profile for the Accounting AI Agent.
Scope: Resolve the config path, validate stored auth settings, and provide safe
load/save/delete helpers for CLI commands.
Dependencies: Pydantic for config validation plus the local filesystem under the
user's configuration directory.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_DIRECTORY_NAME = "accounting-ai-agent"
CONFIG_FILE_NAME = "cli-auth.json"


class CliAuthConfig(BaseModel):
    """Describe the persisted CLI auth profile stored on the local workstation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = Field(
        min_length=1,
        description=(
            "Fully qualified API base URL the CLI should target for authenticated requests."
        ),
    )
    token: str = Field(
        min_length=1,
        description="Opaque bearer token issued by the API token login flow.",
    )
    token_type: str = Field(
        default="Bearer",
        min_length=1,
        description="Authorization scheme used with the stored CLI token.",
    )
    user_email: str = Field(
        min_length=3,
        description="Email address associated with the account that owns the stored token.",
    )
    token_name: str = Field(
        min_length=1,
        description="Operator-friendly label for the stored personal access token.",
    )
    expires_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp returned by the API when the token was issued.",
    )


def get_cli_config_path() -> Path:
    """Return the canonical local config path used by CLI auth commands."""

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        config_root = Path(xdg_config_home).expanduser()
    else:
        config_root = Path.home() / ".config"

    return config_root / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME


def load_cli_auth_config(*, path: Path | None = None) -> CliAuthConfig | None:
    """Load and validate the stored CLI auth config, returning None when no file exists."""

    resolved_path = path or get_cli_config_path()
    if not resolved_path.exists():
        return None

    try:
        return CliAuthConfig.model_validate_json(resolved_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as error:
        message = (
            f"CLI auth config at {resolved_path} is invalid or unreadable. "
            "Run the CLI logout command to clear it and login again."
        )
        raise RuntimeError(message) from error


def save_cli_auth_config(config: CliAuthConfig, *, path: Path | None = None) -> Path:
    """Persist the provided CLI auth config and restrict the file to the current user."""

    resolved_path = path or get_cli_config_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(
        config.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    _harden_file_permissions(resolved_path)
    return resolved_path


def delete_cli_auth_config(*, path: Path | None = None) -> bool:
    """Delete the stored CLI auth config when present and report whether a file was removed."""

    resolved_path = path or get_cli_config_path()
    if not resolved_path.exists():
        return False

    resolved_path.unlink()
    return True


def _harden_file_permissions(path: Path) -> None:
    """Restrict the CLI auth config file to the current user on POSIX-compatible systems."""

    if os.name != "posix":
        return

    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


__all__ = [
    "CliAuthConfig",
    "delete_cli_auth_config",
    "get_cli_config_path",
    "load_cli_auth_config",
    "save_cli_auth_config",
]
