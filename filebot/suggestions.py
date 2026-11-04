"""Text-only next-step suggestions and the capabilities overview.

Suggestions are never executed. They only mention supported, non-destructive actions
and are derived from the action result plus the current workspace state.
"""

from __future__ import annotations

import os
from .actions import ActionResult
from .security import PathSecurityError, Workspace

CAPABILITIES: list[tuple[str, str]] = [
    ("Create a file", "create a file called notes.txt with the text hello"),
    ("Create a folder", "make a folder named projects"),
    ("List a folder", "what's in the projects folder?"),
    ("Read a file", "show me notes.txt"),
    ("Edit a file", "append 'second line' to notes.txt  /  replace the contents of notes.txt with 'new text'"),
    ("Rename", "rename notes.txt to todo.txt"),
    ("Move", "move todo.txt into projects"),
    ("Copy", "copy projects/todo.txt to backup.txt"),
    ("Delete a file", "delete backup.txt  (asks for confirmation)"),
    ("Delete a folder", "delete the folder projects  (asks for confirmation)"),
]

EXAMPLE_COMMANDS = [
    "create a folder named projects",
    "create a file called projects/notes.txt with the text hello",
    "list the projects folder",
    "rename projects/notes.txt to todo.txt",
]

OFF_TOPIC_MESSAGE = "I can only manage files and folders in your workspace."


def _safe_listing(ws: Workspace, rel: str | None) -> list[os.DirEntry] | None:
    try:
        folder = ws.resolve(rel)
        if not folder.is_dir():
            return None
        with os.scandir(folder) as it:
            return list(it)
    except (OSError, PathSecurityError):
        return None


def _is_file(ws: Workspace, rel: str) -> bool:
    try:
        return ws.resolve(rel).is_file()
    except (OSError, PathSecurityError):
        return False


def _q(path: str) -> str:
    return f'"{path}"' if " " in path else path


def suggest(result: ActionResult, ws: Workspace) -> str | None:
    """Return one short suggestion, or None."""
    if not result.ok:
        return None
    a, target = result.action, result.target

    if a == "create_folder" and target:
        entries = _safe_listing(ws, target)
        if entries is not None and not any(e.name.lower() == "readme.md" for e in entries):
            return f"Add a README: create a file called {_q(target + '/README.md')}"
        return f"See what's inside: list {_q(target)}"

    if a == "create_file" and target:
        if _is_file(ws, target) and ws.resolve(target).stat().st_size == 0:
            return f"Add some text: append 'your text' to {_q(target)}"
        return f"Check it: read {_q(target)}"

    if a == "edit_file" and target:
        return f"Review the result: read {_q(target)}"

    if a == "read_file" and target:
        return f"Add a line: append 'new line' to {_q(target)}"

    if a in ("rename_path", "move_path", "copy_path") and target:
        if _is_file(ws, target):
            return f"Open it: read {_q(target)}"
        return f"See what's inside: list {_q(target)}"

    if a in ("delete_file", "delete_folder"):
        return "See what's left: list the workspace"

    if a == "list_folder":
        folder = target  # None means the workspace root
        entries = _safe_listing(ws, folder) or []
        if not entries:
            where = f"{folder}/" if folder else ""
            return f"Start something: create a file called {_q(where + 'notes.txt')}"
        prefix = f"{folder}/" if folder else ""
        files = sorted((e.name for e in entries if e.is_file(follow_symlinks=False)), key=str.lower)
        dirs = sorted((e.name for e in entries if e.is_dir(follow_symlinks=False)), key=str.lower)
        if files:
            return f"Open a file: read {_q(prefix + files[0])}"
        if dirs:
            return f"Look inside: list {_q(prefix + dirs[0])}"
    return None


def capabilities_rows() -> list[tuple[str, str]]:
    return list(CAPABILITIES)


__all__ = ["suggest", "capabilities_rows", "EXAMPLE_COMMANDS", "OFF_TOPIC_MESSAGE"]
