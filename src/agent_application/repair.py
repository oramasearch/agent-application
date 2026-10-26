"""Repair a failed App, or change an App from instructions (lake#1207).

A `repair` or `change` turn names one existing App (`context.app_id`). The agent reads what the App
reported (its events, runtime logs, newest build and its log, its source and bindings), asks the
model for the change, and proposes it as a diff of the files it changes. Nothing is written on that
turn. Lake hands the reviewed proposal back on a later turn as `context.confirmed_proposal`; only
then does the agent commit the changed files to the App's repository (one commit, whose writes are
liquid drips), rebuild when its dependencies need an image, redeploy that revision, follow the
rollout and report. An auto-fix turn (`context.auto_fix`, which lake grants only when the person
turned auto-fix on and attempts remain) applies its own proposal without a confirmation.
"""

from __future__ import annotations

import ast
import base64
import difflib
import json
import tomllib
from typing import Any

from openai import OpenAI

from agent_application.agent import (
    BAKED_DEPENDENCIES,
    MAX_FILES,
    MAX_SOURCE_BYTES,
    PROMPTS,
    ROLLOUT_FAILED,
    SECRET_LITERAL,
    Lake,
    LakeError,
    Turn,
    build_failure_explanation,
    build_image_reference,
    build_status,
    dependency_names,
    dependency_requirements,
    failure_explanation,
    follow_rollout,
    model_answer,
    rollout_state,
    safe_path,
    wait_for_build,
)

REPAIR_PROMPT_VERSION = "repair-v1"
TURN_KINDS = {"repair", "change"}
MAX_REPAIRS = 2
MAX_EVENTS = 30
MAX_LOG_LINES = 120
MAX_BUILD_LOG_LINES = 60
MAX_FILE_BYTES = 120 * 1024
MAX_DIFF_CHARS = 60_000


def answer_app_turn(
    turn: Turn,
    history: list[dict[str, Any]],
    client: OpenAI,
    model: str,
    lake: Lake,
) -> dict[str, Any]:
    kind = turn.kind
    if kind not in TURN_KINDS:
        return question(f"A turn on an existing App is a repair or a change, not {kind!r}.")
    app_id = str(turn.context.get("app_id") or "")
    if not app_id:
        return question("This turn does not name the App to work on.")
    confirmed = turn.context.get("confirmed_proposal")
    if isinstance(confirmed, dict):
        return apply_proposal(turn, kind, app_id, confirmed, lake, automatic=False)
    facts = gather_facts(turn, app_id, lake)
    if facts.get("problem"):
        return question(str(facts["problem"]))
    designed = propose(turn, kind, facts, history, client, model, lake)
    result = designed["result"]
    auto_fix = turn.context.get("auto_fix")
    if kind == "repair" and isinstance(auto_fix, dict):
        if result.get("kind") != "proposal":
            return {
                "reply": "Auto-fix could not find a fix: " + designed["reply"],
                "result": {**result, "auto_fix": auto_fix},
            }
        applied = apply_proposal(turn, kind, app_id, result, lake, automatic=True)
        applied["result"]["auto_fix"] = auto_fix
        applied["reply"] = (
            f"Auto-fix attempt {auto_fix.get('attempt')} of {auto_fix.get('max_attempts')}: "
            f"{result.get('diagnosis', '')}\n\n{applied['reply']}"
        )
        return applied
    return designed


def question(reply: str) -> dict[str, Any]:
    return {
        "reply": reply,
        "result": {"kind": "question", "files": [], "app": {}, "confirm": False},
    }


def optional_read(lake: Lake, capability: str, arguments: dict[str, Any]) -> tuple[dict, str]:
    try:
        return lake.read(capability, arguments), ""
    except LakeError as error:
        return {}, f"{error.code}: {error.message}"


def app_source(app: dict[str, Any], repair: dict[str, Any]) -> dict[str, Any]:
    """Where the App's source is: its runtime source, else the source lake recorded when it was
    last deployed (an App deployed from a built image keeps no source in its desired state)."""
    for candidate in (app.get("source"), repair.get("source")):
        if isinstance(candidate, dict) and candidate.get("repository"):
            return candidate
    return {}


def source_root(source: dict[str, Any]) -> str:
    root = str(source.get("path") or "").strip().strip("/")
    return "" if root in {"", "."} else root


