"""Design, validate, commit, and deploy lake Apps after confirmation."""

from __future__ import annotations

import ast
import base64
import json
import posixpath
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
# A cold image build (pip install into app-runtime) can take minutes; each read below is task
# activity, which keeps lake's claim lease alive.
BUILD_DEADLINE_SECONDS = 15 * 60
BUILD_WAIT_MS = 20_000
# How long a confirmed turn follows a rollout to Ready before judging it (agent-application#16).
# A new pod reports Degraded or a failed probe while it starts; only a state that persists to the
# deadline is a failure.
ROLLOUT_DEADLINE_SECONDS = 3 * 60
ROLLOUT_READY = {"ready", "deployed", "succeeded", "running"}
ROLLOUT_FAILED = {"failed", "error", "degraded", "crashloopbackoff"}
MAX_BUILD_LOG_LINES = 20
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
    "build_logs",
    "get_build",
    "list_builds",
    "get_auto_fix",
    "receive_app_events",
    "receive_build_events",
}
# Lake proxies every workspace read as GET and answers POST with 405 method_not_allowed.
POST_READS: set[str] = set()
CUSTOMER_SCOPED_CAPABILITIES = {
    "app_logs",
    "build_logs",
    "get_app",
    "get_build",
    "get_auto_fix",
    "list_apps",
    "list_builds",
    "list_datasets",
    "list_repositories",
    "list_services",
    "receive_app_events",
    "receive_build_events",
    "workspace_sql",
}
SECRET_NAME = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)", re.I)
SECRET_LITERAL = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*['\"][^'\"]{6,}['\"])",
    re.I,
)
# A Kubernetes Secret key, which a lake secret reference's field must be (lake docs/api/web-apps.md).
SECRET_FIELD = re.compile(r"[-._a-zA-Z0-9]+")
LAKE_SECRET_SCHEME = "lake-secret://"
SECRET_REFERENCE_RULE = (
    "a lake secret reference, {\"secret\": {\"mount_id\", \"name\", \"field\"}} or "
    "{\"secret\": \"lake-secret://<mount id>/<name>#<field>\"}, never a Kubernetes Secret name; "
    "when the person has not named an existing secret, leave the variable out of app.env and "
    "tell them to set it on the App's Environment settings"
)
DEPENDENCY_CREDENTIAL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s]+@", re.I)
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
    # `app` builds a new App; `repair` and `change` work on the existing App in
    # `context.app_id` (lake#1207, repair.py).
    kind: str = "app"


class LakeError(Exception):
    def __init__(self, capability: str, code: str, message: str) -> None:
        super().__init__(f"{capability}: {code}: {message}")
        self.capability = capability
        self.code = code
        self.message = message


