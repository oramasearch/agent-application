# agent-application

The ApplicationAgent: builds web apps and data apps from a description, commits them to a lake repository, and deploys them as lake Apps after the person confirms. Built on agent-runtime. Epic: oramasearch/lake#297.

Private. Its image is published **private** as `oramasearch/agent-application` on Docker Hub.

Current release: [v0.2.7](docs/releases/v0.2.7.md) ([all releases](docs/releases/README.md)).

## Service

The Python service exposes `POST /v1/turn` and `GET /healthz` (which reports the `version`) on port
8080. `POST /v1/turn` requires `Authorization: Bearer <AGENT_SERVICE_TOKEN>`, compared in constant time (`401` otherwise), and fails closed: with `AGENT_SERVICE_TOKEN` unset or empty it refuses every turn with `503`. The operator always sets it; set it yourself for a hand-run service. Turns from lake's bus do not use it. It can also receive `agent_turn` tasks (lake gives them kind `app`) from lake's agent bus when
`LAKE_URL` and `AGENT_BUS_TOKEN` are set; `AGENT_BUS_AGENT` names the agent and defaults to
`agent-application`. Bus tasks are claimed by reference (lake#871): no task credential reaches the
process, and every call made for a task carries the bus credential and `X-Lake-Agent-Task: <task
id>`, falling back to the body's credential on a Lake that refuses it
([service and bus operation](docs/operations/service.md)). Every app deployment remains subject to an explicit confirmation carried on a
follow-up turn. A confirmed proposal that needs packages outside app-runtime's baked set is first
stored as a source-backed runtime App, built by lake, and deployed from the resulting immutable
image reference.

**The model is reached through lake** (lake#248, lake `docs/api/model-gateway.md`): every call goes
to `<LAKE_URL>/openai/v1` with the turn's own `lake_token` as the API key (`model.py`), so lake
counts its tokens in the turn's workspace and enforces the account's model-token limit. The service
holds no `OPENAI_API_KEY`; only the offline evals, which run for no workspace, call OpenAI directly. The model name is `APPLICATION_AGENT_MODEL`, else the Lake's `AGENT_MODEL` (set
by the operator from `spec.agents.model`), else `gpt-6.1-sol` (`docs/operations/service.md`).

**A turn's credential goes only to Lake at this service's own `LAKE_URL`** (#30). Lake capabilities
and the model gateway are always reached at `LAKE_URL`; a turn's `lake_url` is ignored, so the
bearer is never sent to a host the caller names. With `LAKE_URL` unset, or not an absolute
http(s) URL without user info, query or fragment, every turn is refused before a request is made.

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
are delivered through bindings. A secret environment value is a lake secret reference
(`lake-secret://<mount id>/<name>#<field>`), proposed only when the person names an existing secret;
otherwise the agent leaves it out and the person sets it on the App's Environment settings
([docs/building/apps.md](docs/building/apps.md)). Dependencies outside app-runtime's baked set remain an image-mode
signal in the reviewed proposal. On confirmation, the agent creates a runtime App with the source
revision and a derived, non-secret `APP_DEPS`, asks lake to build it, waits for a terminal build
phase, and deploys the built image. Baked-only Apps skip the image build.

## Repairing and changing an App

Lake also starts `repair` turns (an App failed) and `change` turns (more instructions for an App)
on an existing App named by `context.app_id` (lake#1207). The agent reads the App's events,
runtime logs, newest build, source and bindings, and proposes the change as a diff of the files it
changes. Confirmed, it commits them to the App's repository in one commit (each write a drip),
rebuilds when new packages need an image, redeploys and follows the rollout. An auto-fix turn,
which lake grants only when the person opted in and attempts remain, applies its own fix. See
[docs/building/repair.md](docs/building/repair.md).

## Verification and evals

`uv run pytest` covers the service, bus (with claims by reference, `tests/test_by_reference.py`), proposal rules, confirmation gate, deployment calls, and
generated Apps loaded by the pinned app-runtime revision, and whether a generated Streamlit App's sidebar
filters reach every metric, chart and table (agent-application#64): the App runs under Streamlit's
`AppTest` over a SQLite copy of its dataset and each filter is chosen in turn
(`agent_application.app_filters`). Every proposal is also refused when a widget's value is assigned and
never used. The generation and change prompts require every filter to apply to the whole page, `OR`
instead of `IN` (the workspace's SQL has none), filter values as arguments of cached reads, and small
pages (tables of at most 1,000 rows, charts of aggregates). `uv run application-agent-evals` runs the
dashboard, Postgres CRUD, static-site and filtered-dashboard live model cases against deterministic
fake lake facts.
