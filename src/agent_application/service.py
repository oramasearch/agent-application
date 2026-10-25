"""HTTP and bus surfaces for ApplicationAgent."""

from __future__ import annotations

import hmac
import importlib.metadata
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlsplit

from openai import OpenAI

from agent_application import lake_bus
from agent_application.model import gateway_client

PROMPT_VERSION = "v1"
# The release this service is, read from the installed package (pyproject.toml's version).
VERSION = importlib.metadata.version("agent-application")


def lakes_url(configured: str) -> str | None:
    """Lake's address from this service's `LAKE_URL`, the only place a turn's credential is ever
    sent, or None when it is unset or not an absolute http(s) URL without user info, query or
    fragment."""
    url = configured.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return None
    if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
            or "@" in parts.netloc or parts.query or parts.fragment):
        return None
    return url


DEFAULT_MODEL = "gpt-5.5"


def model_from_env() -> str:
    """The model: `APPLICATION_AGENT_MODEL` when set, else the Lake's `AGENT_MODEL`, else the default.

    The operator sets `AGENT_MODEL` on every library agent pod from the Lake's `spec.agents.model`
    (agent-application#22); `APPLICATION_AGENT_MODEL` stays as this agent's own override.
    """
    return (os.environ.get("APPLICATION_AGENT_MODEL")
            or os.environ.get("AGENT_MODEL")
            or DEFAULT_MODEL)


def health(state: State) -> dict[str, Any]:
    """What `GET /healthz` answers."""
    return {
        "ok": True,
        "agent": "agent-application",
        "version": VERSION,
        "model": state.model,
        "prompt_version": PROMPT_VERSION,
    }


class TurnRunner(Protocol):
    def __call__(
        self,
        body: dict[str, Any],
        state: State,
        client: OpenAI | None = None,
        lake: Any = None,
    ) -> dict[str, Any]: ...


class State:
    def __init__(self) -> None:
        self.model = model_from_env()
        self.token = os.environ.get("AGENT_SERVICE_TOKEN", "")
        self.lake_url = os.environ.get("LAKE_URL", "")
        self.lock = threading.Lock()
        self.conversations: dict[str, list[dict[str, Any]]] = {}

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return self.conversations.setdefault(conversation_id, [])


def run_turn(
    body: dict[str, Any],
    state: State,
    client: OpenAI | None = None,
    lake: Any = None,
) -> dict[str, Any]:
    message = str(body.get("message") or "")
    token = str(body.get("lake_token") or "")
    # The turn's credential goes only to Lake at this service's own LAKE_URL; a turn's
    # `lake_url` is ignored, so the bearer cannot be sent to a host the caller names
    # (agent-application#30).
    lake_url = lakes_url(state.lake_url)
    if lake_url is None:
        return {
            "ok": False,
            "error": "LAKE_URL is not set: this agent sends a turn's credential only to "
                     "Lake's configured address",
        }
    if not message or not token:
        return {
            "ok": False,
            "error": "a turn needs a message and the person's lake_token",
        }
    from agent_application.agent import Turn, answer_turn

    conversation_id = str(body.get("conversation_id") or uuid.uuid4())
    turn = Turn(
        message=message,
        lake_url=lake_url,
        lake_token=token,
        customer_id=str(body.get("customer_id") or ""),
        conversation_id=conversation_id,
        context=body.get("context") if isinstance(body.get("context"), dict) else {},
        kind=str(body.get("kind") or "app"),
    )
    # The model is reached through lake with the turn's own key, which counts its tokens in the
    # turn's workspace (lake#248); this agent holds no provider key.
    answer = answer_turn(
        turn,
        state.history(conversation_id),
        client or gateway_client(lake_url, token),
        state.model,
        lake,
    )
    # The trace id names the turn; lake stores it with the answer in the turn's workspace Zetta.
    # Nothing is written to the pod's disk: the pod serves every tenant, and the message and the
    # answer (with the generated App source) are workspace content (agent-application#21).
    trace_id = str(uuid.uuid4())
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "trace_id": trace_id,
        **answer,
    }


def turn_refusal(authorization: str | None, token: str) -> tuple[int, str] | None:
    """Why `POST /v1/turn` is refused, as `(status, error)`, or None when the caller may proceed.

    The route fails closed: with `AGENT_SERVICE_TOKEN` unset or empty every turn is refused, so a
    service started without it is never open. The bearer is compared in constant time
    (`hmac.compare_digest` over bytes), so the comparison's timing says nothing about the token.
    """
    if not token:
        return 503, "AGENT_SERVICE_TOKEN is not set; this service refuses every turn"
    expected = f"Bearer {token}".encode()
    supplied = (authorization or "").encode("utf-8", "surrogateescape")
    if not hmac.compare_digest(supplied, expected):
        return 401, "unauthorized"
    return None


def serve() -> int:
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send(200, health(state))
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/turn":
                self._send(404, {"ok": False, "error": "not found"})
                return
            refusal = turn_refusal(self.headers.get("Authorization"), state.token)
            if refusal is not None:
                status, error = refusal
                self._send(status, {"ok": False, "error": error})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, ValueError):
                self._send(400, {"ok": False, "error": "the body is not JSON"})
                return
            try:
                answer = run_turn(body if isinstance(body, dict) else {}, state)
            except Exception as error:
                answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            self._send(200 if answer.get("ok") else 422, answer)

        def log_message(self, format: str, *args: Any) -> None:
            print(
                json.dumps(
                    {"agent": "agent-application", "request": format % args}
                ),
                flush=True,
            )

    lake_bus.start_from_env(
        lambda body: run_turn(body, state),
        "agent-application",
    )
    port = int(os.environ.get("PORT", "8080"))
    print(json.dumps({"agent": "agent-application", "listening": port}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0
