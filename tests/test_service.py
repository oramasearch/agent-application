from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_application.service import State, run_turn


def test_turn_requires_person_credential_and_lake_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAKE_URL", "https://lake.test")
    state = State()
    state.trace_dir = tmp_path

    answer = run_turn({"message": "build a dashboard"}, state)

    assert not answer["ok"]
    assert "lake_token" in answer["error"]


def test_turn_preserves_result_contract_and_writes_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAKE_URL", "https://lake.test")
    state = State()
    state.trace_dir = tmp_path
    expected = {
        "reply": "Ready to review.",
        "result": {
            "kind": "proposal",
            "files": [],
            "app": {},
            "confirm": True,
        },
    }

    def answer_turn(*_args, **_kwargs):
        return expected

    module = SimpleNamespace(Turn=lambda **values: values, answer_turn=answer_turn)
    monkeypatch.setitem(__import__("sys").modules, "agent_application.agent", module)

    answer = run_turn(
        {
            "message": "build a dashboard",
            "lake_token": "person-token",
            "lake_url": "https://lake.test",
        },
        state,
        client=SimpleNamespace(),
    )

    assert answer["ok"]
    assert answer["result"] == expected["result"]
    assert len(list(tmp_path.glob("*.json"))) == 1



def test_turn_calls_its_model_through_lakes_gateway_with_the_turns_key(tmp_path, monkeypatch) -> None:
    """lake#248: the model is lake's `/openai/v1`, with the turn's own key, never a provider key."""
    import httpx

    from agent_application import model, service

    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5.5",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Ready."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})

    def answer_turn(turn, history, client, model_name, lake):
        completion = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": turn["message"]}])
        return {"reply": completion.choices[0].message.content, "result": {}}

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "agent_application.agent",
                        SimpleNamespace(Turn=lambda **values: values, answer_turn=answer_turn))
    monkeypatch.setattr(service, "gateway_client", lambda url, token: model.gateway_client(
        url, token, http_client=httpx.Client(transport=httpx.MockTransport(gateway))))
    monkeypatch.setenv("LAKE_URL", "http://lake:8080")
    state = State()
    state.trace_dir = tmp_path
    # The turn's own lake_url is never used: the key goes to this service's LAKE_URL (#30).
    answer = run_turn({"message": "build a dashboard", "lake_token": "lat.task.secret",
                       "lake_url": "http://elsewhere:9"}, state)
    assert answer["ok"] and answer["reply"] == "Ready."
    assert [str(request.url) for request in seen] == ["http://lake:8080/openai/v1/chat/completions"]
    assert seen[0].headers["authorization"] == "Bearer lat.task.secret"


def test_healthz_reports_the_release_version() -> None:
    import importlib.metadata

    from agent_application.service import PROMPT_VERSION, health

    body = health(State())

    assert body["ok"]
    assert body["version"] == importlib.metadata.version("agent-application")
    assert body["prompt_version"] == PROMPT_VERSION


def _record_turns(monkeypatch) -> list[dict]:
    """Replace the agent with one that records the Turn it is given and makes no request."""
    turns: list[dict] = []

    def answer_turn(turn, *_args, **_kwargs):
        turns.append(turn)
        return {"reply": "ok", "result": {}}

    monkeypatch.setitem(__import__("sys").modules, "agent_application.agent",
                        SimpleNamespace(Turn=lambda **values: values, answer_turn=answer_turn))
    return turns


def test_a_turns_lake_url_is_ignored_and_lake_is_reached_at_lake_url(tmp_path, monkeypatch) -> None:
    """agent-application#30: Lake capabilities and the model go to LAKE_URL, never to the turn's."""
    from agent_application import service

    turns = _record_turns(monkeypatch)
    gateways: list[str] = []
    monkeypatch.setattr(service, "gateway_client",
                        lambda url, token: gateways.append(url) or SimpleNamespace())
    monkeypatch.setenv("LAKE_URL", "http://lake-prod.lake.svc.cluster.local:80/")
    state = State()
    state.trace_dir = tmp_path
    answer = run_turn({"message": "build", "lake_token": "lat.task.secret",
                       "lake_url": "http://attacker.example"}, state)
    assert answer["ok"], answer
    assert [turn["lake_url"] for turn in turns] == ["http://lake-prod.lake.svc.cluster.local:80"]
    assert gateways == ["http://lake-prod.lake.svc.cluster.local:80"]


@pytest.mark.parametrize("configured", [
    "",
    "   ",
    "lake-prod.lake.svc.cluster.local",
    "file:///etc/passwd",
    "http://user@lake.test",
    "http://lake.test:notaport",
    "http://lake.test/?next=http://attacker.example",
])
def test_without_a_valid_lake_url_no_turn_sends_its_credential(
    tmp_path, monkeypatch, configured
) -> None:
    from agent_application import service

    turns = _record_turns(monkeypatch)
    monkeypatch.setattr(service, "gateway_client",
                        lambda url, token: pytest.fail(f"model called at {url}"))
    monkeypatch.setenv("LAKE_URL", configured)
    state = State()
    state.trace_dir = tmp_path
    answer = run_turn({"message": "build", "lake_token": "lat.task.secret",
                       "lake_url": "https://lake.test"}, state)
    assert not answer["ok"]
    assert "LAKE_URL is not set" in answer["error"]
    assert turns == []
