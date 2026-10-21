# agent-application

The ApplicationAgent: builds web apps and data apps from a description, commits them to a lake repository, and deploys them as lake Apps after the person confirms. Built on agent-runtime. Epic: oramasearch/lake#297.

Private. Its image is published **private** as `oramasearch/agent-application` on Docker Hub.

Current release: [v0.1.0](docs/releases/v0.1.0.md) ([all releases](docs/releases/README.md)).

## Service

The Python service exposes `POST /v1/turn` and `GET /healthz` (which reports the `version`) on port
8080. `POST /v1/turn` requires `Authorization: Bearer <AGENT_SERVICE_TOKEN>`, compared in constant time (`401` otherwise), and fails closed: with `AGENT_SERVICE_TOKEN` unset or empty it refuses every turn with `503`. The operator always sets it; set it yourself for a hand-run service. Turns from lake's bus do not use it. It can also receive `agent_turn` tasks (lake gives them kind `app`) from lake's agent bus when
`LAKE_URL` and `AGENT_BUS_TOKEN` are set; `AGENT_BUS_AGENT` names the agent and defaults to
`agent-application`. Every app deployment remains subject to an explicit confirmation carried on a
follow-up turn. A confirmed proposal that needs packages outside app-runtime's baked set is first
stored as a source-backed runtime App, built by lake, and deployed from the resulting immutable
image reference.

**The model is reached through lake** (lake#248, lake `docs/api/model-gateway.md`): every call goes
to `<lake_url>/openai/v1` with the turn's own `lake_token` as the API key (`model.py`), so lake
counts its tokens in the turn's workspace and enforces the account's model-token limit. The service
holds no `OPENAI_API_KEY`; only the offline evals, which run for no workspace, call OpenAI directly.

Development uses Python 3.12 and uv:

```console
uv sync
uv run pytest
```

See the [documentation index](docs/README.md) for the service contract, application workflow and releases.

## Build and confirmation contract

An initial turn reads the person's lake metadata as needed, validates generated `app.toml` and
source, and returns `result.kind: proposal` with `confirm: true`. It performs no repository or App
writes. A follow-up turn must carry the reviewed object in `context.confirmed_proposal`; only then
does the agent commit the files, create or update the App, deploy it, and follow its rollout.
The follow waits up to three minutes for Ready: a starting pod's Degraded or failed probe is not
judged, only a state that is still there at the deadline (re-read with `get_app`). When the App
logs are unavailable the reply says so and never gives that as the reason it failed (#16).
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
