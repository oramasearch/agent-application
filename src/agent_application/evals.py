"""Live model evaluations for the three required ApplicationAgent scenarios."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from openai import OpenAI

from agent_application.agent import Turn, answer_turn, proposal_defects


@dataclass(frozen=True)
class Case:
    id: str
    message: str
    framework: str
    binding: str
    source_markers: tuple[str, ...]


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
)


class FakeLake:
    """Workspace facts for reproducible design evals; mutations are forbidden."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.reads.append(capability)
        values = {
            "list_datasets": {
                "datasets": [
                    {
                        "id": "customer_success/accounts",
                        "name": "accounts",
                    }
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
    return reasons


def run() -> list[dict[str, Any]]:
    client = OpenAI()
    model = os.environ.get("APPLICATION_AGENT_MODEL", "gpt-5.5")
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
