# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from agent_application.agent import Lake, LakeError, Turn, answer_turn, proposal_defects


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
        if capability == "receive_build_events":
            return {
                "events": [{"type": "done", "phase": "done"}],
                "next_sequence": 2,
            }
        if capability == "get_build":
            return {
                "id": "build-1",
                "phase": "Succeeded",
                "target": "registry.test/customer/test-app:build-1",
                "digest": "sha256:abc123",
            }
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


class FailedBuildLake(FakeLake):
    def read(self, capability, arguments):
        self.reads.append((capability, arguments))
        if capability == "receive_build_events":
            return {
                "events": [{"type": "error", "message": "pip install failed"}],
                "next_sequence": 3,
            }
        if capability == "get_build":
            return {
                "id": "build-1",
                "phase": "Failed",
                "message": "builder exited with status 1",
            }
        if capability == "build_logs":
            return {"lines": ["Collecting package", "No matching distribution found"]}
        return super().read(capability, arguments)


class RetryLake(FakeLake):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.failed = False
        self.repositories: list[dict] = []
        self.apps: list[dict] = []
        self.commits = 0

    def read(self, capability, arguments):
        self.reads.append((capability, arguments))
        if capability == "list_repositories":
            return {"items": list(self.repositories)}
        if capability == "list_apps":
            return {"apps": list(self.apps)}
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
        if capability == self.failure and not self.failed:
            self.failed = True
            raise LakeError(capability, "test_failure", "injected deployment failure")
        if capability == "create_repository":
            repository = {
                "id": "repo-1",
                "customer_id": arguments["customer_id"],
                "name": arguments["name"],
            }
            self.repositories.append(repository)
            return repository
        if capability == "commit_repository_files":
            self.commits += 1
            return {"commitOid": f"blake3:revision-{self.commits}"}
        if capability == "create_app":
            app = {
                "id": "app-1",
                "customer_id": arguments["customer_id"],
                "name": arguments["name"],
            }
            self.apps.append(app)
            return app
        if capability == "deploy_app":
            return {"id": arguments["app"], "status": "deploying"}
        raise AssertionError(f"unexpected capability {capability}")


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


def test_retry_after_repository_creation_reuses_repository() -> None:
    lake = RetryLake("create_app")
    reviewed = proposal()
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    with pytest.raises(LakeError, match="create_app: test_failure"):
        answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    answer = answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert answer["result"]["kind"] == "deployed"
    assert [name for name, _ in lake.writes] == [
        "create_repository",
        "commit_repository_files",
        "create_app",
        "commit_repository_files",
        "create_app",
        "deploy_app",
    ]
    assert answer["result"]["app"]["repository"]["id"] == "repo-1"


def test_retry_after_app_creation_reuses_app_and_deploys_new_revision() -> None:
    lake = RetryLake("deploy_app")
    reviewed = proposal()
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    with pytest.raises(LakeError, match="deploy_app: test_failure"):
        answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    answer = answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert answer["result"]["kind"] == "deployed"
    assert [name for name, _ in lake.writes] == [
        "create_repository",
        "commit_repository_files",
        "create_app",
        "deploy_app",
        "commit_repository_files",
        "deploy_app",
    ]
    assert lake.writes[-1][1]["app"] == "app-1"
    assert lake.writes[-1][1]["source"]["revision"] == "blake3:revision-2"


def test_image_mode_uses_lake_build_capability() -> None:
    lake = FakeLake()
    reviewed = proposal(app={"mode": "image"})
    reviewed["files"] = files(dependencies=["fastapi", "psycopg[binary]>=3.2"])
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    answer = answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert answer["result"]["kind"] == "deployed"
    assert [name for name, _ in lake.writes] == [
        "create_repository",
        "commit_repository_files",
        "create_app",
        "build_app",
        "deploy_app",
    ]
    create_app = lake.writes[2][1]
    assert create_app["mode"] == "runtime"
    build_app = lake.writes[3][1]
    assert build_app["source"] == create_app["source"]
    assert create_app["source"]["revision"] == "blake3:revision"
    assert create_app["env"]["APP_DEPS"] == "psycopg[binary]>=3.2"
    assert (
        "receive_build_events",
        {
            "customer_id": "customer-1",
            "build_id": "build-1",
            "from_sequence": 1,
            "wait_ms": 20_000,
        },
    ) in lake.reads
    assert (
        "get_build",
        {
            "customer_id": "customer-1",
            "build_id": "build-1",
        },
    ) in lake.reads
    deploy_app = lake.writes[4][1]
    assert deploy_app["image"] == (
        "registry.test/customer/test-app:build-1@sha256:abc123"
    )
    assert "source" not in deploy_app


