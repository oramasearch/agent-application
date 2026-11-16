# SPDX-License-Identifier: MIT

from __future__ import annotations

from agent_application.evals import CASES, FakeLake, grade


def test_eval_matrix_covers_dashboard_crud_static_site_and_filters() -> None:
    assert [case.id for case in CASES] == [
        "churn-dashboard",
        "postgres-crud",
        "static-guide",
        "filtered-dashboard",
    ]
    assert {case.framework for case in CASES} == {"streamlit", "fastapi", "static"}
    assert [case.id for case in CASES if case.filters] == ["filtered-dashboard"]


def test_eval_workspace_answers_customers_sql_and_refuses_in() -> None:
    import pytest

    from agent_application.agent import LakeError

    lake = FakeLake()

    schema = lake.read("get_dataset", {"id": "datasets/customers"})
    grouped = lake.read(
        "workspace_sql",
        {"statement": "SELECT country, COUNT(*) AS n FROM datasets.customers GROUP BY country"},
    )

    assert {column["name"] for column in schema["columns"]} >= {"country", "segment", "signup_date"}
    assert sum(row["n"] for row in grouped["rows"]) == 400
    with pytest.raises(LakeError, match="IN"):
        lake.read(
            "workspace_sql",
            {"statement": "SELECT COUNT(*) FROM datasets.customers WHERE country IN ('Japan')"},
        )


def test_eval_workspace_has_dataset_schema_sql_and_postgres_service() -> None:
    lake = FakeLake()

    dataset = lake.read("get_dataset", {"id": "customer_success/accounts"})
    sql = lake.read("workspace_sql", {"statement": "SELECT plan"})
    services = lake.read("list_services", {})

    assert {column["name"] for column in dataset["columns"]} >= {"plan", "churn_risk"}
    assert sql["rows"]
    assert services["services"][0]["kind"] == "postgres"
    assert lake.reads == ["get_dataset", "workspace_sql", "list_services"]


def test_grader_rejects_a_question_for_a_complete_eval_request() -> None:
    reasons = grade(
        CASES[0],
        {
            "reply": "Which dataset?",
            "result": {"kind": "question", "files": [], "app": {}},
        },
    )

    assert reasons == ["did not return a proposal"]
