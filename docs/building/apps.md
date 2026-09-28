# Building, confirming, and deploying Apps

ApplicationAgent designs FastAPI, Streamlit, and static Apps. An initial turn may read lake facts
as the signed-in person: datasets and schemas, governed workspace SQL, available Services,
repository files, existing Apps, rollout events, and logs. A proposal contains the full file set
and an App description with source repository, path, mode, framework, port, host, auth, bindings,
and non-secret environment values.

The answer contract is:

```json
{"reply":"...","result":{"kind":"question|proposal|deployed","files":[],"app":{},"confirm":false}}
```

`confirm` is true only for a validated proposal. Text such as “yes” does not authorize a change.
Lake must send the reviewed object back on a later turn as `context.confirmed_proposal`. The agent
validates it again, creates a private repository when needed, commits the files, creates or updates
the App, deploys the immutable revision, and follows lifecycle events. Existing Apps keep their id
and receive a new revision. A failed rollout is explained with its event and `app_logs` detail.

Runtime mode accepts only app-runtime's baked dependencies. A required package outside that set
causes an image-mode proposal; after confirmation the agent calls lake's in-cluster `build_app`
capability. It never builds an image itself.

Apps default to `auth: lake`. Public access is valid only when the current request explicitly asks
for it. Zetta bindings are read-only by default. Service connection data and credentials are read
through `orama_app` bindings, never copied into code or ordinary environment values.

