# Repairing and changing an App

Lake starts two more kinds of ApplicationAgent turn on an App that already exists
(lake#1207, lake `docs/api/notifications.md`):

- **`repair`**: the App failed. Its rollout ended Degraded, failed or crash-looping, or its image
  build failed. The person pressed *Fix with the Application Agent*, or auto-fix started it.
- **`change`**: the person wrote more instructions on the App page (*Change this app with the
  Application Agent*), such as "add a chart by region".

The turn's body carries `kind` and `context.app_id`. Lake adds `context.app` (the App as `get_app`
answers it) and `context.repair` (its repair record: who deployed it, the source it was last
deployed from, auto-fix and its attempts). The code is `src/agent_application/repair.py`; the
prompt is `prompts/repair-v1.md`.

## What the agent reads

With the turn's read-only credential, scoped to the person's own permissions:

- the App's lifecycle events (`receive_app_events` from the first retained one);
- its runtime logs (`app_logs`). When lake cannot answer them, the agent says so in
  what it sends the model and works from the events, the build log and the source. It never gives
  missing logs as the reason the App failed;
- its newest image build and the build log (`list_builds`, `build_logs`);
- its source: every file under the App's folder in its repository (`repository_tree`,
  `repository_file`), at the deployed revision. An App deployed from a built image keeps no source
  in its desired state, so the agent uses the source lake recorded at its last deploy.

## The proposal is a diff

The model answers with the files it changes, each with its complete new contents, and any files to
delete. The agent checks them before anyone sees them:

- safe relative paths, at most 40 files, Python that compiles;
- no secret literal and never the turn's credential;
- `app.toml` keeps its `name`, `framework` and `port`, and no dependency carries a credential;
- at least one file actually changes.

A failed check goes back to the model, twice at most, then the turn answers a question. A valid
answer is `result.kind: proposal` with `confirm: true`, `turn` (`repair` or `change`), `diagnosis`
(what was found, in plain words), `diff` (a unified diff of every changed file), `files`, `delete`,
and `app` (its id, name and source). Nothing is written on this turn.

## Confirmed, one commit and a redeploy

Lake hands the reviewed proposal back on a later turn of the same kind, as
`context.confirmed_proposal`, after checking it against the stored one (`proposal_task_id`,
`proposal_sha256`). Only that turn has `commit_repository_files`, `build_app`, `set_app_env` and
`deploy_app`, and never `create_app` or `set_app_auth`. The agent:

1. refuses a proposal for another App;
2. commits the changed files to the App's repository on `main` in one commit (`Repair the <name>
   App: <diagnosis>` or `Change the <name> App: …`). Each file it writes is a liquid drip, signed
   and attributed to the person, so every repair can be seen and reverted;
3. when `app.toml` now needs packages outside app-runtime's baked set, sets `APP_DEPS`, builds the
   image (`build_app`) and deploys the built image; otherwise deploys the new source revision;
4. follows the rollout to Ready (up to three minutes, as for a new App) and reports in the same
   conversation: running again, or still failing and why.

## Auto-fix

When the person turned auto-fix on for the App, lake starts a `repair` turn with
`context.auto_fix: {attempt, max_attempts}` and grants the write tools without a confirmation. Lake
counts the attempts and stops at the limit; the agent applies its own proposal, prefixes the reply
with `Auto-fix attempt n of m`, and writes nothing when it finds no fix.

## Later instructions

Lake keeps one repair and one change conversation per App, so later instructions continue it. The
agent's history for that conversation (held in memory while the service runs, never on disk) holds
the earlier answers, and each turn is given the App's files as they are now, so a turn after a
restart still starts from the current source.

## Conversation history across restarts

This service keeps a conversation's messages in process memory, so a restart or another replica starts empty. Lake sends
up to 20 earlier turns of an App's change or repair conversation with each turn (`history`: message, reply, outcome,
diff, confirmed; lake#1247). When the service has no history for the conversation, `seeded_history()` turns them into
chat messages first, so the agent keeps the thread.
