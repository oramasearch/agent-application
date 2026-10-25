# Service and bus operation

ApplicationAgent serves `POST /v1/turn` and `GET /healthz` on `PORT`, which defaults to `8080`.
`/healthz` answers `ok`, the agent name, the release `version`, the model and the prompt version.
`AGENT_SERVICE_TOKEN` is required as the bearer on direct turn requests, compared in constant time
(`401` otherwise). The route fails closed: with it unset or empty every turn is refused with `503`.
Each turn also carries the signed-in person's `lake_token`, `lake_url`, and optional `customer_id`; the agent uses that
credential only for the duration of the turn.

The lake agent bus is enabled when `LAKE_URL` and `AGENT_BUS_TOKEN` are set; `AGENT_BUS_AGENT`
names the agent and defaults to `agent-application`.
The worker long-polls `receive_agent_tasks`, claims each task once, and publishes progress followed
by a result or failure. The bus credential is used only for those three bus capabilities. The
person's delegated credential remains inside the claimed task body.

The model is `APPLICATION_AGENT_MODEL` when it is set, else `AGENT_MODEL`, else `gpt-5.5`. The Orama
operator sets `AGENT_MODEL` on every library agent pod from the Lake's `spec.agents.model`, so the
Lake's model choice reaches this agent; `APPLICATION_AGENT_MODEL` is this agent's own override
([agent-application#22](https://github.com/oramasearch/agent-application/issues/22)). `/healthz`
reports the model in use.

A turn keeps nothing on the pod's disk (agent-application#21). The pod serves every workspace, and
a turn's message and answer, including the generated App source, are that workspace's content,
which belongs in its workspace Zetta. No trace file is written: the answer goes back on the bus (or
in the `/v1/turn` response) with its `trace_id`, and lake keeps it in the task's record in the
turn's workspace Zetta (lake `docs/api/agent-bus.md`). `TRACE_DIR` is no longer read.

The image runs as UID 10001, exposes port 8080, and is private as
`oramasearch/agent-application`. The repository and image must remain private.


A turn's credential goes only to Lake at this service's own `LAKE_URL` (agent-application#30): Lake
capabilities and the model gateway are always reached there, and a `lake_url` in the turn is
ignored, so the bearer is never sent to a host the caller names. With `LAKE_URL` unset, or not an
absolute http(s) URL without user info, query or fragment, every turn is refused before any request
goes out.
