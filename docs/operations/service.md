# Service and bus operation

ApplicationAgent serves `POST /v1/turn` and `GET /healthz` on `PORT`, which defaults to `8080`.
`/healthz` answers `ok`, the agent name, the release `version`, the model and the prompt version.
Set `AGENT_SERVICE_TOKEN` to require a bearer token on direct turn requests. Each turn also carries
the signed-in person's `lake_token`, `lake_url`, and optional `customer_id`; the agent uses that
credential only for the duration of the turn.

The lake agent bus is enabled when `LAKE_URL` and `AGENT_BUS_TOKEN` are set; `AGENT_BUS_AGENT`
names the agent and defaults to `agent-application`.
The worker long-polls `receive_agent_tasks`, claims each task once, and publishes progress followed
by a result or failure. The bus credential is used only for those three bus capabilities. The
person's delegated credential remains inside the claimed task body.

`APPLICATION_AGENT_MODEL` selects the model. JSON traces are written under `TRACE_DIR`, which
defaults to `/tmp/agent-application-traces`.

The image runs as UID 10001, exposes port 8080, and is private as
`oramasearch/agent-application`. The repository and image must remain private.

