from __future__ import annotations

from types import SimpleNamespace

from agent_application.service import State, run_turn


def test_turn_requires_person_credential_and_lake_url(tmp_path) -> None:
    state = State()

    answer = run_turn({"message": "build a dashboard"}, state)

    assert not answer["ok"]
    assert "lake_token" in answer["error"]


def test_turn_preserves_result_contract_and_keeps_nothing_on_disk(tmp_path, monkeypatch) -> None:
    """agent-application#21: the pod serves every tenant, and the message and the answer (the
    generated App source) are workspace content, so a turn writes no trace file, even when the
    operator still sets TRACE_DIR. Lake keeps the answer in the turn's workspace Zetta."""
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    state = State()
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
    assert answer["trace_id"], "the turn is still named, for lake to store with the answer"
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    assert not hasattr(state, "trace_dir")



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
    state = State()
    answer = run_turn({"message": "build a dashboard", "lake_token": "lat.task.secret",
                       "lake_url": "http://lake:8080"}, state)
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


def test_the_lakes_agent_model_reaches_the_agent(monkeypatch) -> None:
    # The operator sets AGENT_MODEL on every library agent pod from the Lake's spec.agents.model.
    monkeypatch.delenv("APPLICATION_AGENT_MODEL", raising=False)
    monkeypatch.setenv("AGENT_MODEL", "gpt-lake-choice")

    assert State().model == "gpt-lake-choice"


def test_application_agent_model_overrides_the_lakes_model(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MODEL", "gpt-lake-choice")
    monkeypatch.setenv("APPLICATION_AGENT_MODEL", "gpt-override")

    assert State().model == "gpt-override"


def test_the_model_defaults_when_neither_is_set(monkeypatch) -> None:
    # An empty value counts as unset.
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("APPLICATION_AGENT_MODEL", "")

    assert State().model == "gpt-6.1-sol"


def test_a_turn_seeds_its_conversation_from_lakes_history_after_a_restart() -> None:
    from agent_application.service import seeded_history

    items = [
        {"task_id": "t1", "message": "Add a chart by region", "reply": "Here is the change.", "outcome": "proposal",
         "diff": "--- a/app.py\n+++ b/app.py\n+chart()", "status": "Succeeded"},
        {"task_id": "t2", "message": "Yes, do it", "reply": "Deployed.", "confirmed": True, "status": "Succeeded"},
        "not a turn",
    ]
    seeded = seeded_history(items)
    assert [m["role"] for m in seeded] == ["user", "assistant", "user", "assistant"]
    assert seeded[0]["content"] == "Add a chart by region"
    assert "Proposed diff:" in seeded[1]["content"] and "+chart()" in seeded[1]["content"]
    assert "confirmed" in seeded[3]["content"]
    assert seeded_history(None) == [] and seeded_history({"x": 1}) == []
