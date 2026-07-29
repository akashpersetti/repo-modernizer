# RepoModernizer Sub-Project 3b: Dashboard + Demo + Polish — Design

**Parent spec:** `RepoModernizer-Spec.md` (Build Order §11, step 7)
**Prior sub-projects:** local agent core (1), durable service layer (2), infra + deploy (3a) — all implemented, live-tested, deployed, pushed to `main`. The real backend (API Gateway → Lambda → SQS → Fargate/EFS → DynamoDB → GitHub PR) is live at `https://6yncgq73gk.execute-api.us-east-1.amazonaws.com`.
**Scope of this sub-project:** a Next.js dashboard for the deployed service, a small backend fix it depends on, CI/CD for the frontend, a demo recording script, and final README polish. This is the last sub-project — after this, every row in spec §1's signal table is covered by something real and deployed.

## Why this scope

Spec's Build Order step 7 (dashboard, demo GIF, README polish) was deliberately deferred out of 3a to keep infra/deploy focused. Everything here is presentation-layer work sitting on top of an already-complete, already-verified backend — no new backend architecture, one small necessary fix.

## Decisions locked in for this sub-project

- **Hosting: S3 + CloudFront on AWS, not Vercel.** Explicit requirement — "everything on AWS, near-zero cost." Confirmed against `github.com/akashpersetti/twin`'s working pattern (`next.config.ts` has `output: 'export'`, an S3 bucket, and a separately-deployed backend called client-side) before locking this in, since a naive "Next.js dashboard" build defaults to assuming a Node server, which static S3 hosting can't run.
- **Static export, client-side fetch, CORS on API Gateway** — the corrected architecture after checking `twin`'s actual pattern. `output: 'export'` means no server-side code runs per-request (no Server Components data-fetching, no Route Handlers) — the browser calls the real API Gateway URL directly. `infra/apigateway.tf`'s existing `aws_apigatewayv2_api.main` gains a `cors_configuration` block.
- **S3 access via CloudFront Origin Access Control (OAC)**, not a public bucket policy — more correct than `twin`'s own public-bucket approach; barely more Terraform, meaningfully better practice. The bucket itself stays fully private.
- **No dynamic Next.js routes.** Static export can't resolve per-request dynamic path segments (`/task/[id]`) without a known-at-build-time set of IDs, which doesn't exist here. Client components read `task_id` from a query string (`/task?id=...`) instead — sidesteps the limitation entirely, still a normal-feeling URL.
- **No frontend component library** (no shadcn/ui etc.) — two pages, a form, a table, a diff view, a couple of buttons. Tailwind alone is enough; a UI kit would be pure dependency weight at this scope.
- **No dedicated frontend test framework.** Default `create-next-app` TypeScript + ESLint, then live verification — open the real CloudFront URL, drive a real migration through it. Matches this project's consistent choice throughout (sub-projects 1–3a) of live/integration proof over unit-test coverage for exactly this kind of thin, mostly-UI layer.
- **Frontend CI/CD wired in now**, not deferred — `deploy.yml` gains a `frontend` job (npm build, S3 sync, CloudFront invalidation) triggered the same push-to-`main` way as the backend job.
- **Demo GIF: I write the script, you record it.** Screen capture isn't something this session can do. `docs/demo_script.md` gives exact steps/commands to reproduce the full story on camera.

## The backend gap found while designing this

`GET /tasks/{id}` currently has no way to surface a PR URL. `app/worker/entrypoint.py`'s `_finalize_if_done` calls `github.open_pull_request(...)` and gets a URL back, but that happens *after* `graph.invoke()` returns — the checkpointed `GraphState` is never updated with it, so there's no way for a later status read to know the PR exists. Fix: write the URL as a plain sibling DynamoDB item (`PK=TASK#{task_id}`, `SK=PR_URL`) directly after `open_pull_request()` succeeds — not routed through the LangGraph checkpointer interface, no reason to. `routes_tasks.py`'s `get_task()` does one extra `get_item` for it and adds `pr_url: Optional[str]` to `TaskStatusResponse`.

## Architecture

```
Browser ← CloudFront (OAC) ← S3 (static Next.js export, output: 'export')
   │
   └─ client-side fetch() directly to API Gateway (CORS enabled)
        POST /tasks, GET /tasks/{id} (polled), POST /tasks/{id}/approve, /resume

Pages (static, no dynamic path segments — client components read query params):
  /              — start-migration form → POST /tasks → redirect to /task?id={task_id}
  /task?id=X     — polls GET /tasks/{id}, renders matrix (files × status × tokens × cost),
                   shows awaiting_approval banner + diff + approve/reject buttons,
                   shows PR link once done (via the new pr_url field)
```

## Terraform additions

- `infra/frontend.tf` — private S3 bucket (no public policy, no website-hosting config — pure OAC-fronted origin), CloudFront distribution with an Origin Access Control, bucket policy scoped to that specific distribution's ARN only (not "any CloudFront," not public). Output: the CloudFront domain (the live dashboard URL).
- `infra/apigateway.tf` — add a `cors_configuration` block to the existing `aws_apigatewayv2_api.main`: `allow_origins` scoped to the CloudFront domain (built via Terraform interpolation, not `*`), `allow_methods = ["GET", "POST", "OPTIONS"]`, `allow_headers = ["content-type"]`.

## CI/CD

`.github/workflows/deploy.yml` gains a `frontend` job, same trigger (push to `main`) as the existing `deploy` job: `npm ci` in `frontend/`, `npm run build` (produces `frontend/out/` via the static export config), `aws s3 sync frontend/out/ s3://<bucket> --delete`, `aws cloudfront create-invalidation --paths "/*"`. Uses the same OIDC role (`github_deploy`) — needs its IAM policy extended with `s3:PutObject`/`s3:DeleteObject`/`s3:ListBucket` on the frontend bucket and `cloudfront:CreateInvalidation`.

## Demo + README polish

- `docs/demo_script.md` — numbered steps to record on camera: open the live dashboard, submit a real migration against `repomodernizer-demo-target`, show the interrupt/approve UI live, show the resulting PR. Suggested capture tooling (macOS screen recording → `gifski`/`ffmpeg`), not something this session executes.
- `README.md` rewrite — demo GIF embedded at the top, spec §1's reliability-signal table, one architecture diagram reflecting what's actually deployed (Lambda/Fargate/SQS/EFS split — not the original spec sketch, which predates several corrections made along the way), a "Run it yourself" section with the real live dashboard URL and API URL, ordered so the first screen sells the project before any scrolling.

## Testing / verification

- `terraform plan`/`apply` for `frontend.tf` and the CORS addition, run for real against the existing AWS account.
- One real end-to-end pass through the **deployed dashboard in an actual browser**: submit the form, watch it hit the real interrupt (risk ≥ threshold on the demo repo, same as every prior live run), click approve, watch it reach `done`, click through to the real PR. This is the actual proof, same standard as every prior sub-project.
- CI/CD verified with a real merge: `frontend` job builds, syncs, invalidates; confirm the CloudFront URL serves the new build afterward (e.g. a trivial visible change, or just re-checking the deployed JS bundle hash changed).

## Out of scope

- Any change to the migration/agent logic itself (guardrails, risk, budget, providers) — untouched.
- Auth on the dashboard or the API — matches the rest of the project, no auth anywhere yet; out of scope here too.
- A "list all tasks" view — `GET /tasks/{id}` still requires knowing the `task_id` (returned by the start form), consistent with sub-project 2's decision to skip a separate Tasks registry table.
