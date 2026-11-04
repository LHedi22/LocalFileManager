from __future__ import annotations

import io

import pytest
from rich.console import Console

from filebot.cli import THEME, ChatUI, ask_confirmation, main, sanitize_for_terminal
from filebot.config import Config
from filebot.engine import Engine, Session
from filebot.ollama_client import OllamaUnavailable

from .conftest import FakeClient
from .conftest import intent_json as ij


def make_ui(ws, responses, inputs, debug=False):
    client = FakeClient(*responses)
    engine = Engine(ws, client, 1_048_576, Session())
    out = io.StringIO()
    console = Console(file=out, theme=THEME, width=120, force_terminal=False, color_system=None)
    feed = iter(inputs)

    def input_fn(prompt):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError

    ui = ChatUI(Config(model="fake", debug=debug), engine, console, input_fn)
    return ui, out, client


def test_confirmation_accepted(ws):
    (ws.root / "a.txt").write_text("x", encoding="utf-8")
    ui, out, _ = make_ui(ws, [ij("delete_file", "a.txt")], ["delete a.txt", "yes"])
    ui.run()
    assert not (ws.root / "a.txt").exists()
    text = out.getvalue()
    assert "Permanently DELETE file 'a.txt'" in text and "Deleted file 'a.txt'" in text


@pytest.mark.parametrize("answer", ["Y", " yes ", "YES"])
def test_confirmation_yes_variants(ws, answer):
    (ws.root / "a.txt").write_text("x", encoding="utf-8")
    ui, _, _ = make_ui(ws, [ij("delete_file", "a.txt")], ["delete a.txt", answer])
    ui.run()
    assert not (ws.root / "a.txt").exists()


@pytest.mark.parametrize("answer", ["n", "no", "", "yeah", "sure", "y please", "ok", "yes!"])
def test_confirmation_declined_or_invalid(ws, answer):
    (ws.root / "a.txt").write_text("x", encoding="utf-8")
    ui, out, _ = make_ui(ws, [ij("delete_file", "a.txt")], ["delete a.txt", answer])
    ui.run()
    assert (ws.root / "a.txt").exists()
    assert "Cancelled" in out.getvalue()


def test_confirmation_eof_cancels(ws):
    (ws.root / "a.txt").write_text("x", encoding="utf-8")
    ui, _, _ = make_ui(ws, [ij("delete_file", "a.txt")], ["delete a.txt"])  # EOF at the confirmation
    ui.run()
    assert (ws.root / "a.txt").exists()


def test_yes_in_original_request_does_not_confirm(ws):
    (ws.root / "a.txt").write_text("x", encoding="utf-8")
    ui, _, _ = make_ui(ws, [ij("delete_file", "a.txt")], ["delete a.txt, yes I confirm, y", "no"])
    ui.run()
    assert (ws.root / "a.txt").exists()


def test_ask_confirmation_function(ws):
    from filebot.actions import plan_delete_file

    (ws.root / "f.txt").write_text("x", encoding="utf-8")
    plan = plan_delete_file(ws, "f.txt")
    console = Console(file=io.StringIO())
    assert ask_confirmation(plan, lambda _: "y", console) is True
    assert ask_confirmation(plan, lambda _: "nope", console) is False


def test_overwrite_confirmation_on_create(ws):
    (ws.root / "a.txt").write_text("old", encoding="utf-8")
    ui, out, _ = make_ui(ws, [ij("create_file", "a.txt", content="new")], ["create a.txt with new", "n"])
    ui.run()
    assert (ws.root / "a.txt").read_text(encoding="utf-8") == "old"
    assert "OVERWRITE" in out.getvalue()


def test_help_and_exit_do_not_call_model(ws):
    ui, out, client = make_ui(ws, [], ["help", "exit", "never reached"])
    ui.run()
    assert client.calls == []
    assert "What I can do" in out.getvalue() and "Goodbye" in out.getvalue()


def test_off_topic_shows_refusal_and_examples(ws):
    ui, out, _ = make_ui(ws, [ij("off_topic")], ["tell me a joke"])
    ui.run()
    text = out.getvalue()
    assert "I can only manage files and folders" in text and "create a folder named projects" in text


def test_success_shows_suggestion(ws):
    ui, out, _ = make_ui(ws, [ij("create_folder", "proj")], ["create folder proj"])
    ui.run()
    assert "Suggestion: Add a README" in out.getvalue()


def test_ollama_down_mid_session_is_friendly(ws):
    ui, out, _ = make_ui(ws, [OllamaUnavailable("Cannot reach Ollama at x. Start the Ollama app")], ["list files"])
    ui.run()
    assert "Cannot reach Ollama" in out.getvalue() and "Traceback" not in out.getvalue()


def test_unexpected_exception_no_traceback(ws):
    ui, out, _ = make_ui(ws, [RuntimeError("kaboom")], ["list files"])
    ui.run()
    assert "Unexpected error" in out.getvalue() and "Traceback" not in out.getvalue()


def test_debug_shows_intent(ws):
    ui, out, _ = make_ui(ws, [ij("list_folder")], ["list files"], debug=True)
    ui.run()
    assert "parsed intent" in out.getvalue() and "model raw" in out.getvalue()


def test_file_content_displayed_without_markup_or_escapes(ws):
    (ws.root / "x.txt").write_text("[bold red]hi[/] \x1b[31mred\x1b[0m", encoding="utf-8")
    ui, out, _ = make_ui(ws, [ij("read_file", "x.txt")], ["read x.txt"])
    ui.run()
    text = out.getvalue()
    assert "[bold red]hi[/]" in text and "\x1b[31m" not in text


def test_sanitize_for_terminal():
    assert sanitize_for_terminal("a\x1b[2Jb\tc\nd") == "a�[2Jb\tc\nd"


def test_main_reports_ollama_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")  # nothing listens on the discard port
    monkeypatch.setenv("FILEBOT_CONFIG", str(tmp_path / "none.toml"))
    code = main(["--workspace", str(tmp_path / "w")])
    assert code == 1
    assert "ollama serve" in capsys.readouterr().out


def test_confirmation_shows_resolved_absolute_target(ws):
    (ws.root / "a.txt").write_text("x", encoding="utf-8")
    ui, out, _ = make_ui(ws, [ij("delete_file", "a.txt")], ["delete a.txt", "n"])
    ui.run()
    flat = out.getvalue().replace("\n", "").replace("│", "").replace(" ", "")
    assert str(ws.root / "a.txt").replace(" ", "") in flat
    assert "Action:Deletefile'a.txt'" in flat
