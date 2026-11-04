"""Workspace path security.

Every path that comes from the user or the model goes through ``Workspace.resolve``.
It rejects anything that could reach outside the workspace:

* absolute paths outside the workspace, drive-relative paths (``C:foo``), and rooted
  paths (``\\foo``)
* UNC paths (``\\\\server\\share``) and device paths (``\\\\?\\``, ``\\\\.\\``)
* ``..`` components
* alternate data streams (any ``:`` in a component)
* Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9, ...)
* characters Windows forbids, and trailing dots/spaces that Windows silently strips
* symbolic links, junctions and other name-surrogate reparse points anywhere
  between the workspace root and the target

After those checks it resolves the real path and verifies that it is still inside the
root, comparing case-insensitively on Windows.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

_RESERVED_BASE = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
_RESERVED_NUMBERED = {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in list("0123456789") + ["¹", "²", "³"]}
RESERVED_NAMES = frozenset(_RESERVED_BASE | _RESERVED_NUMBERED)

_INVALID_CHARS = set('<>"|?*') | {chr(i) for i in range(32)}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
MAX_COMPONENT_LEN = 255

# Reparse tags with the "name surrogate" bit set redirect to another location
# (symlinks, junctions/mount points, and similar). Cloud placeholders (OneDrive)
# do not set this bit and are treated as ordinary files.
_NAME_SURROGATE_BIT = 0x20000000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class PathSecurityError(Exception):
    """A path was rejected by the workspace security rules."""


def is_link_stat(st: os.stat_result) -> bool:
    """True if an lstat result describes a symlink, junction or other redirecting reparse point."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", 0)
        # If the tag is unknown, be conservative and treat it as a link.
        return tag == 0 or bool(tag & _NAME_SURROGATE_BIT)
    return False


def is_link(path: str | os.PathLike) -> bool:
    try:
        return is_link_stat(os.lstat(path))
    except OSError:
        return False


def validate_component(part: str) -> None:
    """Validate one path component (a single file or folder name)."""
    if part == "..":
        raise PathSecurityError("Parent-folder references ('..') are not allowed.")
    if ":" in part:
        raise PathSecurityError(
            f"'{part}' contains ':' - drive letters and alternate data streams are not allowed."
        )
    bad = sorted({c for c in part if c in _INVALID_CHARS})
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise PathSecurityError(f"'{part}' contains characters Windows does not allow in names: {shown}.")
    if part != part.rstrip(" ."):
        raise PathSecurityError(f"'{part}' ends with a dot or space, which Windows does not allow.")
    if part != part.lstrip(" "):
        raise PathSecurityError(f"'{part}' starts with a space.")
    if len(part) > MAX_COMPONENT_LEN:
        raise PathSecurityError("A file or folder name is longer than 255 characters.")
    stem = part.split(".")[0].rstrip(" ").upper()
    if stem in RESERVED_NAMES:
        raise PathSecurityError(f"'{part}' uses the reserved Windows device name '{stem}'.")


@dataclass(frozen=True)
class TreeStats:
    files: int = 0
    folders: int = 0
    links: int = 0
    total_bytes: int = 0


