from __future__ import annotations

from types import SimpleNamespace

from agent_application.service import State, run_turn


def test_turn_requires_person_credential_and_lake_url(tmp_path) -> None:
    state = State()
    state.trace_dir = tmp_path

    answer = run_turn({"message": "build a dashboard"}, state)

    assert not answer["ok"]
    assert "lake_token" in answer["error"]


def test_turn_preserves_result_contract_and_writes_trace(tmp_path, monkeypatch) -> None:
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

