# agent-application

The ApplicationAgent: builds web apps and data apps from a description, commits them to a lake repository, and deploys them as lake Apps after the person confirms. Built on agent-runtime. Epic: oramasearch/lake#297.

Private. Its image is published **private** as `oramasearch/agent-application` on Docker Hub.

## Service

The Python service exposes `POST /v1/turn` and `GET /healthz` on port 8080. It can also receive
`agent_turn` tasks of kind `app` from lake's agent bus with `AGENT_BUS_TOKEN` and
`AGENT_BUS_AGENT`. Every app deployment remains subject to an explicit confirmation carried on a
follow-up turn. A confirmed proposal that needs packages outside app-runtime's baked set is first
stored as a source-backed runtime App, built by lake, and deployed from the resulting immutable
image reference.

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
Every customer-owned read (repositories, datasets, services, App state, and image-build state)
carries the turn's `customer_id`. Build progress is read with `receive_build_events` and
`get_build`; build failures include the tail from `build_logs`.

Apps default to lake authentication and private source repositories. Zetta and Service credentials
are delivered through bindings. Dependencies outside app-runtime's baked set remain an image-mode
signal in the reviewed proposal. On confirmation, the agent creates a runtime App with the source
revision and a derived, non-secret `APP_DEPS`, asks lake to build it, waits for a terminal build
phase, and deploys the built image. Baked-only Apps skip the image build.

## Verification and evals

`uv run pytest` covers the service, bus, proposal rules, confirmation gate, deployment calls, and
generated Apps loaded by the pinned app-runtime revision. `uv run application-agent-evals` runs the
dashboard, Postgres CRUD, and static-site live model cases against deterministic fake lake facts.
