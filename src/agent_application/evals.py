# SPDX-License-Identifier: MIT

"""Live model evaluations for the ApplicationAgent scenarios.

The `filtered-dashboard` case also runs the proposed Streamlit App against a SQLite copy of its dataset and chooses
each sidebar filter (`app_filters.sidebar_filter_defects`, agent-application#64).
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from openai import OpenAI

from agent_application.agent import LakeError, Turn, answer_turn, proposal_defects
from agent_application.app_filters import SqliteWorkspace, sidebar_filter_defects
from agent_application.service import model_from_env


@dataclass(frozen=True)
class Case:
    id: str
    message: str
    framework: str
    binding: str
    source_markers: tuple[str, ...]
    filters: bool = False


def customer_rows(count: int = 400) -> list[dict[str, Any]]:
    """The `datasets.customers` rows of the filtered-dashboard case: the same every run."""
    generator = random.Random(64)
    countries = ["United States", "Canada", "Germany", "France", "Brazil", "Japan", "India", "Australia"]
    segments = ["Enterprise", "Mid-Market", "SMB", "Consumer"]
    rows = []
    for index in range(count):
        rows.append(
            {
                "customer_id": f"C{index:04d}",
                "name": f"Customer {index}",
                "email": f"customer{index}@example.com",
                "country": generator.choice(countries),
                "segment": generator.choice(segments),
                "plan": generator.choice(["free", "starter", "pro", "enterprise"]),
                "signup_date": f"20{generator.randint(22, 26)}-{generator.randint(1, 12):02d}-"
                f"{generator.randint(1, 28):02d}",
                "lifetime_value": round(generator.uniform(50, 25_000), 2),
                "monthly_spend": round(generator.uniform(0, 2_000), 2),
                "orders": generator.randint(0, 120),
                "churned": generator.random() < 0.25,
                "nps": generator.randint(0, 10),
            }
        )
    return rows


CUSTOMERS = customer_rows()
CUSTOMER_COLUMNS = [
    {"name": "customer_id", "type": "string"},
    {"name": "name", "type": "string"},
    {"name": "email", "type": "string"},
    {"name": "country", "type": "string"},
    {"name": "segment", "type": "string"},
    {"name": "plan", "type": "string"},
    {"name": "signup_date", "type": "date"},
    {"name": "lifetime_value", "type": "float"},
    {"name": "monthly_spend", "type": "float"},
    {"name": "orders", "type": "int"},
    {"name": "churned", "type": "bool"},
    {"name": "nps", "type": "int"},
]


def customers_workspace() -> SqliteWorkspace:
    return SqliteWorkspace({"datasets.customers": CUSTOMERS})


CASES = (
    Case(
        "churn-dashboard",
        (
            "Build a Streamlit dashboard of average churn_risk by plan from the "
            "customer_success.accounts dataset. Propose it now."
        ),
        "streamlit",
        "zetta",
        ("zetta", "sql", "churn_risk", "plan"),
    ),
    Case(
        "postgres-crud",
        (
            "Build an internal FastAPI CRUD tool for account notes in the "
            "account-notes-postgres Service. Propose it now."
        ),
        "fastapi",
        "service",
        ("service(", "@app.", "note"),
    ),
    Case(
        "static-guide",
        (
            "Build an internal static site that explains how to read the weekly forecast. "
            "Propose it now."
        ),
        "static",
        "none",
        ("<", "forecast"),
    ),
    Case(
        "filtered-dashboard",
        (
            "Build a customer insights dashboard in Streamlit over the datasets.customers dataset. Put filters "
            "in the sidebar for country (a multiselect), segment, and a signup date range, and show key "
            "metrics, charts by segment and country, and a table of customers. Propose it now."
        ),
        "streamlit",
        "zetta",
        ("zetta", "sql", "country", "segment", "signup_date"),
        filters=True,
    ),
)


class FakeLake:
    """Workspace facts for reproducible design evals; mutations are forbidden."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.reads.append(capability)
        named = json.dumps(arguments).lower()
        if capability == "get_dataset" and "customers" in named:
            return {"id": "datasets/customers", "rows": len(CUSTOMERS), "columns": CUSTOMER_COLUMNS}
        if capability == "workspace_sql" and "customers" in named:
            statement = str(arguments.get("statement") or arguments.get("sql") or "")
            try:
                return customers_workspace().sql(statement)
            except Exception as error:
                raise LakeError("workspace_sql", "engine_error", str(error)) from error
        values = {
            "list_datasets": {
                "datasets": [
                    {
                        "id": "customer_success/accounts",
                        "name": "accounts",
                    },
                    {"id": "datasets/customers", "name": "customers"},
                ]
            },
            "get_dataset": {
                "id": "customer_success/accounts",
                "columns": [
                    {"name": "account_id", "type": "string"},
                    {"name": "plan", "type": "string"},
                    {"name": "churn_risk", "type": "float"},
                ],
            },
            "workspace_sql": {
                "columns": ["plan", "churn_risk"],
                "rows": [{"plan": "pro", "churn_risk": 0.42}],
            },
            "list_services": {
                "services": [
                    {
                        "id": "account-notes-postgres",
                        "name": "account-notes-postgres",
                        "kind": "postgres",
                    }
                ]
            },
            "list_apps": {"apps": []},
        }
        return values.get(capability, {})

    def write(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"eval attempted unconfirmed write {capability}: {arguments}")


