"""Terminal interface (Rich). Handles help/exit, confirmations, and all user-facing output."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import __version__
from .actions import ActionResult, PlannedAction
from .config import Config, ConfigError, load_config
from .engine import Engine, EngineResponse
from .ollama_client import ModelNotFound, OllamaClient, OllamaError, OllamaUnavailable
from .security import PathSecurityError, Workspace
from .suggestions import EXAMPLE_COMMANDS, OFF_TOPIC_MESSAGE, capabilities_rows, suggest

THEME = Theme(
    {
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "info": "cyan",
        "confirm": "bold magenta",
        "muted": "dim",
        "prompt": "bold cyan",
    }
)

EXIT_WORDS = {"exit", "quit", "bye", ":q"}
HELP_WORDS = {"help", "?", "/help"}
YES_WORDS = {"y", "yes"}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]")


def sanitize_for_terminal(text: str) -> str:
    """Strip control characters (including ANSI escape sequences) from file content."""
    return _CONTROL_CHARS.sub("�", text)


def ask_confirmation(plan: PlannedAction, input_fn: Callable[[str], str], console: Console) -> bool:
    """Ask the user to confirm. Only an explicit 'y' or 'yes' typed now counts as yes."""
    body = Text()
    body.append("Action:  ", style="bold")
    body.append(plan.summary + "\n")
    if plan.resolved:
        body.append("Target:  ", style="bold")
        body.append(plan.resolved + "\n")
    body.append("\n" + (plan.confirm_prompt or plan.summary))
    console.print(Panel(body, title="Confirm", border_style="magenta"))
    try:
        answer = input_fn("Type 'y' or 'yes' to proceed, anything else cancels: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in YES_WORDS


class ChatUI:
    def __init__(self, config: Config, engine: Engine, console: Console | None = None,
                 input_fn: Callable[[str], str] | None = None):
        self.config = config
        self.engine = engine
        self.console = console or Console(theme=THEME, highlight=False)
        self._input = input_fn or (lambda prompt: self.console.input(f"[prompt]{escape(prompt)}[/]"))

    # -------------------------------------------------------------- output helpers
    def say(self, style: str, text: str) -> None:
        self.console.print(Text(text, style=style))

    def show_startup(self) -> None:
        body = Text()
        body.append(f"FileBot {__version__}", style="bold")
        body.append(" - manages files and folders in one workspace, locally via Ollama.\n")
        body.append("Model:     ", style="muted")
        body.append(self.config.model + "\n")
        body.append("Workspace: ", style="muted")
        body.append(str(self.engine.ws.root) + "\n")
        body.append("Type 'help' for examples, 'exit' to quit.", style="muted")
        if self.config.debug:
            body.append("\nDebug mode is ON.", style="warning")
        self.console.print(Panel(body, border_style="cyan"))

    def show_capabilities(self, intro: str | None = None) -> None:
        if intro:
            self.say("warning", intro)
        table = Table(title="What I can do", show_header=True, header_style="bold", border_style="dim")
        table.add_column("Action")
        table.add_column("Example")
        for name, example in capabilities_rows():
            table.add_row(name, Text(example))
        self.console.print(table)
        self.say("muted", "Deletes and overwrites always ask for confirmation. Type 'exit' to quit.")

    def show_refusal(self) -> None:
        self.say("warning", OFF_TOPIC_MESSAGE)
        self.say("muted", "For example, you could say:")
        for ex in EXAMPLE_COMMANDS:
            self.console.print(Text(f"  - {ex}"))

    def show_debug(self, lines: list[str]) -> None:
        if self.config.debug and lines:
            self.console.print(Panel(Text("\n".join(lines)), title="debug", border_style="dim", style="dim"))

    def show_result(self, result: ActionResult) -> None:
        self.say(result.kind if result.kind != "info" else "success", result.message)
        if result.listing is not None and result.listing:
            table = Table(show_header=True, header_style="bold", border_style="dim")
            table.add_column("Name")
            table.add_column("Type")
            table.add_column("Size", justify="right")
            for e in result.listing:
                kind = "link" if e.is_link else ("folder" if e.is_dir else "file")
                size = "" if e.size is None else f"{e.size:,} B"
                table.add_row(Text(e.name + ("/" if e.is_dir else "")), kind, size)
            self.console.print(table)
            if result.truncated:
                self.say("muted", "(list truncated)")
        if result.content is not None:
            self.console.print(
                Panel(Text(sanitize_for_terminal(result.content)), title=Text(result.target or ""), border_style="blue")
            )

    # -------------------------------------------------------------- main loop
    def handle_line(self, line: str) -> bool:
        """Process one input line. Returns False when the user wants to exit."""
        text = line.strip()
        if not text:
            return True
        low = text.lower()
        if low in EXIT_WORDS:
            return False
        if low in HELP_WORDS:
            self.show_capabilities()
            return True

        try:
            with self.console.status("[muted]Thinking...[/]", spinner="dots"):
                response = self.engine.handle(text)
        except ModelNotFound as exc:
            self.say("error", str(exc))
            return True
        except OllamaUnavailable as exc:
            self.say("error", str(exc))
            return True
        except OllamaError as exc:
            self.say("error", str(exc))
            return True

        self.show_debug(response.debug)
        self._respond(text, response)
        return True

    def _respond(self, text: str, response: EngineResponse) -> None:
        if response.kind == "refuse":
            self.show_refusal()
        elif response.kind == "capabilities":
            self.show_capabilities()
        elif response.kind == "clarify":
            self.say("info", response.message)
        elif response.kind in ("error", "rephrase"):
            self.say("error" if response.kind == "error" else "warning", response.message)
        elif response.kind == "plan" and response.plan is not None:
            self._run_plan(text, response.plan)

    def _run_plan(self, text: str, plan: PlannedAction) -> None:
        confirmed = False
        if plan.needs_confirmation:
            confirmed = ask_confirmation(plan, self._input, self.console)
            if not confirmed:
                self.say("warning", "Cancelled. Nothing was changed.")
                self.engine.record_cancel(text, plan)
                return
        result = plan.execute(confirmed=confirmed)
        self.show_result(result)
        self.engine.record_result(text, result)
        tip = suggest(result, self.engine.ws)
        if tip:
            self.say("muted", f"Suggestion: {tip}")

    def run(self) -> int:
        self.show_startup()
        while True:
            try:
                line = self._input("you> ")
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break
            try:
                if not self.handle_line(line):
                    break
            except KeyboardInterrupt:
                self.say("warning", "Interrupted.")
            except Exception as exc:  # last-resort guard: never show a raw stack trace
                self.say("error", f"Unexpected error: {exc.__class__.__name__}. Nothing further was changed.")
                if self.config.debug:
                    self.console.print_exception()
        self.say("muted", "Goodbye. (Nothing from this conversation was saved.)")
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="filebot", description="Local file-management chatbot (Ollama).")
    parser.add_argument("--workspace", metavar="PATH", help="workspace folder (default: ./workspace)")
    parser.add_argument("--model", help="Ollama model name (overrides config and FILEBOT_MODEL)")
    parser.add_argument("--config", metavar="FILE", help="path to a filebot.toml config file")
    parser.add_argument("--debug", action="store_true", help="show parsed intent and validation details")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console(theme=THEME, highlight=False)
    try:
        config = load_config(
            Path(args.config) if args.config else None, model=args.model, workspace=args.workspace, debug=args.debug
        )
    except ConfigError as exc:
        console.print(Text(f"Configuration error: {exc}", style="error"))
        return 2
    try:
        workspace = Workspace(config.workspace)
    except PathSecurityError as exc:
        console.print(Text(f"Workspace error: {exc}", style="error"))
        return 2

    client = OllamaClient(config.ollama_host, config.model, config.request_timeout)
    try:
        client.check()
    except OllamaUnavailable as exc:
        console.print(Text(str(exc), style="error"))
        console.print(Text(
            "How to fix:\n"
            "  1. Install Ollama from https://ollama.com/download/windows (if not installed)\n"
            "  2. Start it from the Start menu, or run:  ollama serve\n"
            f"  3. Make sure the model is available:   ollama pull {config.model}\n"
            "  4. Run FileBot again.", style="muted"))
        return 1
    except ModelNotFound as exc:
        console.print(Text(str(exc), style="error"))
        return 1

    engine = Engine(workspace, client, config.max_read_bytes)
    try:
        return ChatUI(config, engine, console).run()
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
