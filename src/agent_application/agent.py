"""Design, validate, commit, and deploy lake Apps after confirmation."""

from __future__ import annotations

import ast
import base64
import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from openai import OpenAI

PROMPT_VERSION = "v1"
PROMPTS = Path(__file__).parent / "prompts"
MAX_MODEL_ROUNDS = 10
MAX_REPAIRS = 2
MAX_FILES = 40
MAX_SOURCE_BYTES = 900 * 1024
BAKED_DEPENDENCIES = {
    "fastapi",
    "httpx",
    "jinja2",
    "numpy",
    "pandas",
    "plotly",
    "pydantic",
    "streamlit",
    "uvicorn",
}
READ_CAPABILITIES = {
    "list_datasets",
    "get_dataset",
    "workspace_sql",
    "list_services",
    "repository_tree",
    "repository_file",
    "list_repositories",
    "list_apps",
    "get_app",
    "app_logs",
    "receive_app_events",
}
# Lake proxies every workspace read as GET and answers POST with 405 method_not_allowed.
POST_READS: set[str] = set()
CUSTOMER_SCOPED_CAPABILITIES = {
    "app_logs",
    "get_app",
    "list_apps",
    "list_datasets",
    "list_repositories",
    "list_services",
    "receive_app_events",
    "workspace_sql",
}
SECRET_NAME = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)", re.I)
SECRET_LITERAL = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*['\"][^'\"]{6,}['\"])",
    re.I,
)
PUBLIC_REQUEST = re.compile(
    r"\b(public|publicly|anyone|anonymous|without (?:sign[- ]?in|auth))\b",
    re.I,
)


@dataclass
class Turn:
    message: str
    lake_url: str
    lake_token: str
    customer_id: str = ""
    conversation_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)


class LakeError(Exception):
    def __init__(self, capability: str, code: str, message: str) -> None:
        super().__init__(f"{capability}: {code}: {message}")
        self.capability = capability
        self.code = code
        self.message = message


class Lake:
    """Lake capabilities called as the signed-in person for one turn."""

    def __init__(
        self,
        turn: Turn,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.turn = turn
        self.client = httpx.Client(
            base_url=turn.lake_url.rstrip("/"),
            timeout=180.0,
            headers={"Authorization": f"Bearer {turn.lake_token}"},
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def call(
        self,
        capability: str,
        arguments: dict[str, Any],
        read: bool,
        use_post: bool = False,
    ) -> dict[str, Any]:
        values = arguments
        if capability in CUSTOMER_SCOPED_CAPABILITIES:
            values = with_customer(arguments, self.turn.customer_id)
        try:
            if read and not use_post:
                params = {
                    key: value if isinstance(value, str) else json.dumps(value)
                    for key, value in values.items()
                    if value is not None
                }
                response = self.client.get(f"/v1/{capability}", params=params)
            else:
                response = self.client.post(f"/v1/{capability}", json=values)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise LakeError(capability, "transport_error", str(error)) from error
        if not isinstance(body, dict) or not body.get("ok"):
            details = body.get("error") if isinstance(body, dict) else None
            error = details if isinstance(details, dict) else {}
            raise LakeError(
                capability,
                str(error.get("code") or "lake_error"),
                str(error.get("message") or f"HTTP {response.status_code}"),
            )
        value = body.get("value") or {}
        return value if isinstance(value, dict) else {"value": value}

    def read(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if capability not in READ_CAPABILITIES:
            raise LakeError(capability, "not_allowed", "not an ApplicationAgent read")
        return self.call(
            capability,
            arguments,
            read=True,
            use_post=capability in POST_READS,
        )

    def write(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.call(capability, arguments, read=False)


def with_customer(arguments: dict[str, Any], customer_id: str) -> dict[str, Any]:
    if customer_id and "customer_id" not in arguments:
        return {**arguments, "customer_id": customer_id}
    return arguments


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lake_read",
            "description": (
                "Read lake metadata needed to build or repair an App. Allowed capabilities: "
                + ", ".join(sorted(READ_CAPABILITIES))
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "enum": sorted(READ_CAPABILITIES)},
                    "input": {"type": "object"},
                },
                "required": ["capability", "input"],
            },
        },
    }
]


