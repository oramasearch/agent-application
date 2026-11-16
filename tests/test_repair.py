# SPDX-License-Identifier: MIT

"""Repair and change turns on an existing App (lake#1207)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from agent_application.agent import Turn, answer_turn
from agent_application.service import State, run_turn

APP_ID = "app-1"
MANIFEST = (
    'name = "reports"\nframework = "fastapi"\nentrypoint = "main:app"\nport = 8080\n'
    'dependencies = ["fastapi"]\n'
)
BROKEN = "from fastapi import FastAPI\n\napp = FastAPI()\nPORT = 3000\n"
FIXED = "from fastapi import FastAPI\n\napp = FastAPI()\nPORT = 8080\n"


class Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class Scripted:
    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.requests: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **arguments):
        self.requests.append(list(arguments["messages"]))
        answer = self.answers.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=Message(json.dumps(answer)))])


class AppLake:
    """The facts of one failed App, and lake's answers to the writes a repair makes."""

    def __init__(self, logs_fail: bool = False, rollout: str = "ready") -> None:
        self.reads: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, dict]] = []
        self.logs_fail = logs_fail
        self.rollout = rollout

    def read(self, capability, arguments):
        self.reads.append((capability, arguments))
        if capability == "app_logs":
            if self.logs_fail:
                from agent_application.agent import LakeError

                raise LakeError("app_logs", "store_unavailable", "app logs are not configured")
            return {"items": [{"line": "Uvicorn running on http://0.0.0.0:3000"}]}
        if capability == "receive_app_events":
            if arguments.get("wait_ms") == 0:
                return {"events": [{"type": "rollout", "status": {"phase": "Degraded"}}]}
            return {"events": [{"status": self.rollout}], "next_sequence": 3}
        if capability == "repository_tree":
            return {
                "items": [
                    {"path": "apps/reports/app.toml", "kind": "file", "size": 100},
                    {"path": "apps/reports/main.py", "kind": "file", "size": 100},
                    {"path": "other/secret.py", "kind": "file", "size": 10},
                ]
            }
        if capability == "repository_file":
            contents = {
                "apps/reports/app.toml": MANIFEST,
                "apps/reports/main.py": BROKEN,
            }.get(arguments["path"])
            return {"contents": contents} if contents is not None else {}
        if capability == "get_app":
            return {"id": APP_ID, "status": self.rollout}
        return {}

    def write(self, capability, arguments):
        self.writes.append((capability, arguments))
        return {
            "commit_repository_files": {"commitOid": "blake3:fixed"},
            "deploy_app": {"id": APP_ID, "generation": 3},
            "build_app": {"build_id": "build-2"},
            "set_app_env": {},
        }[capability]


def app_context(**extra) -> dict:
    return {
        "app_id": APP_ID,
        "app": {
            "id": APP_ID,
            "name": "reports",
            "mode": "runtime",
            "framework": "fastapi",
            "port": 8080,
            "auth": "lake",
            "phase": "Degraded",
            "message": "CrashLoopBackOff",
            "source": {"repository": "repo-1", "revision": "blake3:old", "path": "apps/reports"},
            "env": {},
        },
        "repair": {"deployed_by": "user-1", "source": {}},
        **extra,
    }


def turn(kind: str = "repair", message: str = "Fix the app reports.", **context) -> Turn:
    return Turn(
        message=message,
        lake_url="http://lake.test",
        lake_token="lake-token-123456",
        customer_id="customer-1",
        conversation_id="conversation-1",
        context=app_context(**context),
        kind=kind,
    )


FIX = {
    "kind": "proposal",
    "reply": "The app listens on port 3000 but Lake sends traffic to 8080.",
    "files": [{"path": "main.py", "contents": FIXED}],
}


def test_a_repair_proposes_a_diff_and_writes_nothing() -> None:
    lake = AppLake()
    model = Scripted(FIX)
    answer = answer_turn(turn(), [], model, "test-model", lake)
    result = answer["result"]
    assert result["kind"] == "proposal" and result["confirm"] is True
    assert result["turn"] == "repair"
    assert result["app"]["id"] == APP_ID
    assert result["files"] == [{"path": "main.py", "contents": FIXED}]
    assert "-PORT = 3000" in result["diff"] and "+PORT = 8080" in result["diff"]
    assert "a/main.py" in result["diff"]
    assert lake.writes == []
    facts = model.requests[0][1]["content"]
    assert "Uvicorn running on http://0.0.0.0:3000" in facts
    assert "File main.py" in facts and "secret.py" not in facts
    assert {name for name, _ in lake.reads} >= {"receive_app_events", "app_logs", "list_builds"}


def test_missing_logs_are_said_and_the_repair_works_from_events_and_source() -> None:
    lake = AppLake(logs_fail=True)
    model = Scripted(FIX)
    answer = answer_turn(turn(), [], model, "test-model", lake)
    assert answer["result"]["kind"] == "proposal"
    assert "Runtime logs are unavailable here" in model.requests[0][1]["content"]


