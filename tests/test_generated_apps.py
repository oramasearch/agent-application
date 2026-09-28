from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from app_runtime import DependencyError, load_app
from app_runtime.frameworks import build_application

from agent_application.agent import Turn, answer_turn


class Message:
    def __init__(self, value: dict) -> None:
        self.content = json.dumps(value)
        self.tool_calls = []

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class Scripted:
    def __init__(self, value: dict) -> None:
        self.value = value
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_arguments):
        return SimpleNamespace(choices=[SimpleNamespace(message=Message(self.value))])


class ReadOnlyLake:
    def __init__(self) -> None:
        self.writes = []

    def read(self, _capability, _arguments):
        return {}

    def write(self, capability, arguments):
        self.writes.append((capability, arguments))
        raise AssertionError("an unconfirmed generated App cannot write")


def generate(value: dict) -> dict:
    lake = ReadOnlyLake()
    turn = Turn(
        message="build this complete App now",
        lake_url="https://lake.test",
        lake_token="person-token",
        customer_id="customer-1",
    )
    answer = answer_turn(turn, [], Scripted(value), "test-model", lake)
    assert answer["result"]["kind"] == "proposal"
    assert lake.writes == []
    return answer["result"]


def load_generated(result: dict):
    root = result["app"]["path"].strip("/")
    repository_files = {
        f"{root}/{item['path']}": item["contents"] for item in result["files"]
    }

    def fake_lake(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        assert request.headers["authorization"] == "Bearer app-token"
        if request.url.path == "/v1/repository_tree":
            items = [{"path": path, "kind": "file"} for path in repository_files]
            return httpx.Response(200, json={"ok": True, "value": {"items": items}})
        path = query["path"][0]
        value = {"contents": repository_files[path]}
        return httpx.Response(200, json={"ok": True, "value": value})

    return load_app(
        {
            "APP_SOURCE": (
                f"https://lake.test|repository-id|commit-sha|{result['app']['path']}"
            ),
            "APP_TOKEN": "app-token",
        },
        transport=httpx.MockTransport(fake_lake),
    )


def dashboard_proposal() -> dict:
    manifest = """\
name = "churn-dashboard"
framework = "streamlit"
entrypoint = "dashboard.py"
port = 8080
dependencies = ["streamlit", "pandas", "plotly"]

[bindings]
zetta = true
services = []
"""
    source = '''\
import pandas as pd
import plotly.express as px
import streamlit as st
from orama_app import zetta


QUERY = """SELECT plan, AVG(churn_risk) AS churn_risk
FROM \"customer_success\".\"accounts\"
GROUP BY plan
ORDER BY churn_risk DESC"""


def load_rows(workspace=None):
    owned = workspace is None
    workspace = workspace or zetta()
    try:
        return workspace.sql(QUERY)["rows"]
    finally:
        if owned:
            workspace.close()


def main():
    st.title("Churn risk by plan")
    rows = load_rows()
    frame = pd.DataFrame(rows)
    st.plotly_chart(px.bar(frame, x="plan", y="churn_risk"))


if __name__ == "__main__":
    main()
'''
    return {
        "kind": "proposal",
        "reply": "A lake-authenticated churn dashboard over the read-only Zetta binding.",
        "files": [
            {"path": "app.toml", "contents": manifest},
            {"path": "dashboard.py", "contents": source},
            {
                "path": "README.md",
                "contents": "# Churn dashboard\n\nReads grouped churn risk from Zetta.\n",
            },
        ],
        "app": {
            "name": "churn-dashboard",
            "mode": "runtime",
            "repository": {"name": "app-churn-dashboard"},
            "path": "apps/churn-dashboard",
            "framework": "streamlit",
            "port": 8080,
            "auth": "lake",
            "bindings": {"zetta": True, "services": []},
            "env": {},
        },
    }


def crud_proposal() -> dict:
    manifest = """\
name = "account-notes"
framework = "fastapi"
entrypoint = "main:app"
port = 8080
dependencies = ["fastapi", "pydantic", "psycopg[binary]"]

[bindings]
zetta = false
services = ["account-notes-postgres"]
"""
    source = '''\
from fastapi import FastAPI
from orama_app import current_user, service
from pydantic import BaseModel
import psycopg


app = FastAPI()


class Note(BaseModel):
    account_id: str
    body: str


@app.post("/notes")
def create_note(note: Note):
    user = current_user() or {}
    connection = service("account-notes-postgres")["url"]
    with psycopg.connect(connection) as database:
        row = database.execute(
            "INSERT INTO notes(account_id, body, author_id) VALUES (%s, %s, %s) RETURNING id",
            (note.account_id, note.body, user.get("id")),
        ).fetchone()
    return {"id": row[0]}
'''
    return {
        "kind": "proposal",
        "reply": "A lake-authenticated CRUD tool in image mode for its Postgres driver.",
        "files": [
            {"path": "app.toml", "contents": manifest},
            {"path": "main.py", "contents": source},
            {
                "path": "README.md",
                "contents": "# Account notes\n\nWrites through the bound Postgres Service.\n",
            },
        ],
        "app": {
            "name": "account-notes",
            "mode": "image",
            "repository": {"name": "app-account-notes"},
            "path": "apps/account-notes",
            "framework": "fastapi",
            "port": 8080,
            "auth": "lake",
            "bindings": {"zetta": False, "services": ["account-notes-postgres"]},
            "env": {},
        },
    }


def static_proposal() -> dict:
    manifest = """\
name = "forecast-guide"
framework = "static"
entrypoint = "public"
port = 8080
dependencies = []

[bindings]
zetta = false
services = []
"""
    return {
        "kind": "proposal",
        "reply": "A lake-authenticated static forecast guide.",
        "files": [
            {"path": "app.toml", "contents": manifest},
            {
                "path": "public/index.html",
                "contents": "<!doctype html><title>Forecast guide</title><h1>Forecast guide</h1>",
            },
            {
                "path": "README.md",
                "contents": "# Forecast guide\n\nA static internal guide.\n",
            },
        ],
        "app": {
            "name": "forecast-guide",
            "mode": "runtime",
            "repository": {"name": "app-forecast-guide"},
            "path": "apps/forecast-guide",
            "framework": "static",
            "port": 8080,
            "auth": "lake",
            "bindings": {"zetta": False, "services": []},
            "env": {},
        },
    }


def test_generated_dashboard_loads_and_queries_fake_zetta() -> None:
    result = generate(dashboard_proposal())
    loaded = load_generated(result)

    class FakeZetta:
        def __init__(self) -> None:
            self.statements = []

        def sql(self, statement):
            self.statements.append(statement)
            return {"rows": [{"plan": "pro", "churn_risk": 0.42}]}

    workspace = FakeZetta()
    module_name = "generated_churn_dashboard"
    spec = importlib.util.spec_from_file_location(module_name, loaded.root / "dashboard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        rows = module.load_rows(workspace)
        assert rows == [{"plan": "pro", "churn_risk": 0.42}]
        assert '"customer_success"."accounts"' in workspace.statements[0]
    finally:
        sys.modules.pop(module_name, None)
        shutil.rmtree(loaded.root)


def test_generated_postgres_crud_app_selects_image_mode() -> None:
    result = generate(crud_proposal())

    assert result["app"]["mode"] == "image"
    with pytest.raises(DependencyError, match="Use App image mode"):
        load_generated(result)


@pytest.mark.anyio
async def test_generated_static_site_loads_and_serves() -> None:
    result = generate(static_proposal())
    loaded = load_generated(result)
    try:
        application = build_application(loaded.root, loaded.config)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://runtime",
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert "Forecast guide" in (await client.get("/")).text
    finally:
        shutil.rmtree(loaded.root)

