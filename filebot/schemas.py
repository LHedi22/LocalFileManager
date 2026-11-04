"""Schemas for model output and validation helpers.

The model must return exactly one JSON object matching ``INTENT_JSON_SCHEMA``. The output
is treated as untrusted: it is parsed and validated with Pydantic, and any failure means
no filesystem action can happen.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

FILE_ACTIONS: tuple[str, ...] = (
    "create_file",
    "delete_file",
    "create_folder",
    "delete_folder",
    "list_folder",
    "rename_path",
    "move_path",
    "copy_path",
    "read_file",
    "edit_file",
    "show_capabilities",
)
# Non-action outcomes the model may choose.
META_ACTIONS: tuple[str, ...] = ("off_topic", "unclear")
ALL_ACTIONS: tuple[str, ...] = FILE_ACTIONS + META_ACTIONS

ActionName = Literal[
    "create_file",
    "delete_file",
    "create_folder",
    "delete_folder",
    "list_folder",
    "rename_path",
    "move_path",
    "copy_path",
    "read_file",
    "edit_file",
    "show_capabilities",
    "off_topic",
    "unclear",
]

_JSON_ESCAPE_REPAIR = {9: "\\t", 10: "\\n", 13: "\\r", 8: "\\b", 12: "\\f"}

MAX_PATH_CHARS = 400
MAX_CONTENT_CHARS = 200_000

# JSON schema sent to Ollama as the `format` parameter (structured outputs).
# All fields are required strings; an empty string means "not given". This keeps the
# grammar simple enough for very small models.
INTENT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ALL_ACTIONS)},
        "path": {"type": "string"},
        "destination": {"type": "string"},
        "content": {"type": "string"},
        "mode": {"type": "string", "enum": ["", "replace", "append"]},
    },
    "required": ["action", "path", "destination", "content", "mode"],
    "additionalProperties": False,
}


class Intent(BaseModel):
    """A validated intent. Empty strings are normalised to ``None``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    action: ActionName
    path: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    destination: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    content: str | None = Field(default=None, max_length=MAX_CONTENT_CHARS)
    mode: Literal["replace", "append"] | None = None

    @field_validator("path", "destination", mode="before")
    @classmethod
    def _clean_path(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        # A Windows path like "data\temp.csv" written into JSON without escaping arrives as
        # "data<TAB>emp.csv". Control characters are never valid in paths, so restore them.
        cleaned = value.translate(_JSON_ESCAPE_REPAIR).strip()
        previous = None
        while cleaned != previous:
            previous = cleaned
            cleaned = cleaned.strip().strip("`\"'").strip()
            # Trailing sentence punctuation is never part of a path we accept.
            while cleaned and cleaned[-1] in ",;!?":
                cleaned = cleaned[:-1].rstrip()
            if cleaned.endswith(".") and cleaned != "." and not cleaned.endswith(".."):
                cleaned = cleaned[:-1]
        return cleaned or None

    @field_validator("content", mode="before")
    @classmethod
    def _clean_content(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value if value != "" else None

    @field_validator("mode", mode="before")
    @classmethod
    def _clean_mode(cls, value: object) -> object:
        if value in ("", None):
            return None
        return value


class IntentParseError(Exception):
    """The model response was not valid JSON or did not match the schema."""

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


def parse_intent(raw: str) -> Intent:
    """Parse and validate raw model output. Raises IntentParseError on any problem."""
    if not isinstance(raw, str) or not raw.strip():
        raise IntentParseError("empty response", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"invalid JSON: {exc.msg}", raw) from None
    if not isinstance(data, dict):
        raise IntentParseError("response is not a JSON object", raw)
    try:
        return Intent.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}" for err in exc.errors()
        )
        raise IntentParseError(f"schema validation failed: {problems}", raw) from None