def gather_facts(turn: Turn, app_id: str, lake: Lake) -> dict[str, Any]:
    app = turn.context.get("app")
    if not isinstance(app, dict) or app.get("id") != app_id:
        app, problem = optional_read(lake, "get_app", {"app_id": app_id})
        if problem:
            return {"problem": f"The App could not be read ({problem})."}
    repair = turn.context.get("repair") if isinstance(turn.context.get("repair"), dict) else {}
    events, events_problem = optional_read(
        lake, "receive_app_events", {"app_id": app_id, "from_sequence": 1, "wait_ms": 0}
    )
    logs, logs_problem = optional_read(lake, "app_logs", {"app_id": app_id, "limit": MAX_LOG_LINES})
    log_lines = [
        str(item.get("line") if isinstance(item, dict) else item)
        for item in (logs.get("items") or logs.get("logs") or logs.get("lines") or [])
    ][-MAX_LOG_LINES:]
    if logs_problem:
        logs_note = f"Runtime logs are unavailable here ({logs_problem})."
    elif not log_lines:
        logs_note = "Lake reported no runtime log lines for this App."
    else:
        logs_note = ""
    build, build_lines = newest_build(app, lake)
    source = app_source(app, repair)
    files, source_problem = read_source(source, lake)
    return {
        "app": app,
        "source": source,
        "events": (events.get("events") or [])[-MAX_EVENTS:],
        "events_note": f"Events are unavailable ({events_problem})." if events_problem else "",
        "logs": log_lines,
        "logs_note": logs_note,
        "build": build,
        "build_log": build_lines,
        "files": files,
        "source_note": source_problem,
    }


def newest_build(app: dict[str, Any], lake: Lake) -> tuple[dict[str, Any], list[str]]:
    listed, problem = optional_read(lake, "list_builds", {})
    if problem:
        return {}, []
    marker = f"/{app.get('name')}:"
    builds = [
        build
        for build in listed.get("builds") or listed.get("items") or []
        if isinstance(build, dict) and marker in str(build.get("target") or "")
    ]
    if not builds:
        return {}, []
    build = max(builds, key=lambda item: str(item.get("created_at") or ""))
    build_id = str(build.get("id") or build.get("build_id") or "")
    logs, _ = optional_read(lake, "build_logs", {"build_id": build_id}) if build_id else ({}, "")
    return build, [str(line) for line in logs.get("lines") or []][-MAX_BUILD_LOG_LINES:]


def read_source(source: dict[str, Any], lake: Lake) -> tuple[dict[str, str], str]:
    repository = str(source.get("repository") or "")
    if not repository:
        return {}, "Lake has no record of this App's source repository."
    revision = str(source.get("revision") or "main")
    root = source_root(source)
    tree, problem = optional_read(
        lake, "repository_tree", {"id": repository, "revision": revision, "recursive": True}
    )
    if problem:
        return {}, f"The App's source could not be listed ({problem})."
    files: dict[str, str] = {}
    total = 0
    for entry in tree.get("items") or tree.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if str(entry.get("kind") or "file") in {"directory", "dir", "tree", "folder"}:
            continue
        if root and not path.startswith(root + "/"):
            continue
        relative = path[len(root) + 1 :] if root else path
        if not relative or relative.endswith(".lake-folder") or len(files) >= MAX_FILES:
            continue
        if int(entry.get("size") or 0) > MAX_FILE_BYTES:
            continue
        value, problem = optional_read(
            lake, "repository_file", {"id": repository, "revision": revision, "path": path}
        )
        contents = value.get("contents")
        if problem or not isinstance(contents, str):
            continue
        total += len(contents.encode())
        if total > MAX_SOURCE_BYTES:
            break
        files[relative] = contents
    return files, "" if files else "The App's source folder holds no readable files."


