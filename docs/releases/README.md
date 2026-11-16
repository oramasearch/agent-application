# Releases

One page per tagged release, newest first. A page states what the release includes, what it does
not include, known issues, verification output, and where the image runs. The first release lists
everything the agent does.

A release has one `vX.Y.Z` name shared by the Git tag, the GitHub release and the Docker Hub
image tag `oramasearch/agent-application:vX.Y.Z`. The image tag names the digest built from the Git
tag and deployed; it is never produced by a second build.

| Release | Date | Summary |
|---|---|---|
| [v0.2.7](v0.2.7.md) | 2026-10-27 | A patch: files read exactly; filters applied everywhere |
| [v0.2.6](v0.2.6.md) | 2026-10-26 | A patch: repository paths are written without '.' components, so a confirmed App at path '.' commits |
| [v0.2.5](v0.2.5.md) | 2026-10-26 | A patch: repository paths are written without '.' components, so a confirmed App at path '.' commits |
| [v0.2.4](v0.2.4.md) | 2026-10-26 | A patch: a conversation is seeded from Lake's history after a restart |
| [v0.2.3](v0.2.3.md) | 2026-10-26 | A patch: generated Apps aggregate in SQL and page their reads; fallback model `gpt-6.1-sol` |
| [v0.2.2](v0.2.2.md) | 2026-10-26 | A patch: generated Apps call `zetta()` before its reads; `zetta.sql(...)` is refused before commit |
| [v0.2.1](v0.2.1.md) | 2026-10-25 | A patch: `repair` and `change` turns on an existing App, as a confirmed diff committed in one commit, with Lake's opt-in auto-fix |
| [v0.2.0](v0.2.0.md) | 2026-10-25 | Security-review fixes (constant-time service token, refused when unset; the turn credential only to `LAKE_URL`; no turn trace on disk), Lake secret references, and the Lake's `AGENT_MODEL` |
| [v0.1.0](v0.1.0.md) | 2026-10-17 | The first release: the turn API and `/healthz`, the agent-bus worker, the propose-then-confirm App build and deploy through lake, prompt `v1`, and the image |

`pyproject.toml`'s `[project].version` is the one place the version is set; `GET /healthz` reports
it, and `tests/test_release.py` fails when that version has no page here.
