from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from agent_application.agent import Turn, answer_turn, proposal_defects


def files(
    framework: str = "fastapi",
    dependencies: list[str] | None = None,
    zetta: bool = False,
    services: list[str] | None = None,
) -> list[dict[str, str]]:
    dependencies = ["fastapi"] if dependencies is None else dependencies
    services = services or []
    entrypoint = "main:app" if framework == "fastapi" else "site"
    manifest = (
        'name = "test-app"\n'
        f'framework = "{framework}"\n'
        f'entrypoint = "{entrypoint}"\n'
        "port = 8080\n"
        f"dependencies = {json.dumps(dependencies)}\n\n"
        "[bindings]\n"
        f"zetta = {str(zetta).lower()}\n"
        f"services = {json.dumps(services)}\n"
    )
    source = "from fastapi import FastAPI\n\napp = FastAPI()\n"
    if framework == "static":
        source = "<!doctype html><title>Test App</title><h1>Ready</h1>\n"
    source_path = "main.py" if framework == "fastapi" else "site/index.html"
    return [
        {"path": "app.toml", "contents": manifest},
        {"path": source_path, "contents": source},
        {"path": "README.md", "contents": "# Test App\n"},
    ]


def proposal(**changes):
    app = {
        "name": "test-app",
        "mode": "runtime",
        "repository": {"name": "app-test"},
        "path": "apps/test",
        "framework": "fastapi",
        "port": 8080,
        "auth": "lake",
        "bindings": {"zetta": False, "services": []},
        "env": {},
    }
    app.update(changes.pop("app", {}))
    value = {
        "kind": "proposal",
        "reply": "Review the App.",
        "files": files(),
        "app": app,
    }
    value.update(changes)
    return value


class Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class Scripted:
    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_arguments):
        answer = self.answers.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=Message(json.dumps(answer)))])


class FakeLake:
    def __init__(self) -> None:
        self.reads: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, dict]] = []

    def read(self, capability, arguments):
        self.reads.append((capability, arguments))
        if capability == "receive_app_events":
            return {
                "events": [
                    {
                        "type": "ready",
                        "status": "ready",
                        "url": "https://test.apps.example",
                    }
                ],
                "next_sequence": 2,
            }
        return {}

    def write(self, capability, arguments):
        self.writes.append((capability, arguments))
        answers = {
            "create_repository": {"id": "repo-1"},
            "commit_repository_files": {"commitOid": "blake3:revision"},
            "create_app": {"id": "app-1"},
            "build_app": {"build_id": "build-1"},
            "deploy_app": {"id": "app-1", "status": "deploying"},
        }
        return answers[capability]


def turn(message="build an App", context=None):
    return Turn(
        message=message,
        lake_url="https://lake.test",
        lake_token="person-token",
        customer_id="customer-1",
        context=context or {},
    )


def test_initial_turn_returns_proposal_without_writes() -> None:
    lake = FakeLake()

    answer = answer_turn(turn(), [], Scripted(proposal()), "test-model", lake)

    assert answer["result"]["kind"] == "proposal"
    assert answer["result"]["confirm"] is True
    assert lake.writes == []


def test_plain_language_confirmation_is_not_authority() -> None:
    lake = FakeLake()

    answer = answer_turn(
        turn("yes, deploy it"),
        [],
        Scripted({"kind": "question", "reply": "Please confirm the reviewed proposal."}),
        "test-model",
        lake,
    )

    assert answer["result"]["kind"] == "question"
    assert lake.writes == []


def test_confirmed_proposal_commits_then_creates_and_deploys() -> None:
    lake = FakeLake()
    reviewed = proposal()
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    answer = answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert answer["result"]["kind"] == "deployed"
    assert answer["result"]["confirm"] is False
    assert "https://test.apps.example" in answer["reply"]
    assert [name for name, _ in lake.writes] == [
        "create_repository",
        "commit_repository_files",
        "create_app",
        "deploy_app",
    ]
    create_repository = lake.writes[0][1]
    assert create_repository["visibility"] == "private"
    operations = lake.writes[1][1]["operations"]
    assert operations[0]["path"] == "apps/test/app.toml"
    assert base64.b64decode(operations[0]["content"]).decode().startswith("name =")


def test_image_mode_uses_lake_build_capability() -> None:
    lake = FakeLake()
    reviewed = proposal(app={"mode": "image"})
    reviewed["files"] = files(dependencies=["scikit-learn"])
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert [name for name, _ in lake.writes][-2:] == ["build_app", "deploy_app"]


def test_existing_app_gets_a_new_revision_without_create() -> None:
    lake = FakeLake()
    reviewed = proposal(
        app={
            "id": "existing-app",
            "repository": {"id": "existing-repo"},
        }
    )
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert [name for name, _ in lake.writes] == [
        "commit_repository_files",
        "deploy_app",
    ]


def test_invalid_model_answer_gets_repaired() -> None:
    lake = FakeLake()
    unsafe = proposal()
    unsafe["files"][1]["contents"] += 'TOKEN = "sk-abcdefghijklmnopqrstu"\n'

    answer = answer_turn(
        turn(),
        [],
        Scripted(unsafe, proposal()),
        "test-model",
        lake,
    )

    assert answer["result"]["kind"] == "proposal"
    assert lake.writes == []


def test_safety_and_mode_rules_are_enforced() -> None:
    public = proposal(app={"auth": "public"})
    custom = proposal()
    custom["files"] = files(dependencies=["scikit-learn"])
    secret = proposal(app={"env": {"API_TOKEN": "plain-value"}})

    assert any("explicitly" in defect for defect in proposal_defects(public, "build it"))
    assert proposal_defects(public, "make it public") == []
    assert any("image mode" in defect for defect in proposal_defects(custom, "build it"))
    assert any("secret" in defect for defect in proposal_defects(secret, "build it"))

