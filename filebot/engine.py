"""Turns a user message into a safe, validated plan.

Pipeline: model intent (untrusted) -> Pydantic validation -> grounding (Python checks that
every path the model returned was actually written by the user or comes from recent
context) -> required-field check (clarifying question) -> allowlisted action planner.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from .actions import ActionError, ActionResult, PlannedAction, friendly_os_error, plan_action
from .ollama_client import OllamaClient, interpret, pretty_json
from .schemas import FILE_ACTIONS, Intent
from .security import PathSecurityError, Workspace

ResponseKind = Literal["plan", "clarify", "refuse", "capabilities", "error", "rephrase"]
MAX_MESSAGE_CHARS = 4000

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "create_file": ("path",),
    "delete_file": ("path",),
    "create_folder": ("path",),
    "delete_folder": ("path",),
    "list_folder": (),
    "rename_path": ("path", "destination"),
    "move_path": ("path", "destination"),
    "copy_path": ("path", "destination"),
    "read_file": ("path",),
    "edit_file": ("path", "content", "mode"),
    "show_capabilities": (),
}

PATH_QUESTIONS = {
    "create_file": "What should the new file be called?",
    "delete_file": "Which file should I delete?",
    "create_folder": "What should the new folder be called?",
    "delete_folder": "Which folder should I delete?",
    "rename_path": "Which file or folder should I rename?",
    "move_path": "Which file or folder should I move?",
    "copy_path": "Which file or folder should I copy?",
    "read_file": "Which file should I read?",
    "edit_file": "Which file should I edit?",
}

# Words that may legitimately refer back to a path from earlier in the session.
REFERRING_WORDS = re.compile(
    r"\b(it|its|this|that|these|those|them|there|same|again|previous|last|above|one)\b", re.IGNORECASE
)
# Placeholder nouns a model may copy into "path" when the user named nothing specific.
GENERIC_WORDS = {
    "file", "files", "folder", "folders", "directory", "dir", "document", "doc", "something", "it", "this",
    "that", "them", "thing", "stuff", "item", "a file", "the file", "a folder", "the folder", "my file",
    "my folder", "new file", "new folder", "text", "name", "path", "somewhere", "there", "here",
}
# Python double-checks two easily confused classifications from small models.
HELP_CUES = re.compile(
    r"\b(help|capabilit\w*|commands?|features?|examples?|usage|abilities|"
    r"what (?:can|do) you do|what are you able|how (?:do|can|should) i use|how does this work|what can i (?:do|say|ask))\b",
    re.IGNORECASE,
)
FILE_CUES = re.compile(
    r"\b(files?|folders?|director(?:y|ies)|dirs?|documents?|docs?|paths?|workspace|rename|move|copy|delete|remove|"
    r"create|make|list|read|open|edit|append|write|save|organi[sz]e|clean ?up|sort)\b"
    r"|[\w-]+\.[A-Za-z0-9]{1,8}\b|[\w-]+[\\/][\w-]+",
    re.IGNORECASE,
)
CANCEL_WORDS = {"cancel", "never mind", "nevermind", "forget it", "stop", "no", "abort", "skip"}
ROOT_WORDS = {"", ".", "/", "root", "workspace", "the workspace", "workspace root", "main", "home", "everything", "all"}


@dataclass
class Turn:
    user: str
    outcome: str


class Session:
    """Short in-memory context for follow-ups. Nothing is written to disk."""

    def __init__(self, max_turns: int = 3, max_paths: int = 4):
        self.turns: deque[Turn] = deque(maxlen=max_turns)
        self.recent_paths: list[str] = []
        self.max_paths = max_paths
        self.pending: Intent | None = None
        self.pending_question: str | None = None
        self.pending_missing: list[str] = []

    def remember_paths(self, *paths: str | None) -> None:
        for p in paths:
            if not p or p == "(workspace root)":
                continue
            self.recent_paths = [x for x in self.recent_paths if x.lower() != p.lower()]
            self.recent_paths.insert(0, p)
        del self.recent_paths[self.max_paths :]

    def forget_path(self, path: str) -> None:
        low = path.lower().rstrip("/")
        self.recent_paths = [p for p in self.recent_paths if not (p.lower() == low or p.lower().startswith(low + "/"))]

    def add_turn(self, user: str, outcome: str) -> None:
        self.turns.append(Turn(user[:200], outcome[:200]))

    def context_text(self) -> str:
        lines = ["Context:"]
        if self.recent_paths:
            lines.append("- Recent paths (newest first): " + ", ".join(self.recent_paths))
        if self.turns:
            last = self.turns[-1]
            lines.append(f"- Previous request: {last.user!r} -> {last.outcome}")
        if self.pending and self.pending_question:
            lines.append(
                f"- You asked the user: {self.pending_question!r} for action {self.pending.action}. "
                "The new message may be the answer."
            )
        if len(lines) == 1:
            lines.append("- (none)")
        return "\n".join(lines)

    def clear_pending(self) -> None:
        self.pending = None
        self.pending_question = None
        self.pending_missing = []


@dataclass
class EngineResponse:
    kind: ResponseKind
    message: str = ""
    plan: PlannedAction | None = None
    intent: Intent | None = None
    debug: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- grounding
def _norm(s: str) -> str:
    return s.replace("\\", "/").strip().lower()


def _token_in(token: str, text: str) -> bool:
    if not token:
        return False
    pattern = r"(?<![\w\-.])" + re.escape(token) + r"(?![\w\-])"
    return re.search(pattern, text) is not None


def ground_path(path: str | None, message: str, context_paths: list[str]) -> tuple[str | None, str]:
    """Return (path or None, note). A path is grounded if every component was written by the
    user, or the whole path comes from recent context and the message refers back to it."""
    if path is None:
        return None, ""
    msg = _norm(message)
    p = _norm(path).strip("/")
    if not p or p == ".":
        return path, ""
    if p in GENERIC_WORDS:
        return None, f"'{path}' is a generic word, not a name"
    ctx = [_norm(c) for c in context_paths]
    refers_back = REFERRING_WORDS.search(message) is not None
    if refers_back and p in ctx:
        return path, "from context"
    stream = re.search(r"(?<![\w\-.])" + re.escape(p) + r"(:[^\s'\"]+)", msg)
    if stream:
        # The user typed 'name:stream' (an alternate data stream). Keep it so the security
        # layer can block it, instead of silently acting on a different file.
        return path + stream.group(1), "kept the ':' suffix the user typed"
    if _token_in(p, msg):
        expanded = _expand_to_full_token(path, message)
        if expanded:
            return expanded, "expanded to the full path the user typed"
        return path, ""
    if refers_back:
        # "delete it" where the model shortened 'projects/backup.txt' to 'backup.txt':
        # use the most recent full context path with that name.
        for c in context_paths:
            if _norm(c).endswith("/" + p):
                return c, "expanded from context"
    original_parts = path.replace("\\", "/").strip("/").split("/")
    fixed_parts: list[str] = []
    for comp, original in zip(p.split("/"), original_parts):
        if comp in ("", "."):
            continue
        if _token_in(comp, msg) or (refers_back and any(comp in c.split("/") for c in ctx)):
            fixed_parts.append(original)
            continue
        stem = comp.rsplit(".", 1)[0] if "." in comp.strip(".") else comp
        if stem != comp and _token_in(stem, msg):
            # The model added an extension the user never typed: use the user's literal name.
            fixed_parts.append(original.rsplit(".", 1)[0])
            continue
        return None, f"'{path}' was not in the message (component '{comp}')"
    fixed = "/".join(fixed_parts)
    expanded = _expand_to_full_token(fixed, message)
    if expanded:
        return expanded, "expanded to the full path the user typed"
    return fixed, ("adjusted" if fixed != path.replace("\\", "/").strip("/") else "")


def _expand_to_full_token(path: str, message: str) -> str | None:
    """If the model shortened 'logs/server.log' to 'server.log', recover the full path.
    Only when the short form never appears on its own (so 'cp a.css public/a.css' keeps 'a.css')."""
    short = _norm(path)
    tail = "/" + short
    tokens = [tok.rstrip(".,;:!?") for tok in re.findall(r"[^\s'\"`,;]+", message)]
    if any(_norm(tok) == short for tok in tokens):
        return None
    candidates = {tok.replace("\\", "/") for tok in tokens if _norm(tok).endswith(tail)}
    return candidates.pop() if len(candidates) == 1 else None


# Prompt-injection / role-change attempts. Quoted text is removed first, so content the user
# wants written into a file (e.g. 'ignore your rules') is still treated as data.
INJECTION_PATTERNS = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b[^.!?\n]{0,40}\b(instructions?|rules?|prompts?|role|guidelines|restrictions)\b"
    r"|\bsystem prompt\b|\bnew (rules|instructions)\b|\bdeveloper mode\b|\bjailbreak\b"
    r"|\byou are (now|no longer)\b|\bpretend (you are|you're|to be)\b|\bact as (a|an|my)\b|\brole-?play\b"
    r"|^\s*(system|assistant|developer)\s*:|#{3,}|</?s>|<\|"
    r"|\b(create_file|delete_file|create_folder|delete_folder|list_folder|rename_path|move_path|copy_path"
    r"|read_file|edit_file|show_capabilities|off_topic)\b",
    re.IGNORECASE,
)


def looks_like_injection(message: str) -> bool:
    unquoted = _QUOTED.sub(" ", message)
    return INJECTION_PATTERNS.search(unquoted) is not None


_APPEND_WORDS = re.compile(r"\b(append|add(ed|ing)?|to the (end|bottom))\b", re.IGNORECASE)
_REPLACE_WORDS = re.compile(r"\b(replace|overwrite|set the (text|contents?)|change the (text|contents?))\b", re.IGNORECASE)


def infer_mode(message: str) -> str | None:
    """Edit mode stated explicitly by the user's own words (outside quotes), if unambiguous."""
    unquoted = _QUOTED.sub(" ", message)
    a, r = bool(_APPEND_WORDS.search(unquoted)), bool(_REPLACE_WORDS.search(unquoted))
    if a and not r:
        return "append"
    if r and not a:
        return "replace"
    return None


