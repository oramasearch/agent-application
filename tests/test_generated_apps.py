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



# Sidebar filters reach the whole page (agent-application#64). The App is run under Streamlit's AppTest over a SQLite
# copy of `datasets.customers`, and each sidebar filter is chosen in turn.

FILTERED_MANIFEST = """\
name = "customer-insights"
framework = "streamlit"
entrypoint = "app.py"
port = 8080
dependencies = ["streamlit", "pandas", "plotly"]

[bindings]
zetta = true
services = []
"""

# Filters in SQL: one WHERE clause that every query shares.
FILTERED_SQL_APP = '''\
import pandas as pd
import plotly.express as px
import streamlit as st
from orama_app import zetta

TABLE = "datasets.customers"


def frame(workspace, statement):
    return pd.DataFrame(workspace.sql(statement)["rows"])


def quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


with zetta() as workspace:
    countries = frame(workspace, f"SELECT country FROM {TABLE} GROUP BY country ORDER BY country LIMIT 500")
    segments = frame(workspace, f"SELECT segment FROM {TABLE} GROUP BY segment ORDER BY segment LIMIT 500")
    span = frame(workspace, f"SELECT MIN(signup_date) AS first, MAX(signup_date) AS last FROM {TABLE}").iloc[0]
    with st.sidebar:
        chosen = st.multiselect("Country", countries["country"].tolist())
        segment = st.selectbox("Segment", ["All"] + segments["segment"].tolist())
        dates = st.date_input(
            "Signup date range",
            value=(pd.to_datetime(span["first"]).date(), pd.to_datetime(span["last"]).date()),
        )
    clauses = []
    if chosen:
        clauses.append("(" + " OR ".join("country = " + quoted(c) for c in chosen) + ")")
    if segment != "All":
        clauses.append("segment = " + quoted(segment))
    if len(dates) == 2:
        clauses.append(f"signup_date >= {quoted(dates[0].isoformat())} AND signup_date <= {quoted(dates[1].isoformat())}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    summary = frame(workspace, f"SELECT COUNT(*) AS customers, SUM(lifetime_value) AS value FROM {TABLE}{where}").iloc[0]
    st.metric("Customers", f"{int(summary['customers']):,}")
    st.metric("Lifetime value", f"{float(summary['value'] or 0):,.0f}")
    by_segment = frame(workspace, f"SELECT segment, COUNT(*) AS customers FROM {TABLE}{where} GROUP BY segment LIMIT 500")
    st.plotly_chart(px.bar(by_segment, x="segment", y="customers"))
    by_country = frame(workspace, f"SELECT country, COUNT(*) AS customers FROM {TABLE}{BY_COUNTRY_WHERE} GROUP BY country LIMIT 500")
    st.plotly_chart(px.bar(by_country, x="country", y="customers"))
    st.dataframe(frame(workspace, f"SELECT customer_id, name, country, segment FROM {TABLE}{where} ORDER BY customer_id LIMIT 50"))
'''

# Filters in pandas: one filtered frame that everything is drawn from.
FILTERED_FRAME_APP = '''\
import pandas as pd
import plotly.express as px
import streamlit as st
from orama_app import zetta


@st.cache_data
def load():
    with zetta() as workspace:
        rows = workspace.sql("SELECT customer_id, country, segment, signup_date, lifetime_value FROM datasets.customers LIMIT 5000")
    frame = pd.DataFrame(rows["rows"])
    frame["signup_date"] = pd.to_datetime(frame["signup_date"])
    return frame


customers = load()
with st.sidebar:
    countries = st.multiselect("Country", sorted(customers["country"].unique()))
    segments = st.multiselect("Segment", sorted(customers["segment"].unique()))
    dates = st.date_input(
        "Signup date range", value=(customers["signup_date"].min().date(), customers["signup_date"].max().date())
    )
filtered = customers
if countries:
    filtered = filtered[filtered["country"].isin(countries)]
if segments:
    filtered = filtered[filtered["segment"].isin(segments)]
if len(dates) == 2:
    start, end = pd.to_datetime(dates[0]), pd.to_datetime(dates[1])
    filtered = filtered[(filtered["signup_date"] >= start) & (filtered["signup_date"] <= end)]
st.metric("Customers", f"{len(filtered):,}")
by_segment = filtered.groupby("segment").size().reset_index(name="customers")
st.plotly_chart(px.bar(by_segment, x="segment", y="customers"))
by_country = CHART_FRAME.groupby("country").size().reset_index(name="customers")
st.plotly_chart(px.bar(by_country, x="country", y="customers"))
st.dataframe(filtered.head(50))
'''


def filtered_proposal(source: str) -> dict:
    proposal = dashboard_proposal()
    proposal["files"] = [
        {"path": "app.toml", "contents": FILTERED_MANIFEST},
        {"path": "app.py", "contents": source},
        {"path": "README.md", "contents": "# Customer insights\n\nFilters apply to the whole page.\n"},
    ]
    proposal["app"] = {**proposal["app"], "name": "customer-insights", "path": "apps/customer-insights"}
    return proposal


def filter_check(source: str) -> list[str]:
    from agent_application.evals import filter_defects

    return filter_defects(generate(filtered_proposal(source)))


def test_generated_dashboard_applies_every_sidebar_filter_in_sql() -> None:
    assert filter_check(FILTERED_SQL_APP.replace("{BY_COUNTRY_WHERE}", "{where}")) == []


def test_generated_dashboard_applies_every_sidebar_filter_to_one_frame() -> None:
    assert filter_check(FILTERED_FRAME_APP.replace("CHART_FRAME", "filtered")) == []


def test_a_chart_that_ignores_the_sidebar_filters_is_reported() -> None:
    """The query behind one chart is built without the filters: every filter leaves that chart as it was."""
    problems = filter_check(FILTERED_SQL_APP.replace("{BY_COUNTRY_WHERE}", ""))

    assert len(problems) == 3
    for label in ("'Country'", "'Segment'", "'Signup date range'"):
        assert any(label in problem and "plotly chart 2" in problem for problem in problems)
    assert all("plotly chart 1" not in problem and "data frame" not in problem for problem in problems)


def test_a_chart_drawn_from_the_unfiltered_frame_is_reported() -> None:
    """agent-application#64's shape: filters applied to one frame, a chart drawn from the frame read before them."""
    problems = filter_check(FILTERED_FRAME_APP.replace("CHART_FRAME", "customers"))

    assert problems
    assert all("plotly chart 2" in problem for problem in problems)


def test_a_filter_written_with_in_fails_as_it_does_on_lake() -> None:
    """The workspace's SQL has no IN; an App that filters with it shows an error once a country is chosen."""
    source = FILTERED_SQL_APP.replace("{BY_COUNTRY_WHERE}", "{where}").replace(
        '"(" + " OR ".join("country = " + quoted(c) for c in chosen) + ")"',
        '"country IN (" + ", ".join(quoted(c) for c in chosen) + ")"',
    )

    problems = filter_check(source)

    assert any("'Country'" in problem and "IN" in problem for problem in problems)


def test_a_sidebar_widget_whose_value_is_never_used_fails_validation() -> None:
    """A filter read and never applied is refused before anyone sees the App, and the model is told why."""
    from agent_application.agent import proposal_defects

    source = FILTERED_SQL_APP.replace("{BY_COUNTRY_WHERE}", "{where}").replace(
        'segment = st.selectbox("Segment"', 'unused_segment = st.selectbox("Segment"'
    )

    problems = proposal_defects(filtered_proposal(source), "build it")

    assert any("'Segment' selectbox (`unused_segment`) is never used" in problem for problem in problems)
