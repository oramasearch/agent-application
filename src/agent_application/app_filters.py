"""Do a generated App's filters reach its page? (agent-application#64)

Two checks:

- `unused_widgets` reads the source: a widget whose value is assigned and never used is a filter that cannot apply.
  It runs on every proposal (`agent.file_defects`), so the model fixes it before anyone sees the App.
- `sidebar_filter_defects` runs a Streamlit App under Streamlit's `AppTest` against `SqliteWorkspace`, chooses each
  sidebar filter in turn, and reports every chart and table on the main page that did not change (and the metrics,
  when none changed). It needs Streamlit, which only the development environment has, so it runs in the tests and
  the evals, not in the service.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import sqlite3
import sys
import types
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

WIDGETS = {
    "checkbox",
    "date_input",
    "multiselect",
    "number_input",
    "radio",
    "select_slider",
    "selectbox",
    "slider",
    "text_input",
    "toggle",
}


def unused_widgets(path: str, tree: ast.AST) -> list[str]:
    """Widgets such as `countries = st.sidebar.multiselect("Country", ...)` whose value is never read.

    A widget with a `key` or an `on_change` may be read through `st.session_state` instead, so it is not reported.
    """
    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr in WIDGETS):
            continue
        if any(keyword.arg in {"key", "on_change"} for keyword in call.keywords):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [
            name.id
            for target in targets
            for name in ast.walk(target)
            if isinstance(name, ast.Name)
        ]
        unused = [name for name in names if name not in loaded]
        if not unused:
            continue
        label = ""
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            label = f" {call.args[0].value!r}"
        problems.append(
            f"{path} line {node.lineno}: the value of the{label} {call.func.attr} (`{', '.join(unused)}`) is never "
            "used; apply every filter to every metric, chart and table on the page"
        )
    return problems


class SqliteWorkspace:
    """A workspace client over in-memory SQLite, shaped like `orama_app.zetta()`'s: `sql()` answers
    `{"columns": [...], "rows": [{...}]}`.

    It refuses what Lake refuses: `IN` (the workspace's SQL has none) and an answer of more than 5,000 rows.
    `tables` maps `database.table` to its rows.
    """

    MAX_ROWS = 5_000

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.statements: list[str] = []
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        databases = {name.split(".", 1)[0] for name in tables if "." in name}
        for database in sorted(databases):
            self.connection.execute(f"ATTACH DATABASE ':memory:' AS {database}")
        for name, rows in tables.items():
            columns = list(rows[0])
            self.connection.execute(
                f"CREATE TABLE {name} ({', '.join(f'[{column}]' for column in columns)})"
            )
            self.connection.executemany(
                f"INSERT INTO {name} VALUES ({', '.join('?' for _ in columns)})",
                [tuple(row[column] for column in columns) for row in rows],
            )

    def __enter__(self) -> SqliteWorkspace:
        return self

    def __exit__(self, *_exception: object) -> None:
        return None

    def close(self) -> None:
        return None

    def sql(self, statement: str, database: str | None = None, intent: str | None = None) -> dict[str, Any]:
        self.statements.append(statement)
        if re.search(r"\bin\s*\(", statement, re.I):
            raise RuntimeError(
                "sql: expected a comparison operator (=, !=, <>, <, <=, >, >=), got 'IN'"
            )
        cursor = self.connection.execute(statement)
        columns = [column[0] for column in cursor.description or []]
        rows = cursor.fetchmany(self.MAX_ROWS + 1)
        if len(rows) > self.MAX_ROWS:
            raise RuntimeError("sql: too_many_rows: an App query may return at most 5,000 rows")
        return {"columns": columns, "rows": [dict(zip(columns, row)) for row in rows]}


PAGE_ELEMENTS = ("metric", "arrow_data_frame", "table", "plotly_chart", "arrow_vega_lite_chart", "vega_lite_chart")


def page_snapshot(at: Any) -> dict[str, list[tuple[str, str]]]:
    """What the main page shows, by element kind: (label, content) in page order."""
    snapshot: dict[str, list[tuple[str, str]]] = {kind: [] for kind in PAGE_ELEMENTS}

    def walk(node: Any) -> None:
        for child in getattr(node, "children", {}).values():
            kind = getattr(child, "type", "")
            if kind == "metric":
                snapshot[kind].append((str(child.label), f"{child.value} {child.delta}"))
            elif kind in {"arrow_data_frame", "table"}:
                value = child.value
                snapshot[kind].append(("", value.to_csv() if hasattr(value, "to_csv") else str(value)))
            elif kind in snapshot:
                proto = getattr(child, "proto", None)
                spec = getattr(proto, "spec", None) or getattr(proto, "figure", None) or proto
                snapshot[kind].append(("", str(spec)))
            walk(child)

    walk(at.main)
    return snapshot


def narrowing_choices(at: Any) -> list[tuple[str, str, Callable[[Any], Any]]]:
    """For each sidebar filter: (kind, label, a function that sets a narrower value on that widget).

    A multiselect gets its first option alone, a selectbox or radio its last option other than the current one (the
    first is often "All"), and a date range its first half.
    """
    choices: list[tuple[str, str, Callable[[Any], Any]]] = []
    for index, widget in enumerate(at.sidebar.multiselect):
        options = list(widget.options)
        if options:
            choices.append(
                ("multiselect", widget.label, lambda at, i=index, o=options[0]: at.sidebar.multiselect[i].set_value([o]))
            )
    for index, widget in enumerate(at.sidebar.selectbox):
        options = list(widget.options)
        others = [position for position in range(len(options)) if position != widget.index]
        if others:
            choices.append(
                ("selectbox", widget.label, lambda at, i=index, o=others[-1]: at.sidebar.selectbox[i].select_index(o))
            )
    for index, widget in enumerate(at.sidebar.radio):
        options = list(widget.options)
        others = [position for position in range(len(options)) if position != widget.index]
        if others:
            choices.append(
                ("radio", widget.label, lambda at, i=index, o=options[others[-1]]: at.sidebar.radio[i].set_value(o))
            )
    for index, widget in enumerate(at.sidebar.date_input):
        value = widget.value
        if isinstance(value, Iterable) and not isinstance(value, (str, dt.date)):
            dates = list(value)
            if len(dates) == 2 and dates[0] < dates[1]:
                start, end = dates
                middle = start + (end - start) / 2
                choices.append(
                    (
                        "date range",
                        widget.label,
                        lambda at, i=index, s=start, m=middle: at.sidebar.date_input[i].set_value((s, m)),
                    )
                )
    return choices


class FakeOramaApp:
    """Puts an `orama_app` module that answers `zetta()` with a fake workspace in `sys.modules` while it is open."""

    def __init__(self, workspace: Callable[[], Any]) -> None:
        self.workspace = workspace
        self.saved: Any = None

    def __enter__(self) -> FakeOramaApp:
        module = types.ModuleType("orama_app")
        module.zetta = self.workspace  # type: ignore[attr-defined]
        module.current_user = lambda: None  # type: ignore[attr-defined]

        def service(name: str) -> dict[str, Any]:
            raise RuntimeError(f"no Service {name} in the filter check")

        module.service = service  # type: ignore[attr-defined]
        self.saved = sys.modules.get("orama_app")
        sys.modules["orama_app"] = module
        return self

    def __exit__(self, *_exception: object) -> None:
        if self.saved is None:
            sys.modules.pop("orama_app", None)
        else:
            sys.modules["orama_app"] = self.saved


def page_errors(at: Any) -> list[str]:
    """Exceptions and `st.error` messages the page shows: an App that catches its own failure still fails."""
    shown = [str(item.value) for item in at.exception]
    shown += [str(item.value) for item in at.error]
    return shown


def sidebar_filter_defects(
    script: Path,
    workspace: Callable[[], Any],
    timeout: float = 60,
) -> list[str]:
    """Run a Streamlit App and report every sidebar filter that does not reach the whole main page.

    For each sidebar multiselect, selectbox, radio and date range, a fresh run chooses a narrower value. Every chart and
    table on the main page must change, and so must at least one metric. Search boxes are not exercised.
    """
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    def run(change: Callable[[Any], Any] | None = None) -> Any:
        st.cache_data.clear()
        st.cache_resource.clear()
        at = AppTest.from_file(str(script.resolve()), default_timeout=timeout)
        at.run()
        if change is not None and not page_errors(at):
            change(at)
            at.run()
        return at

    with FakeOramaApp(workspace):
        baseline = run()
        if page_errors(baseline):
            return [f"{script.name} shows an error on its first page: {page_errors(baseline)[0][:300]}"]
        before = page_snapshot(baseline)
        filters = narrowing_choices(baseline)
        problems = []
        for kind, label, change in filters:
            at = run(change)
            name = f"the sidebar {kind} {label!r}"
            if page_errors(at):
                problems.append(f"{script.name}: choosing a value in {name} shows an error: {page_errors(at)[0][:300]}")
                continue
            after = page_snapshot(at)
            unchanged = []
            for element in PAGE_ELEMENTS:
                if element == "metric":
                    continue
                for position, (shown, then) in enumerate(before[element]):
                    if position < len(after[element]) and after[element][position][1] == then:
                        unchanged.append(f"{element.replace('arrow_', '').replace('_', ' ')} {position + 1}")
            metrics_before = before["metric"]
            if metrics_before and len(after["metric"]) == len(metrics_before) and after["metric"] == metrics_before:
                unchanged.append("any metric (" + ", ".join(label for label, _ in metrics_before) + ")")
            if unchanged:
                problems.append(
                    f"{script.name}: {name} does not change " + "; ".join(unchanged) + ". Apply every sidebar filter "
                    "to every metric, chart and table: one WHERE clause used by every query, or one filtered frame "
                    "that everything on the page is drawn from"
                )
    return problems