def facts_message(turn: Turn, kind: str, facts: dict[str, Any]) -> str:
    app = facts["app"]
    shown_app = {
        key: app.get(key)
        for key in (
            "id",
            "name",
            "mode",
            "framework",
            "port",
            "auth",
            "bindings",
            "phase",
            "ready",
            "message",
            "generation",
            "observed_generation",
        )
    }
    shown_app["env_keys"] = sorted((app.get("env") or {}).keys())
    shown_app["secret_env_keys"] = sorted((app.get("secret_env_refs") or {}).keys())
    sections = [
        f"Turn: {kind}",
        f"The person's message:\n{turn.message}",
        "App:\n" + json.dumps(shown_app, indent=2, default=str),
        "Recent events:\n" + json.dumps(facts["events"], default=str)
        if facts["events"]
        else (facts["events_note"] or "No events."),
        "Runtime logs:\n" + "\n".join(facts["logs"]) if facts["logs"] else facts["logs_note"],
    ]
    if facts["build"]:
        sections.append(
            "Newest build:\n"
            + json.dumps(
                {key: facts["build"].get(key) for key in ("id", "phase", "message", "target")},
                default=str,
            )
            + ("\nBuild log:\n" + "\n".join(facts["build_log"]) if facts["build_log"] else "")
        )
    if facts["source_note"]:
        sections.append(facts["source_note"])
    for path, contents in facts["files"].items():
        sections.append(f"File {path}:\n{contents}")
    return "\n\n".join(sections)


def propose(
    turn: Turn,
    kind: str,
    facts: dict[str, Any],
    history: list[dict[str, Any]],
    client: OpenAI,
    model: str,
    lake: Lake,
) -> dict[str, Any]:
    if not history:
        history.append(
            {"role": "system", "content": (PROMPTS / f"{REPAIR_PROMPT_VERSION}.md").read_text()}
        )
        history.append({"role": "user", "content": facts_message(turn, kind, facts)})
    else:
        # A later instruction in the same conversation: the files as they are now.
        history.append({"role": "user", "content": facts_message(turn, kind, facts)})
    current: dict[str, str] = facts["files"]
    answer: dict[str, Any] = {}
    problems: list[str] = []
    for attempt in range(MAX_REPAIRS + 1):
        answer = model_answer(history, client, model, lake)
        problems = change_defects(answer, current, (turn.lake_token,))
        if not problems:
            break
        if attempt < MAX_REPAIRS:
            history.append(
                {
                    "role": "user",
                    "content": "Fix every validation failure and answer again with the complete "
                    "JSON object:\n" + "\n".join(f"- {problem}" for problem in problems),
                }
            )
    if problems:
        return question("I could not produce a safe change: " + "; ".join(problems))
    reply = str(answer.get("reply") or answer.get("question") or "")
    if answer.get("kind") == "question":
        return question(reply)
    changed = [
        {"path": item["path"], "contents": item["contents"]} for item in answer.get("files") or []
    ]
    deleted = [str(path) for path in answer.get("delete") or []]
    app = facts["app"]
    source = facts["source"]
    result = {
        "kind": "proposal",
        "confirm": True,
        "turn": kind,
        "diagnosis": reply,
        "diff": unified_diff(current, changed, deleted),
        "files": changed,
        "delete": deleted,
        "app": {
            "id": app.get("id"),
            "name": app.get("name"),
            "framework": app.get("framework"),
            "mode": app.get("mode"),
            "port": app.get("port"),
            "auth": app.get("auth"),
            "path": source.get("path") or ".",
            "repository": {"id": source.get("repository")},
            "source": source,
        },
    }
    return {"reply": reply or "Review the change before it is applied.", "result": result}


def change_defects(
    answer: dict[str, Any], current: dict[str, str], secret_values: tuple[str, ...]
) -> list[str]:
    kind = answer.get("kind")
    if kind == "question":
        text = answer.get("reply") or answer.get("question")
        return [] if isinstance(text, str) and text.strip() else ["question is empty"]
    if kind != "proposal":
        return ["kind must be question or proposal"]
    files = answer.get("files")
    deleted = answer.get("delete") or []
    if not isinstance(files, list) or not isinstance(deleted, list):
        return ["files and delete must be arrays"]
    if not files and not deleted:
        return ["a change needs at least one changed file"]
    if not isinstance(answer.get("reply"), str) or not answer["reply"].strip():
        return ["reply must say what was found and what the change does"]
    return file_change_defects(files, deleted, current, secret_values)


