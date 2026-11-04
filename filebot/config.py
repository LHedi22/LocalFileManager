"""Configuration: defaults < config file (filebot.toml) < environment variables < CLI flags."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_DIR / "filebot.toml"

DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MAX_READ_BYTES = 1_048_576  # 1 MB


class ConfigError(Exception):
    """Raised when the configuration file or environment contains invalid values."""


@dataclass(frozen=True)
class Config:
    model: str = DEFAULT_MODEL
    ollama_host: str = DEFAULT_HOST
    workspace: Path = field(default_factory=lambda: PROJECT_DIR / "workspace")
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    request_timeout: float = 120.0
    debug: bool = False


def _parse_int(name: str, value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from None
    if number <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {number}")
    return number


def _parse_float(name: str, value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number, got {value!r}") from None
    if number <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {number}")
    return number


def load_config(
    config_file: Path | None = None,
    env: dict[str, str] | None = None,
    *,
    model: str | None = None,
    workspace: str | Path | None = None,
    debug: bool = False,
) -> Config:
    """Build the effective configuration."""
    env = dict(os.environ) if env is None else env
    cfg = Config()
    path = config_file or Path(env.get("FILEBOT_CONFIG", DEFAULT_CONFIG_FILE))

    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"Could not read config file {path}: {exc}") from None
        section = data.get("filebot", data)
        if "model" in section:
            cfg = replace(cfg, model=str(section["model"]))
        if "ollama_host" in section:
            cfg = replace(cfg, ollama_host=str(section["ollama_host"]))
        if "workspace" in section:
            ws = Path(str(section["workspace"]))
            cfg = replace(cfg, workspace=ws if ws.is_absolute() else path.parent / ws)
        if "max_read_bytes" in section:
            cfg = replace(cfg, max_read_bytes=_parse_int("max_read_bytes", section["max_read_bytes"]))
        if "request_timeout" in section:
            cfg = replace(cfg, request_timeout=_parse_float("request_timeout", section["request_timeout"]))

    if env.get("FILEBOT_MODEL"):
        cfg = replace(cfg, model=env["FILEBOT_MODEL"])
    if env.get("OLLAMA_HOST"):
        host = env["OLLAMA_HOST"]
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        cfg = replace(cfg, ollama_host=host)
    if env.get("FILEBOT_WORKSPACE"):
        cfg = replace(cfg, workspace=Path(env["FILEBOT_WORKSPACE"]))
    if env.get("FILEBOT_MAX_READ_BYTES"):
        cfg = replace(cfg, max_read_bytes=_parse_int("FILEBOT_MAX_READ_BYTES", env["FILEBOT_MAX_READ_BYTES"]))
    if env.get("FILEBOT_TIMEOUT"):
        cfg = replace(cfg, request_timeout=_parse_float("FILEBOT_TIMEOUT", env["FILEBOT_TIMEOUT"]))

    if model:
        cfg = replace(cfg, model=model)
    if workspace:
        cfg = replace(cfg, workspace=Path(workspace))
    if debug:
        cfg = replace(cfg, debug=True)
    return cfg
