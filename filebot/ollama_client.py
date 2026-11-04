"""Minimal Ollama HTTP client (local only) with structured outputs and validated retries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from .prompts import build_messages, retry_message
from .schemas import INTENT_JSON_SCHEMA, Intent, IntentParseError, parse_intent


class OllamaError(Exception):
    """Base class; messages are safe to show to the user."""


class OllamaUnavailable(OllamaError):
    pass


class ModelNotFound(OllamaError):
    pass


class OllamaTimeout(OllamaError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        self.host = host.rstrip("/")
        self.model = model
        self._http = httpx.Client(base_url=self.host, timeout=timeout, transport=transport)
        self._thinking: bool | None = None

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ low level
    def _post(self, url: str, payload: dict) -> dict:
        try:
            resp = self._http.post(url, json=payload)
        except httpx.ConnectError:
            raise OllamaUnavailable(
                f"Cannot reach Ollama at {self.host}. Start the Ollama app (or run 'ollama serve') and try again."
            ) from None
        except httpx.TimeoutException:
            raise OllamaTimeout("The model took too long to answer. Try again, or use a smaller model.") from None
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Error talking to Ollama: {exc.__class__.__name__}") from None
        if resp.status_code == 404:
            raise ModelNotFound(f"Model '{self.model}' is not installed. Run: ollama pull {self.model}")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            if "not found" in str(detail).lower():
                raise ModelNotFound(f"Model '{self.model}' is not installed. Run: ollama pull {self.model}")
            raise OllamaError(f"Ollama returned an error ({resp.status_code}): {str(detail)[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise OllamaError("Ollama returned a response that is not JSON.") from None

    def check(self) -> None:
        """Verify that Ollama is running and the configured model is installed."""
        try:
            resp = self._http.get("/api/tags")
            resp.raise_for_status()
            models = {m.get("name") for m in resp.json().get("models", [])}
        except httpx.ConnectError:
            raise OllamaUnavailable(
                f"Cannot reach Ollama at {self.host}. Start the Ollama app (or run 'ollama serve') and try again."
            ) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaUnavailable(f"Ollama at {self.host} did not respond correctly ({exc.__class__.__name__}).") from None
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if self.model not in models and wanted not in models:
            raise ModelNotFound(f"Model '{self.model}' is not installed. Run: ollama pull {self.model}")

    def supports_thinking(self) -> bool:
        if self._thinking is None:
            try:
                info = self._post("/api/show", {"model": self.model})
                self._thinking = "thinking" in (info.get("capabilities") or [])
            except OllamaError:
                self._thinking = False
        return self._thinking

    def chat_json(self, messages: list[dict], schema: dict) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "keep_alive": "15m",
            "options": {"temperature": 0, "seed": 7, "num_ctx": 4096, "num_predict": 512},
        }
        if self.supports_thinking():
            payload["think"] = False
        data = self._post("/api/chat", payload)
        message = data.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaError("Ollama returned an unexpected response shape.")
        return content


@dataclass
class Interpretation:
    intent: Intent | None
    raw_responses: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def interpret(client: OllamaClient, user_message: str, context: str) -> Interpretation:
    """Ask the model for an intent. Retries once on invalid output; never raises on bad JSON."""
    result = Interpretation(intent=None)
    messages = build_messages(user_message, context)
    for attempt in range(2):
        raw = client.chat_json(messages, INTENT_JSON_SCHEMA)
        result.raw_responses.append(raw)
        try:
            result.intent = parse_intent(raw)
            return result
        except IntentParseError as exc:
            result.errors.append(str(exc))
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": raw[:2000]},
                    {"role": "user", "content": retry_message(str(exc))},
                ]
    return result


def pretty_json(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return raw