def parse_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def proposal_defects(
    answer: dict[str, Any],
    request: str,
    secret_values: tuple[str, ...] = (),
    confirmed: bool = False,
) -> list[str]:
    kind = answer.get("kind")
    if kind == "question":
        question = answer.get("reply") or answer.get("question")
        return [] if isinstance(question, str) and question.strip() else ["question is empty"]
    if kind != "proposal":
        return ["kind must be question or proposal"]
    files = answer.get("files")
    app = answer.get("app")
    problems: list[str] = []
    if not isinstance(files, list) or not files:
        return ["a proposal needs a non-empty files array"]
    if not isinstance(app, dict):
        return ["a proposal needs an app object"]
    problems.extend(file_defects(files, secret_values))
    problems.extend(app_defects(app, files, request, confirmed))
    return problems


def file_defects(files: list[Any], secret_values: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    paths: set[str] = set()
    total = 0
    if len(files) > MAX_FILES:
        problems.append(f"an App may contain at most {MAX_FILES} files")
    for item in files:
        if not isinstance(item, dict):
            problems.append("every file must be an object")
            continue
        path = item.get("path")
        contents = item.get("contents")
        if not isinstance(path, str) or not safe_path(path):
            problems.append(f"invalid App path: {path!r}")
            continue
        if path in paths:
            problems.append(f"duplicate App path: {path}")
        paths.add(path)
        if not isinstance(contents, str):
            problems.append(f"{path} contents must be text")
            continue
        total += len(contents.encode())
        if SECRET_LITERAL.search(contents):
            problems.append(f"{path} contains what looks like a secret")
        for secret in secret_values:
            if len(secret) >= 6 and secret in contents:
                problems.append(f"{path} contains a delegated credential")
        if path.endswith(".py"):
            try:
                ast.parse(contents, filename=path)
            except SyntaxError as error:
                problems.append(
                    f"{path} does not compile: {error.msg} at line {error.lineno}"
                )
    if total > MAX_SOURCE_BYTES:
        problems.append("App source is larger than the proposal limit")
    if "app.toml" not in paths:
        problems.append("app.toml is required")
    if "README.md" not in paths:
        problems.append("README.md is required to document the generated App")
    return problems


def safe_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts


def app_defects(
    app: dict[str, Any],
    files: list[Any],
    request: str,
    confirmed: bool = False,
) -> list[str]:
    problems: list[str] = []
    for name in ("name", "framework", "path"):
        if not isinstance(app.get(name), str) or not str(app[name]).strip():
            problems.append(f"app.{name} must be a non-empty string")
    framework = app.get("framework")
    if framework not in {"fastapi", "streamlit", "static"}:
        problems.append("app.framework must be fastapi, streamlit, or static")
    mode = app.get("mode", "runtime")
    if mode not in {"runtime", "image"}:
        problems.append("app.mode must be runtime or image")
    port = app.get("port", 8080)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append("app.port must be an integer from 1 through 65535")
    auth = app.get("auth", "lake")
    if auth not in {"lake", "public"}:
        problems.append("app.auth must be lake or public")
    if auth == "public" and not confirmed and not PUBLIC_REQUEST.search(request):
        problems.append("public exposure was not explicitly requested")
    repository = app.get("repository")
    if not isinstance(repository, dict):
        problems.append("app.repository must name an existing id or a new private name")
    elif not repository.get("id") and not repository.get("name"):
        problems.append("app.repository needs id or name")
    env = app.get("env", {})
    if not isinstance(env, dict):
        problems.append("app.env must be an object")
    else:
        for name, value in env.items():
            if SECRET_NAME.search(str(name)):
                if not secret_reference(value):
                    problems.append(
                        f"app.env cannot carry plaintext secret {name}; use a secret reference"
                    )
            elif SECRET_LITERAL.search(str(value)):
                problems.append(f"app.env cannot carry secret value {name}; use a binding")
    bindings = app.get("bindings", {})
    if not isinstance(bindings, dict):
        problems.append("app.bindings must be an object")
    else:
        services = bindings.get("services", [])
        if not isinstance(services, list) or not all(
            isinstance(service, str) and service for service in services
        ):
            problems.append("app.bindings.services must be an array of Service names")
    manifest = next(
        (
            item.get("contents")
            for item in files
            if isinstance(item, dict) and item.get("path") == "app.toml"
        ),
        None,
    )
    if isinstance(manifest, str):
        problems.extend(manifest_defects(manifest, app))
    return problems


def secret_reference(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"secret"}:
        return False
    name = value.get("secret")
    return isinstance(name, str) and bool(name.strip())


def manifest_defects(manifest: str, app: dict[str, Any]) -> list[str]:
    try:
        values = tomllib.loads(manifest)
    except tomllib.TOMLDecodeError as error:
        return [f"app.toml is invalid: {error}"]
    problems: list[str] = []
    for key in ("name", "framework", "entrypoint"):
        if not isinstance(values.get(key), str) or not values[key].strip():
            problems.append(f"app.toml {key} must be a non-empty string")
    if values.get("name") != app.get("name"):
        problems.append("app.toml name must match app.name")
    if values.get("framework") != app.get("framework"):
        problems.append("app.toml framework must match app.framework")
    port = values.get("port", 8080)
    if port != app.get("port", 8080):
        problems.append("app.toml port must match app.port")
    dependencies = values.get("dependencies", [])
    names = dependency_names(dependencies)
    outside = sorted(names - BAKED_DEPENDENCIES)
    if outside and app.get("mode", "runtime") != "image":
        problems.append(
            "dependencies outside app-runtime's baked set require image mode: "
            + ", ".join(outside)
        )
    manifest_bindings = normalize_manifest_bindings(values.get("bindings", {}))
    proposed_bindings = normalize_app_bindings(app.get("bindings", {}))
    if manifest_bindings != proposed_bindings:
        problems.append("app.toml bindings must match app.bindings")
    return problems


def dependency_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(name).lower().replace("_", "-") for name in value}
    if not isinstance(value, list):
        return set()
    names = set()
    for requirement in value:
        if isinstance(requirement, str):
            name = re.split(r"[<>=!~\s\[]", requirement, maxsplit=1)[0]
            if name:
                names.add(name.lower().replace("_", "-"))
    return names


