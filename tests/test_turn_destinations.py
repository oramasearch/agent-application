"""A turn's credential goes only to Lake at LAKE_URL, never to an address the turn names (#30)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import httpx
import pytest

from agent_application import model, service
from agent_application.service import State, run_turn


@pytest.fixture
def turns(monkeypatch) -> list[dict]:
    """Replace the agent with one that records the Turn it is given and makes no request."""
    seen: list[dict] = []

    def answer_turn(turn, *_args, **_kwargs):
        seen.append(turn)
        return {"reply": "ok", "result": {}}

    monkeypatch.setitem(sys.modules, "agent_application.agent",
                        SimpleNamespace(Turn=lambda **values: values, answer_turn=answer_turn))
    return seen


def _state(tmp_path) -> State:
    state = State()
    state.trace_dir = tmp_path
    return state


def test_a_turns_lake_url_is_ignored_and_lake_is_reached_at_lake_url(
    tmp_path, monkeypatch, turns
) -> None:
    gateways: list[str] = []
    monkeypatch.setattr(service, "gateway_client",
                        lambda url, token, **kw: gateways.append(url) or SimpleNamespace())
    monkeypatch.setenv("LAKE_URL", "http://lake-prod.lake.svc.cluster.local:80/")
    answer = run_turn({"message": "build", "lake_token": "lat.task.secret",
                       "lake_url": "http://attacker.example"}, _state(tmp_path))
    assert answer["ok"], answer
    # Turn.lake_url is the base agent.Lake sends every capability call and the bearer to.
    assert [turn["lake_url"] for turn in turns] == ["http://lake-prod.lake.svc.cluster.local:80"]
    assert gateways == ["http://lake-prod.lake.svc.cluster.local:80"]


def test_the_model_is_called_at_lake_url_not_the_turns_lake_url(tmp_path, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5.5",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Ready."}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def answer_turn(turn, history, client, model_name, lake):
        client.chat.completions.create(model=model_name, messages=[{"role": "user",
                                                                    "content": "x"}])
        return {"reply": "Ready.", "result": {}}

    monkeypatch.setitem(sys.modules, "agent_application.agent",
                        SimpleNamespace(Turn=lambda **values: values, answer_turn=answer_turn))
    monkeypatch.setattr(service, "gateway_client", lambda url, token, **kw: model.gateway_client(
        url, token, http_client=httpx.Client(transport=httpx.MockTransport(gateway)), **kw))
    monkeypatch.setenv("LAKE_URL", "http://lake:8080")
    answer = run_turn({"message": "build", "lake_token": "lat.task.secret",
                       "lake_url": "http://elsewhere:9"}, _state(tmp_path))
    assert answer["ok"], answer
    assert [str(request.url) for request in seen] == ["http://lake:8080/openai/v1/chat/completions"]


@pytest.mark.parametrize("configured", [
    None,
    "",
    "   ",
    "lake-prod.lake.svc.cluster.local",
    "file:///etc/passwd",
    "http://user@lake.test",
    "http://lake.test:notaport",
    "http://lake.test/?next=http://attacker.example",
])
def test_without_a_valid_lake_url_no_turn_sends_its_credential(
    tmp_path, monkeypatch, turns, configured
) -> None:
    monkeypatch.setattr(service, "gateway_client",
                        lambda url, token, **kw: pytest.fail(f"model called at {url}"))
    if configured is None:
        monkeypatch.delenv("LAKE_URL", raising=False)
    else:
        monkeypatch.setenv("LAKE_URL", configured)
    answer = run_turn({"message": "build", "lake_token": "lat.task.secret",
                       "lake_url": "https://lake.test"}, _state(tmp_path))
    assert not answer["ok"]
    assert "LAKE_URL is not set" in answer["error"]
    assert turns == []