def file_change_defects(
    files: list[Any],
    deleted: list[Any],
    current: dict[str, str],
    secret_values: tuple[str, ...],
) -> list[str]:
    problems: list[str] = []
    paths: set[str] = set()
    if len(files) > MAX_FILES:
        problems.append(f"a change may touch at most {MAX_FILES} files")
    unchanged = 0
    for item in files:
        if not isinstance(item, dict):
            problems.append("every file must be an object")
            continue
        path, contents = item.get("path"), item.get("contents")
        if not isinstance(path, str) or not safe_path(path):
            problems.append(f"invalid App path: {path!r}")
            continue
        if path in paths:
            problems.append(f"duplicate App path: {path}")
        paths.add(path)
        if not isinstance(contents, str):
            problems.append(f"{path} contents must be text")
            continue
        if current.get(path) == contents:
            unchanged += 1
        if SECRET_LITERAL.search(contents):
            problems.append(f"{path} contains what looks like a secret")
        for secret in secret_values:
            if len(secret) >= 6 and secret in contents:
                problems.append(f"{path} contains a delegated credential")
        if path.endswith(".py"):
            try:
                ast.parse(contents, filename=path)
            except SyntaxError as error:
                problems.append(f"{path} does not compile: {error.msg} at line {error.lineno}")
        if path == "app.toml":
            problems.extend(manifest_change_defects(contents, current.get("app.toml")))
    for path in deleted:
        if not isinstance(path, str) or not safe_path(path) or path in paths:
            problems.append(f"invalid deleted path: {path!r}")
        elif path == "app.toml":
            problems.append("app.toml cannot be deleted")
    if files and unchanged == len(files) and not deleted:
        problems.append("the change leaves every file as it is")
    return problems


def manifest_change_defects(contents: str, before: str | None) -> list[str]:
    try:
        values = tomllib.loads(contents)
    except tomllib.TOMLDecodeError as error:
        return [f"app.toml is invalid: {error}"]
    problems = []
    if before:
        try:
            previous = tomllib.loads(before)
        except tomllib.TOMLDecodeError:
            previous = {}
        for key in ("name", "framework", "port"):
            if key in previous and values.get(key) != previous.get(key):
                problems.append(f"app.toml {key} must stay {previous.get(key)!r}")
    requirements = dependency_requirements(values.get("dependencies", []))
    if any("@" in requirement and "://" in requirement for requirement in requirements):
        problems.append("app.toml dependencies cannot contain credentials")
    return problems


def unified_diff(current: dict[str, str], files: list[dict[str, str]], deleted: list[str]) -> str:
    parts: list[str] = []
    for item in files:
        path = item["path"]
        before = current.get(path)
        parts.extend(
            difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                item["contents"].splitlines(keepends=True),
                fromfile=f"a/{path}" if before is not None else "/dev/null",
                tofile=f"b/{path}",
            )
        )
    for path in deleted:
        parts.extend(
            difflib.unified_diff(
                current.get(path, "").splitlines(keepends=True),
                [],
                fromfile=f"a/{path}",
                tofile="/dev/null",
            )
        )
    text = "".join(line if line.endswith("\n") else line + "\n" for line in parts)
    return text[:MAX_DIFF_CHARS]


def manifest_of(
    files: list[dict[str, str]], source: dict[str, Any], lake: Lake
) -> str:
    for item in files:
        if item["path"] == "app.toml":
            return item["contents"]
    root = source_root(source)
    value, problem = optional_read(
        lake,
        "repository_file",
        {
            "id": source.get("repository"),
            "revision": source.get("revision") or "main",
            "path": f"{root}/app.toml" if root else "app.toml",
        },
    )
    contents = value.get("contents")
    return contents if not problem and isinstance(contents, str) else ""


def outside_dependencies(manifest: str) -> list[str]:
    try:
        dependencies = tomllib.loads(manifest).get("dependencies", []) if manifest else []
    except tomllib.TOMLDecodeError:
        return []
    return [
        requirement
        for requirement in dependency_requirements(dependencies)
        if (names := dependency_names([requirement])) and names.isdisjoint(BAKED_DEPENDENCIES)
    ]


