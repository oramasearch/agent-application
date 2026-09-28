# Evaluation scenarios

`uv run application-agent-evals` runs three live model cases against deterministic fake lake
facts. It supplies the schema of `customer_success.accounts`, a governed SQL result, and an
`account-notes-postgres` Service. Writes fail immediately because an eval request is not a
confirmation.

The cases require:

- a Streamlit churn-risk dashboard using a read-only Zetta binding and verified columns;
- a FastAPI CRUD tool using the Postgres Service binding, with image mode when a driver outside
  the baked runtime is required;
- a static internal forecast guide with no unnecessary binding.

The rule grader validates the complete proposal, framework, bindings, and scenario-specific source
markers. It also requires the dashboard to read datasets, schema, and governed SQL, and the CRUD
case to read Services. JSON and Markdown results are written under `reports/` and are ignored until
deliberately selected for publication.

The native test suite also sends representative generated files through app-runtime's pinned
loader using a fake lake repository. The dashboard query runs against a fake Zetta client, the
static site is served through app-runtime, and the Postgres proposal proves that app-runtime directs
its custom driver to image mode.