def test_failed_image_build_returns_log_tail_without_deploying() -> None:
    lake = FailedBuildLake()
    reviewed = proposal(app={"mode": "image"})
    reviewed["files"] = files(dependencies=["fastapi", "psycopg[binary]"])
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    answer = answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert answer["result"]["kind"] == "question"
    assert answer["result"]["confirm"] is False
    assert answer["result"]["app"]["build"]["id"] == "build-1"
    assert "Build build-1 failed" in answer["reply"]
    assert "No matching distribution found" in answer["reply"]
    assert [name for name, _ in lake.writes] == [
        "create_repository",
        "commit_repository_files",
        "create_app",
        "build_app",
    ]


def test_baked_only_image_proposal_skips_build() -> None:
    lake = FakeLake()
    reviewed = proposal(app={"mode": "image", "env": {"APP_DEPS": "unsafe"}})
    context = {
        "confirmed_proposal": {
            "files": reviewed["files"],
            "app": reviewed["app"],
        }
    }

    answer_turn(turn("confirmed", context), [], Scripted(), "test-model", lake)

    assert [name for name, _ in lake.writes] == [
        "create_repository",
        "commit_repository_files",
        "create_app",
        "deploy_app",
    ]
    create_app = lake.writes[2][1]
    assert create_app["mode"] == "runtime"
    assert "APP_DEPS" not in create_app["env"]
    assert "source" in lake.writes[3][1]


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
    secret_reference = proposal(
        app={"env": {"API_TOKEN": {"secret": "lake-secret://mnt-1/forecast/api#token"}}}
    )
    dependency_credential = proposal(app={"mode": "image"})
    dependency_credential["files"] = files(
        dependencies=["package @ https://person:credential@example.test/package.whl"]
    )

    assert any("explicitly" in defect for defect in proposal_defects(public, "build it"))
    assert proposal_defects(public, "make it public") == []
    assert any("image mode" in defect for defect in proposal_defects(custom, "build it"))
    assert any("secret" in defect for defect in proposal_defects(secret, "build it"))
    assert proposal_defects(secret_reference, "build it") == []
    assert any(
        "dependencies cannot contain credentials" in defect
        for defect in proposal_defects(dependency_credential, "build it")
    )


def test_lake_adds_customer_only_to_customer_scoped_reads() -> None:
    queries = []

    def transport(request: httpx.Request) -> httpx.Response:
        queries.append((request.url.path, parse_qs(request.url.query.decode())))
        return httpx.Response(200, json={"ok": True, "value": {}})

    lake = Lake(turn(), transport=httpx.MockTransport(transport))
    try:
        lake.read("list_repositories", {})
        lake.read("list_apps", {})
        lake.read("get_app", {"app_id": "app-1"})
        lake.read("receive_app_events", {"app_id": "app-1"})
        lake.read("get_build", {"build_id": "build-1"})
        lake.read("receive_build_events", {"build_id": "build-1"})
        lake.read("build_logs", {"build_id": "build-1"})
        lake.read("repository_tree", {"id": "repo-1"})
    finally:
        lake.close()

    assert queries[0][1]["customer_id"] == ["customer-1"]
    assert queries[1][1]["customer_id"] == ["customer-1"]
    # Lake resolves app reads through the owning customer, so they need it too.
    assert queries[2][1]["customer_id"] == ["customer-1"]
    assert queries[3][1]["customer_id"] == ["customer-1"]
    assert queries[4][1]["customer_id"] == ["customer-1"]
    assert queries[5][1]["customer_id"] == ["customer-1"]
    assert queries[6][1]["customer_id"] == ["customer-1"]
    assert "customer_id" not in queries[7][1]


def test_follow_rollout_names_the_customer_and_app() -> None:
    from agent_application.agent import follow_rollout

    lake = FakeLake()
    result = follow_rollout("customer-1", {"id": "app-1"}, lake)

    assert result["status"] == "ready"
    capability, arguments = lake.reads[0]
    assert capability == "receive_app_events"
    assert arguments["customer_id"] == "customer-1"
    assert arguments["app_id"] == "app-1"
    assert "app" not in arguments