def apply_proposal(
    turn: Turn,
    kind: str,
    app_id: str,
    confirmed: dict[str, Any],
    lake: Lake,
    automatic: bool,
) -> dict[str, Any]:
    proposal = confirmed.get("result") if isinstance(confirmed.get("result"), dict) else confirmed
    app = proposal.get("app") if isinstance(proposal.get("app"), dict) else {}
    files = proposal.get("files") if isinstance(proposal.get("files"), list) else []
    deleted = proposal.get("delete") if isinstance(proposal.get("delete"), list) else []
    if app.get("id") != app_id:
        return question("The confirmed change is for another App and was not applied.")
    problems = file_change_defects(files, deleted, {}, (turn.lake_token,))
    problems = [problem for problem in problems if "leaves every file" not in problem]
    source = app.get("source") if isinstance(app.get("source"), dict) else {}
    repository = str((app.get("repository") or {}).get("id") or source.get("repository") or "")
    if not repository:
        problems.append("the App's source repository is unknown")
    if not files and not deleted:
        problems.append("the change has no files")
    if problems:
        return question("The change was not applied: " + "; ".join(problems))
    root = source_root(source) or source_root({"path": app.get("path")})
    operations = []
    for item in files:
        path = f"{root}/{item['path']}" if root else item["path"]
        operations.append(
            {
                "type": "file",
                "path": path,
                "content": base64_text(item["contents"]),
            }
        )
    for path in deleted:
        operations.append({"type": "deletedFile", "path": f"{root}/{path}" if root else path})
    headline = first_line(str(proposal.get("diagnosis") or turn.message))
    verb = "Repair" if kind == "repair" else "Change"
    message = f"{verb} the {app.get('name')} App: {headline}"
    if automatic:
        message = f"Auto-fix the {app.get('name')} App: {headline}"
    # One commit: each file it writes is a liquid drip, signed and attributed to the person whose
    # turn this is, so every repair can be seen and reverted.
    committed = lake.write(
        "commit_repository_files",
        {"id": repository, "revision": "main", "message": message, "operations": operations},
    )
    revision = str(committed.get("commitOid") or committed.get("revision") or "")
    if not revision:
        raise LakeError("commit_repository_files", "invalid_response", "revision is missing")
    new_source = {"repository": repository, "revision": revision, "path": source.get("path") or "."}
    if source.get("entrypoint"):
        new_source["entrypoint"] = source["entrypoint"]
    outside = outside_dependencies(manifest_of(files, new_source, lake))
    deployed_app: dict[str, Any] = {**app, "revision": revision}
    if outside:
        lake.write(
            "set_app_env",
            {"customer_id": turn.customer_id, "app_id": app_id, "key": "APP_DEPS",
             "value": " ".join(outside)},
        )
        queued = lake.write(
            "build_app", {"customer_id": turn.customer_id, "app": app_id, "source": new_source}
        )
        build_id = str(queued.get("build_id") or queued.get("id") or "")
        if not build_id:
            raise LakeError("build_app", "invalid_response", "build id is missing")
        build = {**queued, **wait_for_build(turn.customer_id, build_id, lake), "id": build_id}
        if build_status(build) != "succeeded":
            return {
                "reply": f"The change was committed ({revision}) but its image did not build. "
                + build_failure_explanation(turn.customer_id, build, lake),
                "result": result(kind, "question", files, proposal, deployed_app, revision),
            }
        deployment: dict[str, Any] = {"image": build_image_reference(build)}
    else:
        deployment = {"source": new_source}
    deployed = lake.write(
        "deploy_app", {"customer_id": turn.customer_id, "app": app_id, **deployment}
    )
    deployed_app.update(deployed)
    deployed_app["id"] = app_id
    rollout = follow_rollout(turn.customer_id, deployed_app, lake)
    deployed_app["rollout"] = rollout
    failed = rollout_state(rollout) in ROLLOUT_FAILED or rollout.get("timed_out")
    if failed:
        reply = (
            f"The change was committed ({revision}) and deployed, but the App still fails. "
            + failure_explanation(deployed_app, rollout, lake)
        )
    else:
        url = deployed_app.get("url") or rollout.get("url") or ""
        reply = f"The change was committed ({revision}) and the App is running again."
        if kind == "change":
            reply = f"The change was committed ({revision}) and deployed."
        if url:
            reply += f" {url}"
    return {
        "reply": reply,
        "result": result(kind, "deployed", files, proposal, deployed_app, revision),
    }


def result(
    kind: str,
    shape: str,
    files: list[dict[str, str]],
    proposal: dict[str, Any],
    app: dict[str, Any],
    revision: str,
) -> dict[str, Any]:
    return {
        "kind": shape,
        "turn": kind,
        "files": files,
        "diff": proposal.get("diff", ""),
        "app": app,
        "revision": revision,
        "confirm": False,
    }


def first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:120] if line else "requested change"


def base64_text(text: str) -> str:
    return base64.b64encode(text.encode()).decode()