def source_text(result: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get("contents") or "")
        for item in result.get("files") or []
        if isinstance(item, dict)
    )


def grade(case: Case, answer: dict[str, Any]) -> list[str]:
    result = answer.get("result") or {}
    app = result.get("app") or {}
    reasons: list[str] = []
    if result.get("kind") != "proposal":
        return ["did not return a proposal"]
    candidate = {
        "kind": "proposal",
        "files": result.get("files"),
        "app": app,
    }
    reasons.extend(proposal_defects(candidate, case.message))
    if app.get("framework") != case.framework:
        reasons.append(f"framework is not {case.framework}")
    bindings = app.get("bindings") or {}
    if case.binding == "zetta" and not bindings.get("zetta"):
        reasons.append("missing Zetta binding")
    if case.binding == "service" and not bindings.get("services"):
        reasons.append("missing Service binding")
    if case.binding == "none" and (
        bindings.get("zetta") or bindings.get("services")
    ):
        reasons.append("static site requested unnecessary bindings")
    source = source_text(result).lower()
    for marker in case.source_markers:
        if marker.lower() not in source:
            reasons.append(f"source is missing {marker!r}")
    if case.filters and not reasons:
        reasons.extend(filter_defects(result))
    return reasons


def filter_defects(result: dict[str, Any]) -> list[str]:
    """Run the proposed Streamlit App over `datasets.customers` and choose each sidebar filter."""
    files = {
        str(item.get("path")): str(item.get("contents") or "")
        for item in result.get("files") or []
        if isinstance(item, dict)
    }
    try:
        entrypoint = tomllib.loads(files.get("app.toml", ""))["entrypoint"]
    except (tomllib.TOMLDecodeError, KeyError):
        return ["app.toml names no entrypoint"]
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for path, contents in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        sys.path.insert(0, str(root))
        try:
            return sidebar_filter_defects(root / entrypoint, customers_workspace)
        finally:
            sys.path.remove(str(root))


def run() -> list[dict[str, Any]]:
    client = OpenAI()
    model = model_from_env()
    rows = []
    for case in CASES:
        lake = FakeLake()
        turn = Turn(
            message=case.message,
            lake_url="https://fake-lake.invalid",
            lake_token="eval-person-token",
            customer_id="eval-customer",
        )
        answer = answer_turn(turn, [], client, model, lake)
        reasons = grade(case, answer)
        required_reads = {
            "churn-dashboard": {"list_datasets", "get_dataset", "workspace_sql"},
            "postgres-crud": {"list_services"},
            "static-guide": set(),
            "filtered-dashboard": {"list_datasets", "get_dataset", "workspace_sql"},
        }[case.id]
        missing_reads = sorted(required_reads - set(lake.reads))
        if missing_reads:
            reasons.append("missing lake reads: " + ", ".join(missing_reads))
        rows.append(
            {
                "case": case.id,
                "passed": not reasons,
                "reasons": reasons,
                "answer": answer,
            }
        )
    return rows


def write_report(rows: list[dict[str, Any]]) -> Path:
    reports = Path(__file__).resolve().parents[2] / "reports"
    reports.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    json_path = reports / f"{stamp}-evals.json"
    json_path.write_text(json.dumps(rows, indent=1, default=str) + "\n")
    passed = sum(bool(row["passed"]) for row in rows)
    lines = [
        f"# ApplicationAgent evals, {stamp}",
        "",
        f"**{passed} / {len(rows)} passed.**",
        "",
        "| Case | Result |",
        "|---|---|",
    ]
    for row in rows:
        result = "pass" if row["passed"] else "FAIL: " + "; ".join(row["reasons"])
        lines.append(f"| `{row['case']}` | {result} |")
    markdown_path = reports / f"{stamp}-evals.md"
    markdown_path.write_text("\n".join(lines) + "\n")
    return markdown_path


def main() -> int:
    rows = run()
    path = write_report(rows)
    passed = sum(bool(row["passed"]) for row in rows)
    print(f"{passed}/{len(rows)} passed; {path}")
    return 0 if passed == len(rows) else 2


if __name__ == "__main__":
    sys.exit(main())