class Workspace:
    """A sandbox directory. All file operations must use paths returned by ``resolve``."""

    def __init__(self, root: str | os.PathLike, create: bool = True):
        raw = Path(root).expanduser()
        if create:
            try:
                raw.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PathSecurityError(f"Could not create workspace folder {raw}: {exc.strerror or exc}") from None
        if not raw.is_dir():
            raise PathSecurityError(f"Workspace {raw} is not a folder.")
        self.root = Path(os.path.realpath(raw))
        self._root_key = os.path.normcase(str(self.root))

    # ------------------------------------------------------------------ helpers
    def _key(self, path: str | os.PathLike) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    def contains(self, path: str | os.PathLike) -> bool:
        """True if ``path`` (already absolute) is the root or inside it (case-insensitive on Windows)."""
        key = self._key(path)
        if key == self._root_key:
            return True
        root = self._root_key if self._root_key.endswith(os.sep) else self._root_key + os.sep
        return key.startswith(root)

    def is_root(self, path: str | os.PathLike) -> bool:
        return self._key(os.path.realpath(path)) == self._root_key

    def display(self, path: str | os.PathLike) -> str:
        """Workspace-relative path with forward slashes, for messages."""
        rel = os.path.relpath(str(path), str(self.root))
        if rel in (".", ""):
            return "(workspace root)"
        return rel.replace(os.sep, "/")

    def _check_root_intact(self) -> None:
        if self._key(os.path.realpath(self.root)) != self._root_key or not self.root.is_dir():
            raise PathSecurityError("The workspace folder has been moved, removed or replaced by a link.")

    # ------------------------------------------------------------------ resolve
    def _split(self, user_path: str) -> list[str]:
        text = user_path.strip()
        if "\x00" in text:
            raise PathSecurityError("Path contains a null character.")
        text = text.replace("/", "\\")
        if text.startswith("\\\\"):
            raise PathSecurityError("Network (UNC) and device paths are not allowed.")
        if _DRIVE_RE.match(text):
            if len(text) > 2 and text[2] == "\\":
                # Absolute Windows path: allowed only if it points inside the workspace.
                parts_abs = [p for p in text[3:].split("\\") if p not in ("", ".")]
                for part in parts_abs:
                    if part == "..":
                        raise PathSecurityError("Parent-folder references ('..') are not allowed.")
                absolute = os.path.normpath(text[:3] + "\\".join(parts_abs))
                if not self.contains(absolute):
                    raise PathSecurityError("Absolute paths outside the workspace are not allowed.")
                rel = os.path.relpath(absolute, str(self.root))
                return [] if rel == "." else rel.split(os.sep)
            raise PathSecurityError("Drive-relative paths like 'C:folder' are not allowed.")
        if text.startswith("\\"):
            raise PathSecurityError(
                "Absolute paths outside the workspace are not allowed. Use a path relative to the workspace."
            )
        if text.startswith("~"):
            raise PathSecurityError("Home-folder paths ('~') are not allowed. Use a path relative to the workspace.")
        return [p for p in text.split("\\") if p not in ("", ".")]

    def resolve(self, user_path: str | None) -> Path:
        """Validate a user/model supplied path and return the absolute path inside the workspace.

        ``None`` or an empty string means the workspace root.
        """
        self._check_root_intact()
        parts = self._split(user_path or "")
        for part in parts:
            validate_component(part)

        candidate = self.root.joinpath(*parts) if parts else self.root

        # Refuse any link/junction along the chain - never follow them.
        current = self.root
        for part in parts:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                break
            except NotADirectoryError:
                raise PathSecurityError(f"'{self.display(current.parent)}' is a file, not a folder.") from None
            except OSError as exc:
                raise PathSecurityError(f"Cannot inspect '{self.display(current)}': {exc.strerror or exc}") from None
            if is_link_stat(st):
                raise PathSecurityError(
                    f"'{self.display(current)}' is a symbolic link or junction; links are not followed."
                )

        real = os.path.realpath(candidate)
        if not self.contains(real) or not self.contains(candidate):
            raise PathSecurityError("That path points outside the workspace.")
        return candidate

    # ------------------------------------------------------------------ tree helpers
    def tree_stats(self, folder: Path) -> TreeStats:
        """Count files, subfolders and links below ``folder`` without following links."""
        files = folders = links = total = 0
        stack = [str(folder)]
        while stack:
            current = stack.pop()
            with os.scandir(current) as it:
                for entry in it:
                    st = entry.stat(follow_symlinks=False)
                    if is_link_stat(st):
                        links += 1
                    elif stat.S_ISDIR(st.st_mode):
                        folders += 1
                        stack.append(entry.path)
                    else:
                        files += 1
                        total += st.st_size
        return TreeStats(files, folders, links, total)


def safe_rmtree(folder: Path) -> None:
    """Delete a folder tree without ever following symlinks or junctions.

    Links found inside are removed as links; their targets are never touched.
    """
    st = os.lstat(folder)
    if is_link_stat(st):
        raise PathSecurityError("Refusing to delete through a link.")
    with os.scandir(folder) as it:
        entries = list(it)
    for entry in entries:
        est = entry.stat(follow_symlinks=False)
        if is_link_stat(est):
            # A directory junction/symlink is removed with rmdir, a file symlink with unlink.
            is_dir_link = IS_WINDOWS and bool(getattr(est, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY)
            if is_dir_link:
                os.rmdir(entry.path)
            else:
                os.unlink(entry.path)
        elif stat.S_ISDIR(est.st_mode):
            safe_rmtree(Path(entry.path))
        else:
            _unlink_file(entry.path)
    os.rmdir(folder)


def _unlink_file(path: str) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        # Clear the read-only attribute (the user already confirmed deletion) and retry once.
        st = os.lstat(path)
        if not st.st_mode & stat.S_IWRITE:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(path)
        else:
            raise