def test_workspace_sql_is_sent_as_get() -> None:
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "value": {}})

    lake = Lake(turn(), transport=httpx.MockTransport(transport))
    try:
        lake.read("workspace_sql", {"statement": "SELECT 1"})
    finally:
        lake.close()

    # Lake answers a POST read with 405 method_not_allowed.
    assert requests[0].method == "GET"
    assert parse_qs(requests[0].url.query.decode())["statement"] == ["SELECT 1"]


def test_build_wait_stops_at_its_deadline(monkeypatch) -> None:
    import agent_application.agent as agent_module

    clock = iter([0.0, 0.0, 400.0, 1000.0])
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: next(clock))

    class Building(FakeLake):
        def read(self, capability, arguments):
            self.reads.append((capability, arguments))
            if capability == "receive_build_events":
                return {"events": [{"phase": "building"}], "next_sequence": 2}
            if capability == "get_build":
                return {"id": "build-1", "phase": "building"}
            return super().read(capability, arguments)

    result = agent_module.wait_for_build("customer-1", "build-1", Building())
    assert result["timed_out"] is True
    assert result["phase"] == "building"


def test_rollout_state_reads_string_and_object_statuses() -> None:
    from agent_application.agent import rollout_state

    assert rollout_state({"status": "Ready"}) == "ready"
    assert rollout_state({"status": {"phase": "Degraded", "message": "crash-looping"}}) == "degraded"
    assert rollout_state({"phase": "Failed"}) == "failed"
    assert rollout_state({}) == ""


def test_a_rollout_that_starts_degraded_is_followed_to_ready(monkeypatch) -> None:
    # agent-application#16: a starting pod reports Degraded before Ready; that is not a failure.
    import agent_application.agent as agent_module

    monkeypatch.setattr(agent_module.time, "sleep", lambda _seconds: None)

    class Starting(FakeLake):
        def read(self, capability, arguments):
            self.reads.append((capability, arguments))
            if capability == "receive_app_events":
                if arguments["from_sequence"] == 1:
                    return {"events": [{"status": "degraded", "message": "probe failed"}],
                            "next_sequence": 2}
                return {"events": [{"status": "ready", "url": "https://app.example"}],
                        "next_sequence": 3}
            return super().read(capability, arguments)

    result = agent_module.follow_rollout("customer-1", {"id": "app-1"}, Starting())
    assert result["status"] == "ready"


def test_a_rollout_still_failing_at_the_deadline_is_a_failure(monkeypatch) -> None:
    import agent_application.agent as agent_module

    clock = iter([0.0, 0.0, 10.0, 500.0, 500.0, 500.0])
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(agent_module.time, "sleep", lambda _seconds: None)

    class Crashing(FakeLake):
        def read(self, capability, arguments):
            self.reads.append((capability, arguments))
            if capability == "receive_app_events":
                return {"events": [], "next_sequence": 2}
            if capability == "get_app":
                return {"id": "app-1", "status": {"phase": "Degraded", "message": "crash-looping"}}
            return super().read(capability, arguments)

    result = agent_module.follow_rollout("customer-1", {"id": "app-1"}, Crashing())
    assert agent_module.rollout_state(result) == "degraded"


def test_missing_logs_are_named_and_never_given_as_the_reason() -> None:
    from agent_application.agent import failure_explanation
    from agent_application.agent import LakeError

    class NoLogs(FakeLake):
        def read(self, capability, arguments):
            if capability == "app_logs":
                raise LakeError("app_logs", "unavailable", "app logs are not configured")
            return super().read(capability, arguments)

    reply = failure_explanation({"id": "app-1"}, {"status": "failed"}, NoLogs())
    assert reply.startswith("The rollout failed: No failure detail was reported.")
    assert "logs are unavailable here (app logs are not configured)" in reply

    timed_out = failure_explanation({"id": "app-1"}, {"status": "pending", "timed_out": True}, NoLogs())
    assert "did not report Ready within 3 minutes" in timed_out
    assert "failed" not in timed_out


