from __future__ import annotations

import os
from pathlib import Path

import pytest

from filebot.actions import ActionError, plan_delete_folder, plan_list_folder, plan_read_file
from filebot.security import PathSecurityError, Workspace, safe_rmtree, validate_component

from .conftest import make_junction, make_symlink


def test_workspace_created(tmp_path):
    ws = Workspace(tmp_path / "new_ws")
    assert ws.root.is_dir()


@pytest.mark.parametrize("rel", ["a.txt", "docs/a.txt", "docs\\sub\\b.md", "./x", "My Notes/file 1.txt", "."])
def test_valid_paths_resolve_inside(ws, rel):
    p = ws.resolve(rel)
    assert ws.contains(p)


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        "../outside.txt",
        "docs/../../x",
        "docs\\..\\..\\x",
        "a/../b",  # any '..' is refused, even if it would stay inside
    ],
)
def test_traversal_blocked(ws, bad):
    with pytest.raises(PathSecurityError):
        ws.resolve(bad)


@pytest.mark.parametrize("bad", ["C:\\Windows\\win.ini", "c:/windows/system32", "D:\\data", "/etc/passwd", "\\Windows", "C:relative.txt"])
def test_absolute_outside_blocked(ws, bad):
    with pytest.raises(PathSecurityError):
        ws.resolve(bad)


def test_absolute_inside_allowed(ws):
    inside = str(ws.root / "docs" / "a.txt")
    assert ws.resolve(inside) == ws.root / "docs" / "a.txt"
    # case-insensitive match of the workspace prefix on Windows
    if os.name == "nt":
        assert ws.contains(Path(str(ws.root).upper()) / "x")
        assert ws.resolve(inside.swapcase()).parent.name.lower() == "docs"


def test_absolute_with_dotdot_blocked(ws):
    with pytest.raises(PathSecurityError):
        ws.resolve(str(ws.root) + "\\..\\escape.txt")


@pytest.mark.parametrize("bad", ["\\\\server\\share\\file.txt", "//server/share/x", "\\\\?\\C:\\Windows", "\\\\.\\PhysicalDrive0", "//./COM1"])
def test_unc_and_device_paths_blocked(ws, bad):
    with pytest.raises(PathSecurityError):
        ws.resolve(bad)


@pytest.mark.parametrize(
    "bad", ["CON", "con.txt", "PRN", "aux.log", "NUL", "nul.tar.gz", "COM1", "com9.txt", "LPT1", "lpt3.md", "docs/CON/x.txt", "CONIN$", "COM¹"]
)
def test_reserved_names_blocked(ws, bad):
    with pytest.raises(PathSecurityError, match="reserved"):
        ws.resolve(bad)


@pytest.mark.parametrize("ok", ["console.txt", "auxiliary", "com10", "nullable.py", "lpt.txt", "CONTRIBUTING.md"])
def test_names_similar_to_reserved_allowed(ws, ok):
    assert ws.resolve(ok).name == ok


@pytest.mark.parametrize("bad", ["file.txt:hidden", "file.txt::$DATA", "docs:stream/a.txt", "a.txt:$DATA"])
def test_alternate_data_streams_blocked(ws, bad):
    with pytest.raises(PathSecurityError):
        ws.resolve(bad)


@pytest.mark.parametrize("bad", ["a?.txt", "b*.txt", "c<d", "e>f", 'g"h', "i|j", "trailing.", "trailing /x", "a/ leading", "dot./x", "nul\x00byte", "tab\tname"])
def test_invalid_characters_blocked(ws, bad):
    with pytest.raises(PathSecurityError):
        ws.resolve(bad)


def test_home_path_blocked(ws):
    with pytest.raises(PathSecurityError):
        ws.resolve("~/Documents")


def test_long_component_blocked():
    with pytest.raises(PathSecurityError):
        validate_component("a" * 256)


def test_root_detection(ws):
    assert ws.is_root(ws.resolve(""))
    assert ws.is_root(ws.resolve("."))
    assert ws.is_root(ws.resolve(None))
    assert ws.display(ws.root) == "(workspace root)"


# ------------------------------------------------------------------ links / junctions
def test_junction_escape_blocked(ws, outside):
    make_junction(ws.root / "escape", outside)
    for rel in ("escape", "escape/secret.txt", "escape\\new.txt"):
        with pytest.raises(PathSecurityError, match="link"):
            ws.resolve(rel)
    with pytest.raises(PathSecurityError):
        plan_read_file(ws, "escape/secret.txt", 1000)
    with pytest.raises(PathSecurityError):
        plan_list_folder(ws, "escape")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "top secret"


def test_junction_inside_workspace_also_refused(ws):
    (ws.root / "real").mkdir()
    make_junction(ws.root / "alias", ws.root / "real")
    with pytest.raises(PathSecurityError):
        ws.resolve("alias/file.txt")


def test_file_symlink_escape_blocked(ws, outside):
    make_symlink(ws.root / "link.txt", outside / "secret.txt")
    with pytest.raises(PathSecurityError, match="link"):
        ws.resolve("link.txt")


def test_dir_symlink_escape_blocked(ws, outside):
    make_symlink(ws.root / "dlink", outside, is_dir=True)
    with pytest.raises(PathSecurityError):
        ws.resolve("dlink/secret.txt")


def test_delete_folder_containing_junction_does_not_touch_target(ws, outside):
    (ws.root / "box").mkdir()
    (ws.root / "box" / "a.txt").write_text("a", encoding="utf-8")
    make_junction(ws.root / "box" / "portal", outside)
    plan = plan_delete_folder(ws, "box")
    assert "link" in plan.confirm_prompt
    result = plan.execute(confirmed=True)
    assert result.kind == "success", result.message
    assert not (ws.root / "box").exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "top secret"


def test_safe_rmtree_refuses_link_root(ws, outside):
    make_junction(ws.root / "j", outside)
    with pytest.raises(PathSecurityError):
        safe_rmtree(ws.root / "j")
    assert (outside / "secret.txt").exists()


def test_workspace_replaced_by_link_detected(tmp_path, outside):
    ws = Workspace(tmp_path / "ws2")
    os.rmdir(ws.root)
    make_junction(ws.root, outside)
    with pytest.raises(PathSecurityError):
        ws.resolve("secret.txt")


def test_root_cannot_be_given_via_absolute_path_to_delete(ws):
    with pytest.raises(ActionError):
        plan_delete_folder(ws, str(ws.root))
