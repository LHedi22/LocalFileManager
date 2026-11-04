from __future__ import annotations

import pytest

from filebot.actions import ActionResult, plan_create_folder, plan_list_folder
from filebot.config import ConfigError, load_config
from filebot.suggestions import suggest


def test_config_precedence(tmp_path):
    f = tmp_path / "filebot.toml"
    f.write_text('[filebot]\nmodel = "from-file"\nmax_read_bytes = 2048\nworkspace = "ws_here"\n', encoding="utf-8")
    cfg = load_config(f, env={})
    assert cfg.model == "from-file" and cfg.max_read_bytes == 2048 and cfg.workspace == tmp_path / "ws_here"
    cfg = load_config(f, env={"FILEBOT_MODEL": "from-env", "FILEBOT_MAX_READ_BYTES": "10"})
    assert cfg.model == "from-env" and cfg.max_read_bytes == 10
    cfg = load_config(f, env={"FILEBOT_MODEL": "from-env"}, model="from-cli", workspace=tmp_path / "w2", debug=True)
    assert cfg.model == "from-cli" and cfg.workspace == tmp_path / "w2" and cfg.debug


def test_config_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.toml", env={})
    assert cfg.max_read_bytes == 1_048_576 and cfg.workspace.name == "workspace"


def test_config_ollama_host_normalised(tmp_path):
    assert load_config(tmp_path / "x.toml", env={"OLLAMA_HOST": "127.0.0.1:11434"}).ollama_host == "http://127.0.0.1:11434"


@pytest.mark.parametrize("bad", ["abc", "-5", "0"])
def test_config_bad_values(tmp_path, bad):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "x.toml", env={"FILEBOT_MAX_READ_BYTES": bad})


def test_config_bad_toml(tmp_path):
    f = tmp_path / "filebot.toml"
    f.write_text("model = = broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(f, env={})


DESTRUCTIVE = ("delete", "remove", "overwrite", "replace")


def test_suggestions_are_non_destructive(ws):
    r = plan_create_folder(ws, "proj").execute()
    tip = suggest(r, ws)
    assert tip and "README" in tip
    (ws.root / "proj" / "README.md").write_text("x", encoding="utf-8")
    tip2 = suggest(r, ws)
    assert "list" in tip2
    for res in (
        ActionResult("delete_file", "success", "Deleted", related=["a.txt"]),
        ActionResult("read_file", "info", "x", target="a.txt", content="x"),
        ActionResult("edit_file", "success", "x", target="a.txt"),
        plan_list_folder(ws, None).execute(),
    ):
        t = suggest(res, ws)
        assert t is None or not any(w in t.lower() for w in DESTRUCTIVE)


def test_no_suggestion_on_error(ws):
    assert suggest(ActionResult("create_file", "error", "boom"), ws) is None


def test_list_empty_suggests_create(ws):
    r = plan_list_folder(ws, None).execute()
    assert "create a file" in suggest(r, ws)