def normalize_manifest_bindings(value: Any) -> tuple[bool, tuple[str, ...]]:
    if isinstance(value, list):
        zetta = "zetta" in value
        services = sorted(
            item.removeprefix("service:")
            for item in value
            if isinstance(item, str) and item.startswith("service:")
        )
        return zetta, tuple(services)
    if isinstance(value, dict):
        return bool(value.get("zetta", False)), tuple(sorted(value.get("services", [])))
    return False, ()


def normalize_app_bindings(value: Any) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(value, dict):
        return False, ()
    zetta = value.get("zetta", False)
    enabled = bool(zetta)
    services = value.get("services", [])
    if not isinstance(services, list):
        services = []
    return enabled, tuple(sorted(str(service) for service in services))


def answer_turn(
    turn: Turn,
    history: list[dict[str, Any]],
    client: OpenAI,
    model: str,
    lake: Lake | None = None,
) -> dict[str, Any]:
    owned = lake is None
    lake = lake or Lake(turn)
    try:
        confirmed = turn.context.get("confirmed_proposal")
        if isinstance(confirmed, dict):
            return deploy_confirmed(turn, confirmed, lake)
        return design(turn, history, client, model, lake)
    finally:
        if owned:
            lake.close()


def design(
    turn: Turn,
    history: list[dict[str, Any]],
    client: OpenAI,
    model: str,
    lake: Lake,
) -> dict[str, Any]:
    if not history:
        history.append(
            {
                "role": "system",
                "content": (PROMPTS / f"{PROMPT_VERSION}.md").read_text(),
            }
        )
    history.append({"role": "user", "content": turn.message})
    answer: dict[str, Any] = {}
    for repair in range(MAX_REPAIRS + 1):
        answer = model_answer(history, client, model, lake)
        problems = proposal_defects(answer, turn.message, (turn.lake_token,))
        if not problems:
            break
        if repair < MAX_REPAIRS:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "Fix every validation failure and answer again with the complete JSON "
                        "object:\n" + "\n".join(f"- {problem}" for problem in problems)
                    ),
                }
            )
    else:
        problems = ["the proposal could not be validated"]
    if problems:
        return {
            "reply": "I could not produce a safe App proposal: " + "; ".join(problems),
            "result": {
                "kind": "question",
                "files": [],
                "app": {},
                "confirm": False,
            },
        }
    if answer.get("kind") == "question":
        reply = str(answer.get("reply") or answer.get("question"))
        return {
            "reply": reply,
            "result": {
                "kind": "question",
                "files": [],
                "app": {},
                "confirm": False,
            },
        }
    files = answer["files"]
    app = with_safe_defaults(answer["app"])
    reply = str(answer.get("reply") or "Review this App proposal before deployment.")
    return {
        "reply": reply,
        "result": {
            "kind": "proposal",
            "files": files,
            "app": app,
            "confirm": True,
        },
    }