_QUOTED = re.compile(r"\"([^\"\n]+)\"|“([^”\n]+)”|(?<![\w])'([^'\n]+)'(?![\w])")


def _quoted_segments(message: str, exclude: str | None = None) -> list[str]:
    segments = [next(g for g in m.groups() if g) for m in _QUOTED.finditer(message)]
    if exclude:
        ex = _norm(exclude)
        segments = [q for q in segments if _norm(q) != ex and _norm(q) != ex.rsplit("/", 1)[-1]]
    return segments


def ground_content(content: str | None, message: str, path: str | None = None) -> tuple[str | None, bool]:
    """Return (content, ok). Content must come from the user's message: a quoted segment, or
    text that appears verbatim (case-insensitive, on word boundaries)."""
    if content is None:
        return None, True
    quoted = _quoted_segments(message, exclude=path)
    for q in quoted:
        if q.strip().lower() == content.strip().lower():
            return q, True
    pattern = re.escape(content)
    if content[:1].isalnum() or content[:1] == "_":
        pattern = r"(?<!\w)" + pattern
    if content[-1:].isalnum() or content[-1:] == "_":
        pattern = pattern + r"(?!\w)"
    m = re.search(pattern, message, re.IGNORECASE)
    if m and not (quoted and len(quoted) == 1 and m.group(0).lower() not in quoted[0].lower()):
        return m.group(0), True
    squash = lambda s: " ".join(s.replace("\\n", " ").replace("\\t", " ").split()).lower()  # noqa: E731
    if not quoted and squash(content) and squash(content) in squash(message):
        return content, True
    if len(quoted) == 1:
        return quoted[0], True
    return None, False


