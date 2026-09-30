# agent-application

The ApplicationAgent: builds web apps and data apps from a description, commits them to a lake repository, and deploys them as lake Apps after the person confirms. Built on agent-runtime. Epic: oramasearch/lake#297.

Private. Its image is published **private** as `oramasearch/agent-application` on Docker Hub.

## Service

The Python service exposes `POST /v1/turn` and `GET /healthz` on port 8080. It can also receive
`agent_turn` tasks of kind `app` from lake's agent bus with `AGENT_BUS_TOKEN` and
`AGENT_BUS_AGENT`. Every app deployment remains subject to an explicit confirmation carried on a
follow-up turn.

Development uses Python 3.12 and uv:

```console
uv sync
uv run pytest
```

See the [documentation index](docs/README.md) for the service contract and application workflow.

## Build and confirmation contract

An initial turn reads the person's lake metadata as needed, validates generated `app.toml` and
source, and returns `result.kind: proposal` with `confirm: true`. It performs no repository or App
writes. A follow-up turn must carry the reviewed object in `context.confirmed_proposal`; only then
does the agent commit the files, create or update the App, deploy it, and follow its rollout.
Retries reuse a repository or App with the proposal's exact name in the same customer, so a
failure after either row is created can resume from the reviewed proposal.
Every customer-owned read (repositories, datasets, services, and the App reads `get_app`,
`app_logs` and `receive_app_events`) carries the turn's `customer_id`; App reads name the App by
`app_id`.

Apps default to lake authentication and private source repositories. Zetta and Service credentials
are delivered through bindings. Dependencies outside app-runtime's baked set select lake's
in-cluster image mode.

## Verification and evals

`uv run pytest` covers the service, bus, proposal rules, confirmation gate, deployment calls, and
generated Apps loaded by the pinned app-runtime revision. `uv run application-agent-evals` runs the
dashboard, Postgres CRUD, and static-site live model cases against deterministic fake lake facts.