def model_answer(
    history: list[dict[str, Any]],
    client: OpenAI,
    model: str,
    lake: Lake,
) -> dict[str, Any]:
    for _round in range(MAX_MODEL_ROUNDS):
        completion = client.chat.completions.create(
            model=model,
            messages=history,
            tools=TOOLS,
            response_format={"type": "json_object"},
        )
        message = completion.choices[0].message
        dumped = message.model_dump(exclude_none=True)
        history.append(dumped)
        if not message.tool_calls:
            return parse_json(message.content)
        for call in message.tool_calls:
            arguments = parse_json(call.function.arguments)
            capability = str(arguments.get("capability") or "")
            inputs = arguments.get("input")
            inputs = inputs if isinstance(inputs, dict) else {}
            try:
                observation = lake.read(capability, inputs)
                content = json.dumps({"ok": True, "value": observation}, default=str)
            except LakeError as error:
                content = json.dumps(
                    {
                        "ok": False,
                        "error": {"code": error.code, "message": error.message},
                    }
                )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content[:30_000],
                }
            )
    return {}


def with_safe_defaults(app: dict[str, Any]) -> dict[str, Any]:
    return {
        **app,
        "mode": app.get("mode", "runtime"),
        "port": app.get("port", 8080),
        "auth": app.get("auth", "lake"),
        "bindings": app.get("bindings", {"zetta": False, "services": []}),
        "env": app.get("env", {}),
    }


def deploy_confirmed(turn: Turn, confirmed: dict[str, Any], lake: Lake) -> dict[str, Any]:
    proposal = confirmed.get("result") if isinstance(confirmed.get("result"), dict) else confirmed
    files = proposal.get("files") if isinstance(proposal, dict) else None
    app = proposal.get("app") if isinstance(proposal, dict) else None
    answer = {"kind": "proposal", "files": files, "app": app}
    problems = proposal_defects(
        answer,
        turn.message,
        (turn.lake_token,),
        confirmed=True,
    )
    if problems:
        return {
            "reply": "The confirmed proposal is invalid and was not deployed: "
            + "; ".join(problems),
            "result": {
                "kind": "question",
                "files": [],
                "app": {},
                "confirm": False,
            },
        }
    assert isinstance(files, list)
    assert isinstance(app, dict)
    app = with_safe_defaults(app)
    repository = ensure_repository(turn, app, lake)
    revision = commit_files(repository, app, files, lake)
    deployed = create_or_update_app(turn, app, repository, revision, lake)
    rollout = follow_rollout(turn.customer_id, deployed, lake)
    final_app = {
        **app,
        **deployed,
        "repository": repository,
        "revision": revision,
        "rollout": rollout,
    }
    failed = rollout.get("status") in {"failed", "error"}
    if failed:
        explanation = failure_explanation(final_app, rollout, lake)
        return {
            "reply": explanation,
            "result": {
                "kind": "deployed",
                "files": files,
                "app": final_app,
                "confirm": False,
            },
        }
    url = final_app.get("url") or rollout.get("url") or app.get("host") or ""
    reply = "The App is deployed."
    if url:
        reply += f" {url}"
    return {
        "reply": reply,
        "result": {
            "kind": "deployed",
            "files": files,
            "app": final_app,
            "confirm": False,
        },
    }


def ensure_repository(turn: Turn, app: dict[str, Any], lake: Lake) -> dict[str, Any]:
    repository = dict(app["repository"])
    if repository.get("id"):
        return repository
    existing = matching_customer_item(
        lake.read("list_repositories", {"limit": 1_000}),
        "items",
        str(repository["name"]),
        turn.customer_id,
    )
    if existing:
        repository_id = existing.get("id") or existing.get("repository_id")
        if not repository_id:
            raise LakeError(
                "list_repositories",
                "invalid_response",
                "matching repository id is missing",
            )
        return {**existing, **repository, "id": repository_id}
    created = lake.write(
        "create_repository",
        {
            "customer_id": turn.customer_id,
            "name": repository["name"],
            "description": f"Source for the {app['name']} App",
            "visibility": "private",
        },
    )
    repository_id = created.get("id") or created.get("repository_id")
    if not repository_id:
        raise LakeError("create_repository", "invalid_response", "repository id is missing")
    return {**repository, **created, "id": repository_id, "visibility": "private"}


