# Evaluation scenarios

`uv run application-agent-evals` runs four live model cases against deterministic fake lake
facts. It supplies the schema of `customer_success.accounts`, a governed SQL result, an
`account-notes-postgres` Service, and `datasets.customers` (400 fixed rows: country, segment,
signup date, value, churn), whose SQL is answered by SQLite. Like Lake, that SQL refuses `IN` and
more than 5,000 rows. Writes fail immediately because an eval request is not a
confirmation.

The cases require:

- a Streamlit churn-risk dashboard using a read-only Zetta binding and verified columns;
- a FastAPI CRUD tool using the Postgres Service binding, with image mode when a driver outside
  the baked runtime is required;
- a static internal forecast guide with no unnecessary binding;
- a Streamlit customer-insights dashboard with sidebar filters for country, segment and a signup
  date range, where every filter applies to every metric, chart and table.

The rule grader validates the complete proposal, framework, bindings, and scenario-specific source
markers. It also requires the dashboard to read datasets, schema, and governed SQL, and the CRUD
case to read Services. For the filtered dashboard, the grader then runs the proposed App under
Streamlit's `AppTest` against the SQLite copy, chooses each sidebar filter in turn (one country, the
last segment, the first half of the date range), and fails the case for every chart or table that
does not change, or when no metric changes (`agent_application.app_filters.sidebar_filter_defects`).

The evals call the model with the OpenAI client's own settings (`OPENAI_API_KEY`, `OPENAI_BASE_URL`).
Lake's model gateway does not accept a person's key: it answers 403, *only an agent's own task, launch
or schedule credential may call a model through Lake*. So the evals cannot run through a lake with a
test user's API key. JSON and Markdown results are written under `reports/` and are ignored until
deliberately selected for publication.

The native test suite also sends representative generated files through app-runtime's pinned
loader using a fake lake repository. The dashboard query runs against a fake Zetta client, the
static site is served through app-runtime, and the Postgres proposal proves that app-runtime directs
its custom driver to image mode. `tests/test_generated_apps.py` runs the filter check on written
dashboards: filters in one shared `WHERE` clause, and one filtered frame, both pass. A chart whose query
ignores the filters, a chart drawn from the frame read before them, and a filter written with `IN` are
all reported.
