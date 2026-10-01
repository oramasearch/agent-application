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

Confirmation retries are idempotent within a customer. Before creating a repository or App, the
agent lists that customer's rows and reuses an exact name match. It commits the confirmed files as
a new immutable revision, then `deploy_app` updates the existing App's source and desired
generation. This lets the same confirmed proposal resume after `create_repository`, `create_app`,
or deployment failed without creating duplicate rows. Lake may return an existing build for the
same source revision, and the agent observes that build instead of depending on a new build.

Runtime mode accepts only app-runtime's baked dependencies. A required package outside that set
causes an image-mode proposal so the person can review that an image build is required. Confirmation
uses this build chain:

1. Create the lake App in `runtime` mode with its source repository, path, and exact revision.
2. Derive `APP_DEPS` from the manifest requirements outside the baked set and attach the
   space-separated value to the App environment. A proposal-supplied `APP_DEPS` is discarded, and
   credentials remain prohibited in source, manifests, and environment values.
3. Call `build_app`, then long-poll `receive_build_events` and read `get_build` with the
   customer and build IDs until the build reaches a terminal phase.
4. On success, pass the build's immutable target and digest to `deploy_app` as `image`, then
   follow the normal App rollout.
5. On failure, return a question naming the build and the last `build_logs` lines. The agent does
   not call `deploy_app`.

An App whose manifest uses only baked dependencies is created and deployed from source without an
image build, even if an older reviewed proposal labels it as image mode. The agent never builds an
image on the host.

Apps default to `auth: lake`. Public access is valid only when the current request explicitly asks
for it. Zetta bindings are read-only by default. Service connection data and credentials are read
through `orama_app` bindings, never copied into code or ordinary environment values. An environment
credential is represented only by an operator-managed reference such as
`{"API_KEY":{"secret":"forecast-api-key"}}`.

Customer scope is attached to lake list, workspace-query, App, and build reads. Build reads are
GET requests and name the build with `build_id`; App reads name the App with `app_id`. The
delegated credential still enforces the person's policy on every call.
