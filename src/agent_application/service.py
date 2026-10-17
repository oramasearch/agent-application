"""HTTP and bus surfaces for ApplicationAgent."""

from __future__ import annotations

import importlib.metadata
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI

from agent_application import lake_bus
from agent_application.model import gateway_client

PROMPT_VERSION = "v1"
# The release this service is, read from the installed package (pyproject.toml's version).
VERSION = importlib.metadata.version("agent-application")


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
        self.model = os.environ.get("APPLICATION_AGENT_MODEL", "gpt-5.5")
        self.token = os.environ.get("AGENT_SERVICE_TOKEN", "")
        self.lake_url = os.environ.get("LAKE_URL", "")
        self.trace_dir = Path(
            os.environ.get("TRACE_DIR", "/tmp/agent-application-traces")
        )
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
    lake_url = str(body.get("lake_url") or state.lake_url)
    if not message or not token or not lake_url:
        return {
            "ok": False,
            "error": "a turn needs a message, the person's lake_token and lake URL",
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
    trace_id = str(uuid.uuid4())
    state.trace_dir.mkdir(parents=True, exist_ok=True)
    trace = {
        "trace_id": trace_id,
        "agent": "agent-application",
        "prompt_version": PROMPT_VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
        "conversation_id": conversation_id,
        "message": message,
        "answer": answer,
    }
    (state.trace_dir / f"{trace_id}.json").write_text(
        json.dumps(trace, indent=1, default=str)
    )
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "trace_id": trace_id,
        **answer,
    }


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
            authorization = self.headers.get("Authorization")
            if state.token and authorization != f"Bearer {state.token}":
                self._send(401, {"ok": False, "error": "unauthorized"})
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