def matching_customer_item(
    value: dict[str, Any],
    key: str,
    name: str,
    customer_id: str,
) -> dict[str, Any] | None:
    items = value.get(key)
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("name") == name and item.get("customer_id") == customer_id:
            return item
    return None


def commit_files(
    repository: dict[str, Any],
    app: dict[str, Any],
    files: list[dict[str, Any]],
    lake: Lake,
) -> str:
    root = str(app.get("path") or "").strip("/")
    operations = []
    for item in files:
        path = f"{root}/{item['path']}" if root else item["path"]
        content = base64.b64encode(item["contents"].encode()).decode()
        operations.append({"type": "file", "path": path, "content": content})
    committed = lake.write(
        "commit_repository_files",
        {
            "id": repository["id"],
            "revision": repository.get("revision", "main"),
            "message": f"Build the {app['name']} App",
            "operations": operations,
        },
    )
    revision = committed.get("commitOid") or committed.get("revision")
    if not revision:
        raise LakeError("commit_repository_files", "invalid_response", "revision is missing")
    return str(revision)


def create_or_update_app(
    turn: Turn,
    app: dict[str, Any],
    repository: dict[str, Any],
    revision: str,
    lake: Lake,
) -> dict[str, Any]:
    source = {
        "repository": repository["id"],
        "path": app["path"],
        "revision": revision,
    }
    app_id = app.get("id")
    if not app_id:
        existing = matching_customer_item(
            lake.read("list_apps", {}),
            "apps",
            str(app["name"]),
            turn.customer_id,
        )
        if existing:
            app_id = existing.get("id") or existing.get("app_id")
            if not app_id:
                raise LakeError(
                    "list_apps",
                    "invalid_response",
                    "matching App id is missing",
                )
    if not app_id:
        created = lake.write(
            "create_app",
            {
                "customer_id": turn.customer_id,
                "name": app["name"],
                "mode": app["mode"],
                "source": source,
                "framework": app["framework"],
                "port": app["port"],
                "host": app.get("host"),
                "auth": app["auth"],
                "bindings": app["bindings"],
                "env": app["env"],
            },
        )
        app_id = created.get("id") or created.get("app_id")
        if not app_id:
            raise LakeError("create_app", "invalid_response", "App id is missing")
    else:
        created = dict(app)
    if app["mode"] == "image":
        lake.write(
            "build_app",
            {"customer_id": turn.customer_id, "app": app_id},
        )
    deployed = lake.write(
        "deploy_app",
        {
            "customer_id": turn.customer_id,
            "app": app_id,
            "source": source,
        },
    )
    return {**created, **deployed, "id": app_id, "source": source}


def follow_rollout(customer_id: str, app: dict[str, Any], lake: Lake) -> dict[str, Any]:
    app_id = str(app["id"])
    sequence = 1
    latest: dict[str, Any] = {}
    for _attempt in range(6):
        try:
            value = lake.read(
                "receive_app_events",
                {
                    "customer_id": customer_id,
                    "app_id": app_id,
                    "from_sequence": sequence,
                    "wait_ms": 5_000,
                },
            )
        except LakeError as error:
            if error.code in {"unknown_capability", "not_found"}:
                break
            raise
        sequence = int(value.get("next_sequence") or sequence)
        events = value.get("events") or []
        for event in events:
            if isinstance(event, dict):
                latest = event
                status = str(event.get("status") or event.get("type") or "").lower()
                if status in {"ready", "deployed", "succeeded", "failed", "error"}:
                    return {**event, "status": status}
        if not events:
            break
        time.sleep(0.01)
    if latest:
        return latest
    try:
        observed = lake.read("get_app", {"customer_id": customer_id, "app_id": app_id})
    except LakeError:
        observed = {}
    return observed or {"status": str(app.get("status") or "submitted")}


def failure_explanation(
    app: dict[str, Any],
    rollout: dict[str, Any],
    lake: Lake,
) -> str:
    try:
        logs = lake.read("app_logs", {"app": app["id"], "limit": 100})
    except LakeError as error:
        logs = {"error": error.message}
    detail = rollout.get("message") or rollout.get("reason") or logs.get("error")
    if not detail:
        entries = logs.get("logs") or logs.get("entries") or []
        detail = entries[-1] if entries else "No failure detail was reported."
    return f"The rollout failed: {detail}"