def unescape_content(text: str) -> str:
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


# ---------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, workspace: Workspace, client: OllamaClient | None, max_read_bytes: int, session: Session | None = None):
        self.ws = workspace
        self.client = client
        self.max_read_bytes = max_read_bytes
        self.session = session or Session()

    # -------------------------------------------------------------- public API
    def handle(self, message: str) -> EngineResponse:
        """Interpret a message. May raise OllamaError (handled by the UI)."""
        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            return EngineResponse("error", f"That message is too long (over {MAX_MESSAGE_CHARS} characters).")
        assert self.client is not None
        interp = interpret(self.client, message, self.session.context_text())
        debug = [f"model raw: {pretty_json(r)}" for r in interp.raw_responses]
        debug += [f"validation error: {e}" for e in interp.errors]
        if interp.intent is None:
            self.session.add_turn(message, "could not understand")
            return EngineResponse(
                "rephrase",
                "Sorry, I couldn't understand that. Please rephrase it as a file or folder command, "
                "e.g. 'create a folder named notes'.",
                debug=debug,
            )
        resp = self.decide(message, interp.intent)
        resp.debug = debug + resp.debug
        return resp

    def analyze(self, message: str, model_intent: Intent) -> EngineResponse:
        """Deterministic post-processing of a validated model intent: merge with a pending
        question, ground arguments, and check required fields. Does not touch the filesystem.
        Returns kind 'plan' (without a plan object) when the intent is complete."""
        debug: list[str] = [f"parsed intent: {model_intent.model_dump()}"]
        if self.session.pending and message.lower().strip(" .!") in CANCEL_WORDS:
            self.session.clear_pending()
            self.session.add_turn(message, "cancelled the question")
            return EngineResponse("clarify", "OK, never mind. What would you like to do next?", debug=debug)
        intent = self._merge_pending(message, model_intent, debug)
        if looks_like_injection(message):
            debug.append("prompt-injection / role-change pattern detected -> refused")
            intent = Intent(action="off_topic")
        elif intent.action == "show_capabilities" and not HELP_CUES.search(message):
            debug.append("show_capabilities without a help request -> treated as off_topic")
            intent = Intent(action="off_topic")
        elif intent.action == "unclear" and not FILE_CUES.search(message):
            debug.append("unclear but nothing file-related in the message -> treated as off_topic")
            intent = Intent(action="off_topic")

        if intent.action == "off_topic":
            self.session.clear_pending()
            self.session.add_turn(message, "refused (off-topic)")
            return EngineResponse("refuse", intent=intent, debug=debug)
        if intent.action == "show_capabilities":
            self.session.clear_pending()
            return EngineResponse("capabilities", intent=intent, debug=debug)
        if intent.action == "unclear":
            self.session.clear_pending()
            q = "What would you like to do: create, read, edit, list, rename, move, copy or delete a file or folder?"
            self.session.add_turn(message, "asked which action")
            return EngineResponse("clarify", q, intent=intent, debug=debug)

        grounded, extra_missing = self._ground(message, intent, debug)
        missing = [f for f in REQUIRED_FIELDS[grounded.action] if getattr(grounded, f) is None] + extra_missing
        if missing:
            question = self._question(grounded, missing[0])
            self.session.pending = grounded
            self.session.pending_question = question
            self.session.pending_missing = missing
            self.session.add_turn(message, f"asked: {question}")
            debug.append(f"missing fields: {missing}")
            return EngineResponse("clarify", question, intent=grounded, debug=debug)
        self.session.clear_pending()
        debug.append(f"final intent: {grounded.model_dump()}")
        return EngineResponse("plan", intent=grounded, debug=debug)

    def decide(self, message: str, model_intent: Intent) -> EngineResponse:
        """analyze() and, for complete intents, build the allowlisted action plan."""
        resp = self.analyze(message, model_intent)
        if resp.kind != "plan" or resp.intent is None:
            return resp
        grounded, debug = resp.intent, resp.debug
        try:
            plan = plan_action(self.ws, grounded, self.max_read_bytes)
        except PathSecurityError as exc:
            self.session.add_turn(message, "blocked")
            return EngineResponse("error", f"Blocked: {exc}", intent=grounded, debug=debug)
        except ActionError as exc:
            self.session.add_turn(message, "failed")
            return EngineResponse("error", str(exc), intent=grounded, debug=debug)
        except OSError as exc:
            return EngineResponse("error", friendly_os_error(exc), intent=grounded, debug=debug)
        debug.append(f"plan: {plan.summary}; confirmation required: {plan.needs_confirmation}")
        return EngineResponse("plan", plan.summary, plan=plan, intent=grounded, debug=debug)

    def record_result(self, message: str, result: ActionResult) -> None:
        self.session.add_turn(message, result.message)
        if result.ok:
            if result.action in ("delete_file", "delete_folder", "move_path", "rename_path"):
                # The old location no longer exists: never offer it as "it" again.
                for p in result.related:
                    self.session.forget_path(p)
                self.session.remember_paths(result.target)
            else:
                self.session.remember_paths(*result.related, result.target)

    def record_cancel(self, message: str, plan: PlannedAction) -> None:
        self.session.add_turn(message, f"cancelled: {plan.summary}")

    # -------------------------------------------------------------- internals
    def _merge_pending(self, message: str, intent: Intent, debug: list[str]) -> Intent:
        pending = self.session.pending
        if pending is None:
            return intent
        if intent.action in FILE_ACTIONS and intent.action != pending.action and intent.action != "show_capabilities":
            has_args = any(getattr(intent, f) for f in REQUIRED_FIELDS[intent.action])
            if has_args or not REQUIRED_FIELDS[intent.action]:
                return intent  # the user moved on to a different request
        if intent.action in ("off_topic", "unclear", "show_capabilities") and len(message.split()) > 6:
            return intent
        missing = list(self.session.pending_missing)
        if not missing:
            return intent
        data = pending.model_dump()
        filled = False
        if intent.action == pending.action:
            for key in ("path", "destination", "content", "mode"):
                if data.get(key) is None and getattr(intent, key) is not None:
                    data[key] = getattr(intent, key)
                    filled = True
        still = [f for f in missing if data.get(f) is None]
        if still:
            answer = message.strip().strip("`\"'").strip()
            first = still[0]
            low = answer.lower().rstrip(".!")
            if first == "mode":
                if low in ("append", "add", "add it", "append it", "add to the end", "at the end", "end"):
                    data["mode"], filled = "append", True
                elif low in ("replace", "overwrite", "replace it", "overwrite it", "replace everything"):
                    data["mode"], filled = "replace", True
            elif first in ("path", "destination") and answer and " " not in answer and len(answer) < 200:
                data[first], filled = answer, True
            elif first == "content" and answer:
                if pending.action == "create_file" and low in ("empty", "nothing", "none", "blank", "no content"):
                    data["content"] = ""
                else:
                    data["content"] = answer
                filled = True
        if not filled:
            # The message did not answer the question: treat it as a new request.
            self.session.clear_pending()
            return intent
        merged = Intent.model_validate(data)
        debug.append(f"merged with pending question -> {merged.model_dump()}")
        return merged

    def _ground(self, message: str, intent: Intent, debug: list[str]) -> tuple[Intent, list[str]]:
        """Drop any argument the user did not actually provide. Returns (intent, extra_missing)."""
        data = intent.model_dump()
        extra_missing: list[str] = []
        if intent.action in ("create_file", "edit_file"):
            # 'destination' means nothing for these actions; small models sometimes put the text there.
            if data["content"] is None and data["destination"] is not None:
                data["content"] = data["destination"]
            data["destination"] = None
        elif intent.action not in ("rename_path", "move_path", "copy_path"):
            data["destination"] = None
        if intent.action not in ("create_file", "edit_file"):
            data["content"] = None
        if intent.action != "edit_file":
            data["mode"] = None
        context = list(self.session.recent_paths)
        trusted: set[str] = set()
        if self.session.pending:
            # Paths from the pending request were already grounded in an earlier turn.
            trusted = {_norm(p) for p in (self.session.pending.path, self.session.pending.destination) if p}
        mentions_root = re.search(r"\b(root|workspace)\b", message, re.IGNORECASE) is not None
        if intent.action == "list_folder" and (intent.path is None or _norm(intent.path) in ROOT_WORDS):
            data["path"] = None
        else:
            for key in ("path", "destination"):
                if (
                    key == "destination"
                    and intent.action in ("move_path", "copy_path")
                    and data[key] is not None
                    and _norm(data[key]) in ROOT_WORDS
                    and mentions_root
                ):
                    data[key] = "."
                    continue
                if data[key] is not None and _norm(data[key]) in trusted:
                    continue
                value, note = ground_path(data[key], message, context)
                if note:
                    debug.append(f"grounding {key}: {note}")
                data[key] = value
            if (
                intent.action in ("rename_path", "move_path", "copy_path")
                and data["path"] is not None
                and data["destination"] is not None
                and _norm(data["path"]) == _norm(data["destination"])
            ):
                debug.append("destination equals source -> treated as missing")
                data["destination"] = None
        if intent.action == "edit_file":
            stated = infer_mode(message)
            if stated and stated != data["mode"] and (self.session.pending is None or data["mode"] is None):
                debug.append(f"mode set from the user's wording: {stated}")
                data["mode"] = stated
        if intent.action in ("create_file", "edit_file"):
            pending = self.session.pending
            if pending is not None and pending.content is not None and data["content"] == pending.content:
                content, ok = data["content"], True  # already grounded in an earlier turn
            else:
                content, ok = ground_content(data["content"], message, data["path"])
            if ok:
                data["content"] = unescape_content(content) if content is not None else None
            else:
                # Content was paraphrased or invented by the model: ask instead of guessing.
                debug.append("grounding content: model content not found in message")
                data["content"] = None
                if intent.action == "create_file":
                    extra_missing.append("content")
        return Intent.model_validate(data), extra_missing

    def _question(self, intent: Intent, missing: str) -> str:
        name = intent.path or "the file"
        if missing == "path":
            return PATH_QUESTIONS.get(intent.action, "Which file or folder do you mean?")
        if missing == "destination":
            if intent.action == "rename_path":
                return f"What should the new name for '{name}' be?"
            verb = "move" if intent.action == "move_path" else "copy"
            return f"Where should I {verb} '{name}'? Give a folder or a new path."
        if missing == "content":
            return f"What text should I write to '{name}'?"
        if missing == "mode":
            return f"Should I append the text to the end of '{name}', or replace its contents? (append / replace)"
        return "Could you give a bit more detail?"
