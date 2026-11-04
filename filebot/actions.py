"""Filesystem action handlers (fixed allowlist).

Each handler works in two phases:

1. ``plan_*`` validates the request against the current workspace state and returns a
   ``PlannedAction`` with a human-readable summary and, when the action deletes or
   overwrites something, a confirmation prompt.
2. ``PlannedAction.execute(confirmed)`` re-resolves every path, re-checks the state, and
   then performs the operation. If something changed in a way that would now need a
   confirmation the user did not give, the action is refused.

No handler runs shell commands, subprocesses or generated code.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from .schemas import Intent
from .security import PathSecurityError, Workspace, is_link_stat, safe_rmtree

ResultKind = Literal["success", "warning", "error", "info"]
MAX_LIST_ENTRIES = 300


class ActionError(Exception):
    """A request that cannot be carried out; the message is safe to show to the user."""


@dataclass
class ListEntry:
    name: str
    is_dir: bool
    size: int | None
    is_link: bool = False


@dataclass
class ActionResult:
    action: str
    kind: ResultKind
    message: str
    target: str | None = None          # workspace-relative display path of the main result
    related: list[str] = field(default_factory=list)  # other paths useful for follow-ups
    content: str | None = None          # read_file output
    listing: list[ListEntry] | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.kind in ("success", "info")


@dataclass
class PlannedAction:
    action: str
    summary: str                         # exact resolved action and target(s)
    confirm_prompt: str | None           # None = no confirmation needed
    _run: Callable[[bool], ActionResult]
    resolved: str = ""                   # absolute resolved target, shown in confirmations

    @property
    def needs_confirmation(self) -> bool:
        return self.confirm_prompt is not None

    def execute(self, confirmed: bool = False) -> ActionResult:
        if self.needs_confirmation and not confirmed:
            return ActionResult(self.action, "warning", "Cancelled. Nothing was changed.")
        try:
            return self._run(confirmed)
        except PathSecurityError as exc:
            return ActionResult(self.action, "error", f"Blocked: {exc}")
        except ActionError as exc:
            return ActionResult(self.action, "error", str(exc))
        except OSError as exc:
            return ActionResult(self.action, "error", friendly_os_error(exc))


# ---------------------------------------------------------------------------- errors
def friendly_os_error(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    name = Path(exc.filename).name if getattr(exc, "filename", None) else "the item"
    if winerror in (32, 33):
        return f"'{name}' is locked or in use by another program. Close it and try again."
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return f"Permission denied for '{name}'. It may be read-only, in use, or protected."
    if isinstance(exc, FileNotFoundError):
        return f"'{name}' no longer exists."
    if isinstance(exc, FileExistsError):
        return f"'{name}' already exists."
    if exc.errno == errno.ENOSPC:
        return "The disk is full."
    if exc.errno == errno.ENOTEMPTY or winerror == 145:
        return f"Folder '{name}' is not empty."
    if winerror == 206 or exc.errno == errno.ENAMETOOLONG:
        return "The path is too long for Windows."
    return f"The operation failed: {exc.strerror or exc}."


# ---------------------------------------------------------------------------- helpers
def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n}"


def _lexists(p: Path) -> bool:
    return os.path.lexists(p)


def _require_not_root(ws: Workspace, p: Path, verb: str) -> None:
    if ws.is_root(p) or os.path.normcase(str(p)) == os.path.normcase(str(ws.root)):
        raise ActionError(f"The workspace root itself cannot be {verb}.")


def _require_file(ws: Workspace, p: Path) -> None:
    if not _lexists(p):
        raise ActionError(f"File '{ws.display(p)}' does not exist.")
    if p.is_dir():
        raise ActionError(f"'{ws.display(p)}' is a folder, not a file.")


def _require_dir(ws: Workspace, p: Path) -> None:
    if not _lexists(p):
        raise ActionError(f"Folder '{ws.display(p)}' does not exist.")
    if not p.is_dir():
        raise ActionError(f"'{ws.display(p)}' is a file, not a folder.")


def _same_entry(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_inside(child: Path, parent: Path) -> bool:
    c = os.path.normcase(os.path.normpath(str(child)))
    p = os.path.normcase(os.path.normpath(str(parent)))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _ensure_parent(ws: Workspace, target: Path) -> None:
    parent = target.parent
    if parent.exists():
        if not parent.is_dir():
            raise ActionError(f"'{ws.display(parent)}' is a file, not a folder.")
        return
    parent.mkdir(parents=True, exist_ok=True)
    # Re-verify after creation that nothing escaped (e.g. a race that planted a link).
    ws.resolve(ws.display(target.parent) if parent != ws.root else "")


def _check_text_file(ws: Workspace, p: Path, max_bytes: int) -> str:
    """Return the decoded text of a UTF-8 file, or raise ActionError."""
    size = p.stat().st_size
    if size > max_bytes:
        raise ActionError(
            f"'{ws.display(p)}' is {_size(size)}, larger than the {_size(max_bytes)} limit for text files."
        )
    with open(p, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ActionError(f"'{ws.display(p)}' is larger than the {_size(max_bytes)} limit for text files.")
    if b"\x00" in data:
        raise ActionError(f"'{ws.display(p)}' looks like a binary file. Only UTF-8 text files are supported.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ActionError(f"'{ws.display(p)}' is not a UTF-8 text file.") from None


def _write_text(p: Path, text: str, mode: str) -> None:
    with open(p, mode, encoding="utf-8", newline="") as fh:
        fh.write(text)


def _resolve_destination(ws: Workspace, src: Path, destination: str, verb: str) -> Path:
    """Destination rules for move/copy: an existing folder means 'into it'; otherwise the
    destination is the new full path, and its parent folder must already exist."""
    dest = ws.resolve(destination)
    if dest.is_dir() and not _same_entry(dest, src):
        return ws.resolve(f"{ws.display(dest)}/{src.name}" if dest != ws.root else src.name)
    if _lexists(dest):
        return dest
    if not dest.parent.is_dir():
        raise ActionError(
            f"Destination folder '{ws.display(dest.parent)}' does not exist. "
            f"Create it first (e.g. 'create folder {ws.display(dest.parent)}'), then {verb} again."
        )
    if src.is_file() and not Path(destination).suffix and src.suffix:
        # 'move a.txt to archive' where archive does not exist: ambiguous, so refuse.
        raise ActionError(
            f"Folder '{ws.display(dest)}' does not exist. Create it first with "
            f"'create folder {ws.display(dest)}', or give a full new file name."
        )
    return dest


# ---------------------------------------------------------------------------- planners
def plan_create_file(ws: Workspace, path: str, content: str | None) -> PlannedAction:
    target = ws.resolve(path)
    _require_not_root(ws, target, "replaced")
    text = content or ""
    shown = ws.display(target)
    exists = _lexists(target)
    if exists and target.is_dir():
        raise ActionError(f"A folder named '{shown}' already exists.")
    confirm = None
    if exists:
        old_size = target.stat().st_size
        confirm = (
            f"OVERWRITE existing file '{shown}' ({_size(old_size)}) with "
            f"{_plural(len(text), 'character')} of new content? Its current contents will be lost."
        )

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        if _lexists(t) and t.is_dir():
            raise ActionError(f"A folder named '{shown}' already exists.")
        if _lexists(t):
            if not confirmed:
                raise ActionError(f"'{shown}' now exists; refusing to overwrite it without confirmation.")
            _write_text(t, text, "w")
            return ActionResult("create_file", "success", f"Overwrote file '{shown}'.", target=shown)
        _ensure_parent(ws, t)
        ws.resolve(path)  # re-check after creating parents
        _write_text(t, text, "x")
        detail = f" with {_plural(len(text), 'character')}" if text else " (empty)"
        return ActionResult("create_file", "success", f"Created file '{shown}'{detail}.", target=shown)

    summary = f"{'Overwrite' if exists else 'Create'} file '{shown}'"
    return PlannedAction("create_file", summary, confirm, run, str(target))


def plan_delete_file(ws: Workspace, path: str) -> PlannedAction:
    target = ws.resolve(path)
    _require_not_root(ws, target, "deleted")
    _require_file(ws, target)
    shown = ws.display(target)
    size = target.stat().st_size
    confirm = f"Permanently DELETE file '{shown}' ({_size(size)})? This cannot be undone."

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        _require_not_root(ws, t, "deleted")
        _require_file(ws, t)
        _unlink(t)
        return ActionResult("delete_file", "success", f"Deleted file '{shown}'.", target=None, related=[shown])

    return PlannedAction("delete_file", f"Delete file '{shown}'", confirm, run, str(target))


def _unlink(p: Path) -> None:
    try:
        os.unlink(p)
    except PermissionError:
        st = os.lstat(p)
        if not st.st_mode & stat.S_IWRITE:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(p)
        else:
            raise


def plan_create_folder(ws: Workspace, path: str) -> PlannedAction:
    target = ws.resolve(path)
    shown = ws.display(target)
    if ws.is_root(target) or target == ws.root:
        raise ActionError("That is the workspace root, which already exists.")

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        if _lexists(t):
            if t.is_dir():
                return ActionResult("create_folder", "warning", f"Folder '{shown}' already exists.", target=shown)
            raise ActionError(f"A file named '{shown}' already exists.")
        t.mkdir(parents=True, exist_ok=False)
        ws.resolve(path)
        return ActionResult("create_folder", "success", f"Created folder '{shown}'.", target=shown)

    return PlannedAction("create_folder", f"Create folder '{shown}'", None, run)


def plan_delete_folder(ws: Workspace, path: str) -> PlannedAction:
    target = ws.resolve(path)
    _require_not_root(ws, target, "deleted")
    _require_dir(ws, target)
    shown = ws.display(target)
    stats = ws.tree_stats(target)
    parts = [_plural(stats.files, "file"), _plural(stats.folders, "subfolder")]
    if stats.links:
        parts.append(_plural(stats.links, "link") + " (removed as links; targets untouched)")
    contents = "it is empty" if not (stats.files or stats.folders or stats.links) else "it contains " + ", ".join(parts)
    confirm = (
        f"Permanently DELETE folder '{shown}' and everything inside it ({contents})? "
        "This cannot be undone."
    )

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        _require_not_root(ws, t, "deleted")
        _require_dir(ws, t)
        safe_rmtree(t)
        return ActionResult("delete_folder", "success", f"Deleted folder '{shown}' ({contents}).", related=[shown])

    return PlannedAction("delete_folder", f"Delete folder '{shown}'", confirm, run, str(target))


def plan_list_folder(ws: Workspace, path: str | None) -> PlannedAction:
    target = ws.resolve(path)
    _require_dir(ws, target)
    shown = ws.display(target)

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        _require_dir(ws, t)
        entries: list[ListEntry] = []
        with os.scandir(t) as it:
            for entry in it:
                st = entry.stat(follow_symlinks=False)
                link = is_link_stat(st)
                is_dir = (not link) and stat.S_ISDIR(st.st_mode)
                entries.append(ListEntry(entry.name, is_dir, None if is_dir or link else st.st_size, link))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        truncated = len(entries) > MAX_LIST_ENTRIES
        n_dirs = sum(e.is_dir for e in entries)
        n_files = len(entries) - n_dirs
        msg = (
            f"'{shown}' is empty."
            if not entries
            else f"'{shown}' contains {_plural(n_files, 'file')} and {_plural(n_dirs, 'folder')}."
        )
        return ActionResult(
            "list_folder", "info", msg, target=None if t == ws.root else shown,
            listing=entries[:MAX_LIST_ENTRIES], truncated=truncated,
        )

    return PlannedAction("list_folder", f"List folder '{shown}'", None, run)


def plan_rename_path(ws: Workspace, path: str, new_name: str) -> PlannedAction:
    src = ws.resolve(path)
    _require_not_root(ws, src, "renamed")
    if not _lexists(src):
        raise ActionError(f"'{ws.display(src)}' does not exist.")
    cleaned = new_name.replace("\\", "/").strip("/")
    if "/" in cleaned:
        dest_rel = cleaned
    else:
        parent_rel = ws.display(src.parent)
        dest_rel = cleaned if src.parent == ws.root else f"{parent_rel}/{cleaned}"
    dest = ws.resolve(dest_rel)
    _require_not_root(ws, dest, "replaced")
    shown_src, shown_dest = ws.display(src), ws.display(dest)
    case_only = os.path.normcase(str(src)) == os.path.normcase(str(dest))
    if str(src) == str(dest):
        raise ActionError(f"'{shown_src}' already has that name.")
    if _lexists(dest) and not case_only:
        raise ActionError(f"Cannot rename: '{shown_dest}' already exists. Nothing was overwritten.")
    if not dest.parent.is_dir():
        raise ActionError(f"Folder '{ws.display(dest.parent)}' does not exist.")
    if src.is_dir() and _is_inside(dest, src):
        raise ActionError("A folder cannot be moved inside itself.")

    def run(confirmed: bool) -> ActionResult:
        s, d = ws.resolve(path), ws.resolve(dest_rel)
        _require_not_root(ws, s, "renamed")
        if not _lexists(s):
            raise ActionError(f"'{shown_src}' no longer exists.")
        if _lexists(d) and not case_only:
            raise ActionError(f"'{shown_dest}' now exists; nothing was overwritten.")
        os.rename(s, d)
        return ActionResult(
            "rename_path", "success", f"Renamed '{shown_src}' to '{shown_dest}'.", target=shown_dest, related=[shown_src]
        )

    return PlannedAction("rename_path", f"Rename '{shown_src}' to '{shown_dest}'", None, run)


def _plan_move_or_copy(ws: Workspace, action: str, path: str, destination: str, max_copy_bytes: int) -> PlannedAction:
    verb = "move" if action == "move_path" else "copy"
    src = ws.resolve(path)
    _require_not_root(ws, src, "moved" if verb == "move" else "copied")
    if not _lexists(src):
        raise ActionError(f"'{ws.display(src)}' does not exist.")
    dest = _resolve_destination(ws, src, destination, verb)
    shown_src, shown_dest = ws.display(src), ws.display(dest)
    dest_rel = shown_dest
    if _same_entry(src, dest) or os.path.normcase(str(src)) == os.path.normcase(str(dest)):
        raise ActionError(f"'{shown_src}' is already at that location.")
    if _lexists(dest):
        raise ActionError(f"Cannot {verb}: '{shown_dest}' already exists. Nothing was overwritten.")
    if src.is_dir() and _is_inside(dest, src):
        raise ActionError(f"A folder cannot be {verb}d into itself.")
    if verb == "copy" and src.is_dir():
        stats = ws.tree_stats(src)
        if stats.links:
            raise ActionError("That folder contains symbolic links or junctions, so it will not be copied.")
        if stats.total_bytes > max_copy_bytes:
            raise ActionError(f"That folder is {_size(stats.total_bytes)}; copies are limited to {_size(max_copy_bytes)}.")

    def run(confirmed: bool) -> ActionResult:
        s, d = ws.resolve(path), ws.resolve(dest_rel)
        _require_not_root(ws, s, "moved")
        if not _lexists(s):
            raise ActionError(f"'{shown_src}' no longer exists.")
        if _lexists(d):
            raise ActionError(f"'{shown_dest}' now exists; nothing was overwritten.")
        if not d.parent.is_dir():
            raise ActionError(f"Folder '{ws.display(d.parent)}' no longer exists.")
        if verb == "move":
            os.rename(s, d)
            return ActionResult(
                action, "success", f"Moved '{shown_src}' to '{shown_dest}'.", target=shown_dest, related=[shown_src]
            )
        if s.is_dir():
            if ws.tree_stats(s).links:
                raise ActionError("That folder now contains links; copy refused.")
            shutil.copytree(s, d, symlinks=True)
        else:
            shutil.copyfile(s, d, follow_symlinks=False)
            shutil.copystat(s, d, follow_symlinks=False)
        return ActionResult(action, "success", f"Copied '{shown_src}' to '{shown_dest}'.", target=shown_dest, related=[shown_src])

    return PlannedAction(action, f"{verb.capitalize()} '{shown_src}' to '{shown_dest}'", None, run)


def plan_move_path(ws: Workspace, path: str, destination: str, max_copy_bytes: int = 500 * 1024 * 1024) -> PlannedAction:
    return _plan_move_or_copy(ws, "move_path", path, destination, max_copy_bytes)


def plan_copy_path(ws: Workspace, path: str, destination: str, max_copy_bytes: int = 500 * 1024 * 1024) -> PlannedAction:
    return _plan_move_or_copy(ws, "copy_path", path, destination, max_copy_bytes)


def plan_read_file(ws: Workspace, path: str, max_bytes: int) -> PlannedAction:
    target = ws.resolve(path)
    _require_file(ws, target)
    shown = ws.display(target)

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        _require_file(ws, t)
        text = _check_text_file(ws, t, max_bytes)
        note = "(empty file)" if not text else f"{_plural(len(text.splitlines()), 'line')}"
        return ActionResult("read_file", "info", f"Contents of '{shown}' {note}:", target=shown, content=text)

    return PlannedAction("read_file", f"Read file '{shown}'", None, run)


def plan_edit_file(ws: Workspace, path: str, content: str, mode: str, max_bytes: int) -> PlannedAction:
    if mode not in ("replace", "append"):
        raise ActionError("Edit mode must be 'replace' or 'append'.")
    target = ws.resolve(path)
    _require_file(ws, target)
    shown = ws.display(target)
    existing = _check_text_file(ws, target, max_bytes)
    confirm = None
    if mode == "replace" and existing:
        confirm = (
            f"OVERWRITE the contents of '{shown}' ({_size(len(existing.encode('utf-8')))}) with "
            f"{_plural(len(content), 'character')} of new text? The current contents will be lost."
        )

    def run(confirmed: bool) -> ActionResult:
        t = ws.resolve(path)
        _require_file(ws, t)
        current = _check_text_file(ws, t, max_bytes)
        if mode == "replace":
            if current and not confirmed:
                raise ActionError(f"'{shown}' now has content; refusing to overwrite it without confirmation.")
            _write_text(t, content, "w")
            return ActionResult("edit_file", "success", f"Replaced the contents of '{shown}'.", target=shown)
        if len(current.encode("utf-8")) + len(content.encode("utf-8")) > max_bytes:
            raise ActionError(f"Appending would make '{shown}' larger than the {_size(max_bytes)} limit.")
        prefix = "\n" if current and not current.endswith("\n") else ""
        _write_text(t, prefix + content, "a")
        return ActionResult("edit_file", "success", f"Appended {_plural(len(content), 'character')} to '{shown}'.", target=shown)

    summary = f"{'Replace contents of' if mode == 'replace' else 'Append text to'} '{shown}'"
    return PlannedAction("edit_file", summary, confirm, run, str(target))


# ---------------------------------------------------------------------------- dispatch
def plan_action(ws: Workspace, intent: Intent, max_read_bytes: int) -> PlannedAction:
    """Dispatch a complete, grounded intent to the matching handler (fixed allowlist)."""
    a = intent.action
    if a == "create_file":
        return plan_create_file(ws, intent.path or "", intent.content)
    if a == "delete_file":
        return plan_delete_file(ws, intent.path or "")
    if a == "create_folder":
        return plan_create_folder(ws, intent.path or "")
    if a == "delete_folder":
        return plan_delete_folder(ws, intent.path or "")
    if a == "list_folder":
        return plan_list_folder(ws, intent.path)
    if a == "rename_path":
        return plan_rename_path(ws, intent.path or "", intent.destination or "")
    if a == "move_path":
        return plan_move_path(ws, intent.path or "", intent.destination or "")
    if a == "copy_path":
        return plan_copy_path(ws, intent.path or "", intent.destination or "")
    if a == "read_file":
        return plan_read_file(ws, intent.path or "", max_read_bytes)
    if a == "edit_file":
        return plan_edit_file(ws, intent.path or "", intent.content or "", intent.mode or "", max_read_bytes)
    raise ActionError(f"'{a}' is not a file action.")