def test_a_secret_environment_value_is_a_lake_secret_reference() -> None:
    """agent-application#19, lake#625: lake's create_app takes a lake secret reference and refuses
    a Kubernetes Secret name, so the agent proposes only the forms lake accepts."""
    def defects(value):
        return proposal_defects(proposal(app={"env": {"API_KEY": value}}), "build it")

    accepted = [
        {"secret": {"mount_id": "mnt-1", "name": "forecast-api-key", "field": "value"}},
        {"secret": "lake-secret://mnt-1/forecast-api-key#value"},
        # The name may hold `/` and `#`; the field follows the last `#`.
        {"secret": "lake-secret://mnt-1/team/forecast#2#api.key"},
    ]
    for value in accepted:
        assert defects(value) == [], value

    refused = [
        {"secret": "forecast-api-key"},  # a Kubernetes Secret name
        {"secret": "db-credentials/password"},  # a Kubernetes Secret and key
        {"secret": "lake-secret://mnt-1/forecast-api-key"},  # no field
        {"secret": "lake-secret:///forecast-api-key#value"},  # no mount
        {"secret": "lake-secret://mnt-1/#value"},  # no name
        {"secret": "lake-secret://mnt-1/forecast#bad field"},  # not a Kubernetes Secret key
        {"secret": {"mount_id": "mnt-1", "name": "forecast-api-key"}},  # no field
        {"secret": {"mount_id": "", "name": "forecast-api-key", "field": "value"}},
        {"secret": {"mount_id": "mnt-1", "name": "k", "field": "value", "version": 2}},
        {"secret": "lake-secret://mnt-1/k#value", "value": "x"},
        "plain-value",
    ]
    for value in refused:
        problems = defects(value)
        assert any("lake secret reference" in problem for problem in problems), (value, problems)
        assert any("Environment settings" in problem for problem in problems), (value, problems)

    # A variable whose name does not look secret is still held to the rule when it names a secret.
    kubernetes_name = proposal(app={"env": {"FORECAST_URL": {"secret": "forecast-url"}}})
    assert any("lake secret reference" in problem
               for problem in proposal_defects(kubernetes_name, "build it"))


def test_the_prompt_proposes_lake_secret_references_and_never_asks_for_a_value() -> None:
    from agent_application.agent import PROMPTS

    prompt = " ".join((PROMPTS / "v1.md").read_text().split())
    assert "lake-secret://<mount id>/<name>#<field>" in prompt
    assert '"mount_id"' in prompt
    assert "Environment settings" in prompt
    assert "Never ask for a secret's value" in prompt
    assert '{"secret": "forecast-api-key"}' not in prompt


def test_orama_app_functions_must_be_called_before_their_reads() -> None:
    import ast

    from agent_application.agent import orama_app_misuse

    def problems(source: str) -> list[str]:
        return orama_app_misuse("app.py", ast.parse(source))

    # The production App of 2026-10-26: compiles, then fails on every page.
    wrong = problems("from orama_app import zetta\nrows = zetta.sql('SELECT 1')\n")
    assert len(wrong) == 1 and "line 2" in wrong[0] and "zetta().sql" in wrong[0]
    assert problems("from orama_app import zetta as z\nz.datasets()\n")
    assert problems("import orama_app\norama_app.zetta.sql('SELECT 1')\n")
    assert problems("from orama_app import service\nservice.host\n")

    assert problems("from orama_app import zetta\nrows = zetta().sql('SELECT 1')\n") == []
    assert problems("from orama_app import zetta\nwith zetta() as w:\n    w.sql('SELECT 1')\n") == []
    assert problems("from orama_app import service\nservice('db')['host']\n") == []
    assert problems("import orama_app\norama_app.zetta().sql('SELECT 1')\n") == []
    # A name that is not orama_app's is left alone.
    assert problems("zetta = object()\nzetta.sql\n") == []


def test_app_sql_reads_only_what_the_page_shows() -> None:
    import ast

    from agent_application.agent import unbounded_selects

    def problems(source: str) -> list[str]:
        return unbounded_selects("app.py", ast.parse(source))

    # The production loader of 2026-10-26: whole tables, every ten minutes (agent-application#51).
    loader = (
        "with zetta() as w:\n"
        "    a = w.sql(f'SELECT * FROM {CUSTOMERS_TABLE}')\n"
        "    b = w.sql('select * from segmentation_results.profiles')\n"
    )
    found = problems(loader)
    assert len(found) == 2 and "line 2" in found[0] and "LIMIT" in found[0]

    assert problems("w.sql('SELECT segment, COUNT(*) FROM d.t GROUP BY segment')") == []
    assert problems("w.sql('SELECT AVG(spend) FROM d.t')") == []
    assert problems("w.sql(f'SELECT * FROM {T} LIMIT 50 OFFSET {page * 50}')") == []
    assert problems("w.sql(statement)") == []  # not a literal: nothing to read
    assert problems("db.execute('SELECT * FROM t')") == []  # not an orama_app sql call