def test_a_confirmed_repair_commits_one_drip_redeploys_and_follows_the_rollout() -> None:
    proposal = answer_turn(turn(), [], Scripted(FIX), "test-model", AppLake())["result"]
    lake = AppLake()
    answer = answer_turn(turn(confirmed_proposal=proposal), [], Scripted(), "test-model", lake)
    assert [name for name, _ in lake.writes] == ["commit_repository_files", "deploy_app"]
    commit = lake.writes[0][1]
    assert commit["id"] == "repo-1" and commit["revision"] == "main"
    assert commit["message"].startswith("Repair the reports App: The app listens on port 3000")
    assert commit["operations"] == [
        {"type": "file", "path": "apps/reports/main.py", "content": base64.b64encode(FIXED.encode()).decode()}
    ]
    deploy = lake.writes[1][1]
    assert deploy["app"] == APP_ID
    assert deploy["source"] == {"repository": "repo-1", "revision": "blake3:fixed", "path": "apps/reports"}
    assert answer["result"]["kind"] == "deployed"
    assert answer["result"]["revision"] == "blake3:fixed"
    assert "running again" in answer["reply"]


def test_a_confirmed_change_for_another_app_is_refused() -> None:
    proposal = answer_turn(turn(), [], Scripted(FIX), "test-model", AppLake())["result"]
    proposal["app"]["id"] = "app-2"
    lake = AppLake()
    answer = answer_turn(turn(confirmed_proposal=proposal), [], Scripted(), "test-model", lake)
    assert answer["result"]["kind"] == "question"
    assert lake.writes == []


def test_a_new_dependency_outside_the_baked_set_rebuilds_before_deploying(monkeypatch) -> None:
    import agent_application.repair as repair

    monkeypatch.setattr(
        repair,
        "wait_for_build",
        lambda customer, build, lake: {"phase": "Succeeded", "image": "r/reports@sha256:" + "a" * 64},
    )
    manifest = MANIFEST.replace('["fastapi"]', '["fastapi", "scikit-learn"]')
    change = {
        "kind": "proposal",
        "reply": "Use scikit-learn for the forecast.",
        "files": [{"path": "app.toml", "contents": manifest}],
    }
    proposal = answer_turn(turn("change", "Add a forecast."), [], Scripted(change), "m", AppLake())["result"]
    lake = AppLake()
    answer_turn(turn("change", "Apply.", confirmed_proposal=proposal), [], Scripted(), "m", lake)
    names = [name for name, _ in lake.writes]
    assert names == ["commit_repository_files", "set_app_env", "build_app", "deploy_app"]
    assert lake.writes[1][1]["value"] == "scikit-learn"
    assert lake.writes[3][1]["image"].startswith("r/reports@sha256:")


def test_an_auto_fix_turn_applies_its_own_fix_and_says_which_attempt() -> None:
    lake = AppLake()
    answer = answer_turn(
        turn(auto_fix={"attempt": 2, "max_attempts": 3}), [], Scripted(FIX), "m", lake
    )
    assert [name for name, _ in lake.writes] == ["commit_repository_files", "deploy_app"]
    assert lake.writes[0][1]["message"].startswith("Auto-fix the reports App")
    assert answer["reply"].startswith("Auto-fix attempt 2 of 3")
    assert answer["result"]["auto_fix"] == {"attempt": 2, "max_attempts": 3}


def test_an_auto_fix_that_finds_nothing_writes_nothing() -> None:
    lake = AppLake()
    answer = answer_turn(
        turn(auto_fix={"attempt": 1, "max_attempts": 3}),
        [],
        Scripted({"kind": "question", "reply": "Which database should it use?"}),
        "m",
        lake,
    )
    assert lake.writes == []
    assert answer["reply"].startswith("Auto-fix could not find a fix")


def test_a_change_that_leaves_every_file_as_it_is_is_asked_again() -> None:
    same = {"kind": "proposal", "reply": "No change.", "files": [{"path": "main.py", "contents": BROKEN}]}
    model = Scripted(same, FIX)
    answer = answer_turn(turn("change", "Fix the port."), [], model, "m", AppLake())
    assert answer["result"]["kind"] == "proposal"
    assert "leaves every file as it is" in model.requests[1][-1]["content"]


def test_a_change_cannot_rename_the_app_or_carry_a_secret() -> None:
    renamed = {
        "kind": "proposal",
        "reply": "Renamed.",
        "files": [
            {"path": "app.toml", "contents": MANIFEST.replace('"reports"', '"other"')},
            {"path": "main.py", "contents": FIXED + 'API_KEY = "sk-abcdefghijklmnopqrstu"\n'},
        ],
    }
    model = Scripted(renamed, renamed, renamed)
    answer = answer_turn(turn("change", "Rename it."), [], model, "m", AppLake())
    assert answer["result"]["kind"] == "question"
    assert "app.toml name must stay" in answer["reply"]
    assert "looks like a secret" in answer["reply"]


def test_the_service_passes_the_turn_kind() -> None:
    state = State()
    state.lake_url = "http://lake.test"
    lake = AppLake()
    answer = run_turn(
        {
            "message": "Add a chart by region.",
            "lake_token": "lake-token-123456",
            "customer_id": "customer-1",
            "kind": "change",
            "context": app_context(),
        },
        state,
        client=Scripted(FIX),
        lake=lake,
    )
    assert answer["ok"] is True
    assert answer["result"]["turn"] == "change"
