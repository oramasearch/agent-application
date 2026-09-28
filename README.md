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