@dataclass
class AppDeployment:
    app: dict[str, Any]
    build: dict[str, Any] | None = None
    failure: str | None = None


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
        value = self.call(
            capability,
            arguments,
            read=True,
            use_post=capability in POST_READS,
        )
        return exact_file_text(value) if capability == "repository_file" else value

    def write(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.call(capability, arguments, read=False)


def exact_file_text(value: dict[str, Any]) -> dict[str, Any]:
    """A repository file's `contents`, from its exact bytes.

    Lake's `contents` writes every non-ASCII byte as literal `\\xNN` text (lake#1238), so a file read
    from it and committed back kept the escapes: a live App's title became `\\xF0\\x9F\\x91\\xA5`
    instead of an emoji. `content_base64` is exact. Valid UTF-8 becomes `contents`; anything else is
    binary and has no `contents`.
    """
    encoded = value.get("content_base64")
    if not isinstance(encoded, str):
        return value
    value = dict(value)
    try:
        value["contents"] = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        value.pop("contents", None)
    return value


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
                tree = ast.parse(contents, filename=path)
            except SyntaxError as error:
                problems.append(
                    f"{path} does not compile: {error.msg} at line {error.lineno}"
                )
            else:
                problems.extend(orama_app_misuse(path, tree))
                problems.extend(unbounded_selects(path, tree))
    if total > MAX_SOURCE_BYTES:
        problems.append("App source is larger than the proposal limit")
    if "app.toml" not in paths:
        problems.append("app.toml is required")
    if "README.md" not in paths:
        problems.append("README.md is required to document the generated App")
    return problems


ORAMA_APP_FUNCTIONS = {
    "zetta": "call it first: `zetta().sql(...)`, or `with zetta() as workspace: workspace.sql(...)`",
    "service": "call it with the binding name: `service(\"<name>\")[\"<field>\"]`",
}


def orama_app_misuse(path: str, tree: ast.AST) -> list[str]:
    """Attribute reads on orama_app's functions themselves, such as `zetta.sql(...)`.

    `zetta` and `service` are functions that return a client and a connection dictionary. An App that reads
    `zetta.sql` compiles, then stops on every page with `AttributeError: 'function' object has no attribute 'sql'`.
    """
    local: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "orama_app":
            for alias in node.names:
                if alias.name in ORAMA_APP_FUNCTIONS:
                    local[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "orama_app":
                    modules.add(alias.asname or alias.name)
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        target = node.value
        function = None
        if isinstance(target, ast.Name) and target.id in local:
            function = local[target.id]
        elif (
            isinstance(target, ast.Attribute)
            and target.attr in ORAMA_APP_FUNCTIONS
            and isinstance(target.value, ast.Name)
            and target.value.id in modules
        ):
            function = target.attr
        if function is not None:
            problems.append(
                f"{path} line {node.lineno}: `{function}.{node.attr}` reads an attribute of the function "
                f"orama_app.{function}; {ORAMA_APP_FUNCTIONS[function]}"
            )
    return problems


SQL_SELECT = re.compile(r"\bselect\b", re.I)
SQL_BOUNDED = re.compile(r"\blimit\b|\bgroup\s+by\b|\b(?:count|sum|avg|min|max)\s*\(", re.I)


def unbounded_selects(path: str, tree: ast.AST) -> list[str]:
    """`sql(...)` calls whose literal statement reads rows with neither a LIMIT nor an aggregate.

    Such a read copies a whole table into the App (agent-application#51); Lake answers `too_many_rows` past 5,000 rows.
    Only the statement's literal text is read: an f-string's placeholders count as nothing.
    """
    problems = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sql"
            and node.args
        ):
            continue
        statement = node.args[0]
        if isinstance(statement, ast.Constant) and isinstance(statement.value, str):
            text = statement.value
        elif isinstance(statement, ast.JoinedStr):
            text = " ".join(
                part.value
                for part in statement.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        else:
            continue
        if SQL_SELECT.search(text) and not SQL_BOUNDED.search(text):
            problems.append(
                f"{path} line {node.lineno}: the SQL reads rows with no LIMIT and no aggregate; read only what "
                "the page shows: aggregate in SQL (GROUP BY, COUNT, SUM, AVG, MIN, MAX) or page with LIMIT and "
                "OFFSET (at most 5,000 rows a query)"
            )
    return problems


def safe_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts


def repository_path(root: str, path: str) -> str:
    """`path` under the App's `root`, as Lake stores it: no `.` or empty components, no leading or
    trailing slash. Lake refuses `./main.py` or `./main.py` under root `.` (invalid repository path)."""
    joined = posixpath.normpath(posixpath.join(root.strip().strip("/") or ".", path.strip()))
    return "" if joined == "." else joined.strip("/")


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
            if isinstance(value, dict) and "secret" in value:
                if not secret_reference(value):
                    problems.append(f"app.env {name} must be {SECRET_REFERENCE_RULE}")
            elif SECRET_NAME.search(str(name)):
                problems.append(
                    f"app.env cannot carry plaintext secret {name}; it must be "
                    f"{SECRET_REFERENCE_RULE}"
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
    """A secret environment value as lake accepts it (lake#625, docs/api/web-apps.md): one field of
    one secret in a lake secret mount, `{"secret": {"mount_id", "name", "field"}}` or
    `{"secret": "lake-secret://<mount id>/<name>#<field>"}`. A Kubernetes Secret name is refused:
    a Kubernetes Secret is only the projection the operator writes when the App is deployed."""
    if not isinstance(value, dict) or set(value) != {"secret"}:
        return False
    reference = value["secret"]
    if isinstance(reference, dict):
        if set(reference) != {"mount_id", "name", "field"}:
            return False
        if not all(isinstance(reference[key], str) for key in reference):
            return False
        return (
            bool(reference["mount_id"].strip())
            and bool(reference["name"].strip())
            and SECRET_FIELD.fullmatch(reference["field"]) is not None
        )
    return isinstance(reference, str) and lake_secret_ref(reference)


def lake_secret_ref(text: str) -> bool:
    """`lake-secret://<mount id>/<name>#<field>`: the name may contain `/` and `#`, and the field
    follows the last `#`."""
    if not text.startswith(LAKE_SECRET_SCHEME):
        return False
    mount_id, slash, rest = text[len(LAKE_SECRET_SCHEME):].partition("/")
    name, hash_, field = rest.rpartition("#")
    return (
        bool(mount_id) and bool(slash) and bool(hash_) and bool(name)
        and SECRET_FIELD.fullmatch(field) is not None
    )


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
    requirements = dependency_requirements(dependencies)
    if any(DEPENDENCY_CREDENTIAL.search(requirement) for requirement in requirements):
        problems.append("app.toml dependencies cannot contain credentials")
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


def dependency_requirements(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if not isinstance(value, dict):
        return []
    requirements = []
    for name, version in value.items():
        requirement = str(name)
        if isinstance(version, str) and version not in {"", "*"}:
            if version[0] in "<>=!~[":
                requirement += version
            else:
                requirement += f"=={version}"
        requirements.append(requirement)
    return requirements


def manifest_dependencies(files: list[dict[str, Any]]) -> list[str]:
    manifest = next(
        (
            item.get("contents")
            for item in files
            if item.get("path") == "app.toml"
        ),
        "",
    )
    if not isinstance(manifest, str):
        return []
    try:
        dependencies = tomllib.loads(manifest).get("dependencies", [])
    except tomllib.TOMLDecodeError:
        return []
    return dependency_requirements(dependencies)


def external_dependencies(files: list[dict[str, Any]]) -> list[str]:
    requirements = manifest_dependencies(files)
    outside = []
    for requirement in requirements:
        names = dependency_names([requirement])
        if names and names.isdisjoint(BAKED_DEPENDENCIES):
            outside.append(requirement)
    return outside


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
        if turn.kind in {"repair", "change"}:
            from agent_application.repair import answer_app_turn

            return answer_app_turn(turn, history, client, model, lake)
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
    deployment = create_or_update_app(turn, app, repository, revision, files, lake)
    if deployment.failure:
        final_app = {
            **app,
            **deployment.app,
            "repository": repository,
            "revision": revision,
            "build": deployment.build or {},
        }
        return {
            "reply": deployment.failure,
            "result": {
                "kind": "question",
                "files": files,
                "app": final_app,
                "confirm": False,
            },
        }
    deployed = deployment.app
    rollout = follow_rollout(turn.customer_id, deployed, lake)
    final_app = {
        **app,
        **deployed,
        "repository": repository,
        "revision": revision,
        "rollout": rollout,
    }
    failed = rollout_state(rollout) in ROLLOUT_FAILED or rollout.get("timed_out")
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
    root = str(app.get("path") or "")
    operations = []
    for item in files:
        path = repository_path(root, item["path"])
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
    files: list[dict[str, Any]],
    lake: Lake,
) -> AppDeployment:
    source = {
        "repository": repository["id"],
        "path": app["path"],
        "revision": revision,
    }
    dependencies = external_dependencies(files)
    environment = dict(app["env"])
    environment.pop("APP_DEPS", None)
    if dependencies:
        environment["APP_DEPS"] = " ".join(dependencies)
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
                "mode": "runtime",
                "source": source,
                "framework": app["framework"],
                "port": app["port"],
                "host": app.get("host"),
                "auth": app["auth"],
                "bindings": app["bindings"],
                "env": environment,
            },
        )
        app_id = created.get("id") or created.get("app_id")
        if not app_id:
            raise LakeError("create_app", "invalid_response", "App id is missing")
    else:
        created = dict(app)
    build = None
    deployment = {"source": source}
    if dependencies:
        # Name the source: an App already running a built image has no stored source in lake.
        queued = lake.write(
            "build_app",
            {"customer_id": turn.customer_id, "app": app_id, "source": source},
        )
        build_id = queued.get("build_id") or queued.get("id")
        if not build_id:
            raise LakeError("build_app", "invalid_response", "build id is missing")
        build = wait_for_build(turn.customer_id, str(build_id), lake)
        build = {**queued, **build, "id": str(build_id)}
        if build_status(build) != "succeeded":
            failure = build_failure_explanation(turn.customer_id, build, lake)
            pending = {**created, "id": app_id, "source": source}
            return AppDeployment(app=pending, build=build, failure=failure)
        image = build_image_reference(build)
        if not image:
            raise LakeError(
                "get_build",
                "invalid_response",
                "succeeded build image reference is missing",
            )
        deployment = {"image": image}
    deployed = lake.write(
        "deploy_app",
        {
            "customer_id": turn.customer_id,
            "app": app_id,
            **deployment,
        },
    )
    result = {**created, **deployed, "id": app_id, "source": source}
    if build:
        result["build"] = build
        result["image"] = deployment["image"]
    return AppDeployment(app=result, build=build)


def wait_for_build(customer_id: str, build_id: str, lake: Lake) -> dict[str, Any]:
    sequence = 1
    latest: dict[str, Any] = {}
    deadline = time.monotonic() + BUILD_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        events = lake.read(
            "receive_build_events",
            {
                "customer_id": customer_id,
                "build_id": build_id,
                "from_sequence": sequence,
                "wait_ms": BUILD_WAIT_MS,
            },
        )
        sequence = int(events.get("next_sequence") or sequence)
        values = events.get("events") or []
        event = next(
            (value for value in reversed(values) if isinstance(value, dict)),
            {},
        )
        observed = lake.read(
            "get_build",
            {"customer_id": customer_id, "build_id": build_id},
        )
        latest = {**observed, **event}
        if build_status(latest) in {"succeeded", "failed"}:
            return latest
    return {**latest, "timed_out": True}


def build_status(build: dict[str, Any]) -> str:
    values = (build.get("phase"), build.get("status"), build.get("type"))
    normalized = {str(value).strip().lower() for value in values if value}
    if normalized & {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if normalized & {"succeeded", "success", "done", "complete", "completed"}:
        return "succeeded"
    return "pending"


def build_image_reference(build: dict[str, Any]) -> str:
    image = build.get("image") or build.get("image_reference")
    if image:
        return str(image)
    target = str(build.get("target") or "")
    digest = str(build.get("digest") or "")
    if target and digest:
        return f"{target}@{digest}"
    return target


def build_failure_explanation(
    customer_id: str,
    build: dict[str, Any],
    lake: Lake,
) -> str:
    build_id = str(build["id"])
    phase = str(build.get("phase") or build.get("status") or "did not complete")
    message = str(build.get("message") or "").strip()
    try:
        logs = lake.read(
            "build_logs",
            {"customer_id": customer_id, "build_id": build_id},
        )
    except LakeError as error:
        logs = {"lines": [], "error": error.message}
    lines = [str(line) for line in logs.get("lines", [])]
    detail = f"Build {build_id} failed in phase {phase}."
    if build.get("timed_out"):
        detail = f"Build {build_id} did not reach a terminal phase; last phase was {phase}."
    if message:
        detail += f" {message}"
    if lines:
        detail += "\nLast build log lines:\n" + "\n".join(
            lines[-MAX_BUILD_LOG_LINES:]
        )
    elif logs.get("error"):
        detail += f" Build logs were unavailable: {logs['error']}"
    else:
        detail += " No build log lines were reported."
    return detail


def follow_rollout(customer_id: str, app: dict[str, Any], lake: Lake) -> dict[str, Any]:
    """Follow the rollout until Ready, or until the deadline (agent-application#16).

    A starting pod passes through states that look like failure (Degraded, a failed probe); judging
    the first one told people their App failed moments before it became Ready. Ready ends the wait
    at once; anything else is only the answer if it is still the state at the deadline, confirmed by
    a fresh `get_app`.
    """
    app_id = str(app["id"])
    deadline = time.monotonic() + ROLLOUT_DEADLINE_SECONDS
    sequence = 1
    latest: dict[str, Any] = {}
    events_available = True
    while time.monotonic() < deadline:
        if events_available:
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
                if error.code not in {"unknown_capability", "not_found"}:
                    raise
                events_available = False
                continue
            sequence = int(value.get("next_sequence") or sequence)
            for event in value.get("events") or []:
                if isinstance(event, dict):
                    status = str(event.get("status") or event.get("type") or "").lower()
                    latest = {**event, "status": status} if status else event
                    if status in ROLLOUT_READY:
                        return latest
            if value.get("events"):
                continue
        observed = observe_app(customer_id, app_id, lake)
        if rollout_state(observed) in ROLLOUT_READY:
            return observed
        if observed:
            latest = observed
        time.sleep(2)
    final = observe_app(customer_id, app_id, lake)
    if final:
        latest = final
    if not latest:
        latest = {"status": str(app.get("status") or "submitted")}
    if rollout_state(latest) not in ROLLOUT_READY | ROLLOUT_FAILED:
        latest = {**latest, "timed_out": True}
    return latest


def observe_app(customer_id: str, app_id: str, lake: Lake) -> dict[str, Any]:
    try:
        observed = lake.read("get_app", {"customer_id": customer_id, "app_id": app_id})
    except LakeError:
        return {}
    return observed if isinstance(observed, dict) else {}


def rollout_state(rollout: dict[str, Any]) -> str:
    """The rollout's state as text; lake reports it as a string or as a status object."""
    for key in ("status", "phase", "type"):
        value = rollout.get(key)
        if isinstance(value, dict):
            value = value.get("phase") or value.get("state") or value.get("status")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def failure_explanation(
    app: dict[str, Any],
    rollout: dict[str, Any],
    lake: Lake,
) -> str:
    """Why the rollout failed. Missing logs are said as such, never given as the reason."""
    if rollout.get("timed_out"):
        return (
            "The App was deployed but did not report Ready within "
            f"{ROLLOUT_DEADLINE_SECONDS // 60} minutes; its last state was "
            f"{rollout_state(rollout) or 'unknown'}. Check it on the App page."
        )
    detail = rollout.get("message") or rollout.get("reason")
    logs_note = ""
    try:
        logs = lake.read("app_logs", {"app_id": app["id"], "limit": 100})
    except LakeError as error:
        logs = {}
        logs_note = f" Its logs are unavailable here ({error.message})."
    if not detail:
        entries = logs.get("logs") or logs.get("entries") or logs.get("lines") or []
        detail = entries[-1] if entries else "No failure detail was reported."
    return f"The rollout failed: {detail}{logs_note}"
