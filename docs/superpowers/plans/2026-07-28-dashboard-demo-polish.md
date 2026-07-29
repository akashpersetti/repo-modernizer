# Dashboard + Demo + Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Next.js dashboard (static export, S3+CloudFront, client-side calls to the real deployed API) for starting migrations and approving risky diffs in a browser, a small backend fix it depends on, CI/CD for the frontend, a demo recording script, and a final README rewrite.

**Architecture:** Static Next.js export (`output: 'export'`) hosted on S3, served via CloudFront with Origin Access Control. The browser calls the real API Gateway URL directly — CORS added there, no server-side Next.js code runs at request time. Two pages: `/` (start-migration form) and `/task?id=X` (polls status, shows the interrupt/approve UI, links the resulting PR once done).

**Tech Stack:** Next.js (App Router, static export), TypeScript, Tailwind v4, Terraform (S3 + CloudFront + OAC), GitHub Actions.

## Global Constraints

- No dynamic Next.js routes — `output: 'export'` can't resolve per-request path params. `task_id` travels as a query string (`/task?id=...`), not a path segment.
- No component library — Tailwind only.
- No dedicated frontend test framework — TypeScript + default ESLint, then live verification against the real deployed stack (same standard every prior sub-project used).
- `NEXT_PUBLIC_API_URL` defaults to the real, already-known API Gateway URL (`https://6yncgq73gk.execute-api.us-east-1.amazonaws.com`) baked in as a fallback — no secret needed for the frontend build.
- S3 bucket for the frontend is fully private; access only via CloudFront OAC, bucket policy scoped to that specific distribution's ARN.
- AWS account `914697327092`, region `us-east-1` (same as every prior sub-project).

---

### Task 1: Backend fix — surface the PR URL

**Files:**
- Modify: `app/agent/checkpointer.py`
- Modify: `app/worker/entrypoint.py`
- Modify: `app/api/routes_tasks.py`
- Test: `tests/test_checkpointer.py` (extend)
- Test: `tests/test_entrypoint.py` (extend)
- Test: `tests/test_routes.py` (extend)

**Interfaces:**
- Produces: `DynamoDBCheckpointer.put_pr_url(task_id: str, url: str) -> None`, `.get_pr_url(task_id: str) -> Optional[str]`.
- Modifies: `_finalize_if_done(result, token, checkpointer)` (gains the `checkpointer` param); `TaskStatusResponse` gains `pr_url: Optional[str]`.

**Note:** both `test_entrypoint.py` and `test_routes.py`'s existing tests inject `MemorySaver()` (LangGraph's built-in in-memory checkpointer) in place of `DynamoDBCheckpointer` — `MemorySaver` has no `put_pr_url`/`get_pr_url`. Both call sites guard with `hasattr(checkpointer, "...")` so those existing tests keep passing unmodified, while production (which always uses the real `DynamoDBCheckpointer`) stores/reads the URL for real.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_checkpointer.py — add this test
def test_put_and_get_pr_url_roundtrip():
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    assert _checkpointer.get_pr_url(thread_id) is None
    _checkpointer.put_pr_url(thread_id, "https://github.com/x/y/pull/1")
    assert _checkpointer.get_pr_url(thread_id) == "https://github.com/x/y/pull/1"
```

```python
# tests/test_entrypoint.py — add `import uuid` at the top, then add this test
def test_finalize_stores_pr_url_when_migration_completes(tmp_path, monkeypatch):
    import app.worker.entrypoint as ep
    from app.agent.checkpointer import DynamoDBCheckpointer
    from app.config import Settings

    remote = _make_bare_remote(tmp_path)
    settings = Settings()
    checkpointer = DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)
    monkeypatch.setattr(ep.github, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(ep.github, "open_pull_request", lambda *a, **k: "https://github.com/x/y/pull/42")

    task_id = f"pr-url-test-{uuid.uuid4().hex[:8]}"
    responses = [
        json.dumps([{"path": "webapp.py", "rationale": "t", "risk_score": 0.1}]),
        "x = 2\n",
    ]

    def deps_factory():
        return NodeDeps(
            providers=FakeProviderRouter(responses), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        )

    env = {
        "ACTION": "start", "TASK_ID": task_id, "REPO_URL": str(remote),
        "GOAL": "bump x", "TEST_COMMAND": "true", "WORKSPACE_ROOT": str(tmp_path / "workspace_root"),
    }
    monkeypatch.setattr(os, "environ", {**os.environ, **env})
    ep.run(checkpointer_factory=lambda: checkpointer, deps_factory=deps_factory, github_token="")

    assert checkpointer.get_pr_url(task_id) == "https://github.com/x/y/pull/42"
```

```python
# tests/test_routes.py — add this test
def test_get_task_status_includes_pr_url():
    with patch("app.api.routes_tasks.DynamoDBCheckpointer", return_value=MemorySaver()):
        fake_sqs = FakeSQS()
        settings = Settings()
        configure(settings, sqs_client=fake_sqs)
        client = TestClient(app)

        response = client.get("/tasks/fake-task-id")

        assert response.status_code == 200
        assert "pr_url" in response.json()
        assert response.json()["pr_url"] is None  # MemorySaver has no get_pr_url -- must not crash
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_checkpointer.py::test_put_and_get_pr_url_roundtrip tests/test_entrypoint.py::test_finalize_stores_pr_url_when_migration_completes tests/test_routes.py::test_get_task_status_includes_pr_url -v`
Expected: FAIL — `AttributeError: 'DynamoDBCheckpointer' object has no attribute 'put_pr_url'` and `pr_url` missing from the routes response.

- [x] **Step 3: Implement**

```python
# app/agent/checkpointer.py — add these two methods to DynamoDBCheckpointer,
# after get_tuple/list (order doesn't matter, but keep them together)
    def put_pr_url(self, task_id: str, url: str) -> None:
        self._table.put_item(Item={
            "PK": f"TASK#{task_id}", "SK": "PR_URL", "url": url,
            "ttl": int(time.time()) + _TTL_SECONDS,
        })

    def get_pr_url(self, task_id: str) -> Optional[str]:
        resp = self._table.get_item(Key={"PK": f"TASK#{task_id}", "SK": "PR_URL"})
        item = resp.get("Item")
        return item["url"] if item else None
```

```python
# app/worker/entrypoint.py — change _finalize_if_done's signature and body
def _finalize_if_done(result: dict, token: str, checkpointer) -> None:
    if "__interrupt__" in result:
        return
    if any(f["status"] in ("migrated", "approved") for f in result["files"].values()):
        workspace = Path(result["repo_path"])
        github.commit_all(workspace, f"RepoModernizer: {result['goal']}")
        github.push_branch(workspace, result["branch"], token)
        pr_url = github.open_pull_request(
            result["repo_url"], result["branch"], result["base_branch"],
            title=f"RepoModernizer: {result['goal']}",
            body="Opened automatically by RepoModernizer.",
            token=token,
        )
        if hasattr(checkpointer, "put_pr_url"):
            checkpointer.put_pr_url(result["task_id"], pr_url)
```

And update the one call site in `run()`:
```python
    _finalize_if_done(result, token, checkpointer)
```

```python
# app/api/routes_tasks.py — TaskStatusResponse gains a field:
class TaskStatusResponse(BaseModel):
    task_id: str
    files: dict
    cost_used_usd: float
    awaiting_approval: Optional[dict]
    done: bool
    pr_url: Optional[str]
```

And in `get_task()`, add the lookup and pass it through:
```python
    pr_url = checkpointer.get_pr_url(task_id) if hasattr(checkpointer, "get_pr_url") else None
    return TaskStatusResponse(
        task_id=task_id,
        files=snapshot.values.get("files", {}),
        cost_used_usd=snapshot.values.get("cost_used_usd", 0.0),
        awaiting_approval=awaiting_approval,
        done=not snapshot.next,
        pr_url=pr_url,
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: full suite passes.

- [x] **Step 5: Commit**

```bash
git add app/agent/checkpointer.py app/worker/entrypoint.py app/api/routes_tasks.py \
        tests/test_checkpointer.py tests/test_entrypoint.py tests/test_routes.py
git commit -m "feat: surface PR URL on task status — stored as a sibling DynamoDB item after finalize"
```

---

### Task 2: Next.js scaffolding

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/next.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/postcss.config.mjs`
- Create: `frontend/app/globals.css`
- Create: `frontend/app/layout.tsx`
- Create: `frontend/lib/api.ts`
- Create: `frontend/.gitignore`
- Create: `frontend/eslint.config.mjs`

**Interfaces:**
- Produces: `createTask`, `getTaskStatus`, `approveTask` (in `lib/api.ts`), the `TaskStatus`/`FileStatus` types every later page consumes.

- [x] **Step 1: Write the scaffolding**

```json
// frontend/package.json
{
  "name": "repomodernizer-dashboard",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "lint": "eslint"
  },
  "dependencies": {
    "next": "^15.1.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@tailwindcss/postcss": "^4",
    "@types/node": "^20",
    "@types/react": "^19",
    "@types/react-dom": "^19",
    "eslint": "^9",
    "eslint-config-next": "^15.1.0",
    "tailwindcss": "^4",
    "typescript": "^5"
  }
}
```

```typescript
// frontend/next.config.ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
```

```json
// frontend/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2017",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": true,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": { "@/*": ["./*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

```javascript
// frontend/postcss.config.mjs
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
```

```css
/* frontend/app/globals.css */
@import "tailwindcss";
```

```tsx
// frontend/app/layout.tsx
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RepoModernizer",
  description: "Autonomous repository modernization agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-gray-50 text-gray-900 min-h-screen">{children}</body>
    </html>
  );
}
```

```typescript
// frontend/lib/api.ts
const API_URL = process.env.NEXT_PUBLIC_API_URL || "https://6yncgq73gk.execute-api.us-east-1.amazonaws.com";

export type FileStatus = {
  path: string;
  status: string;
  tokens: number;
  cost_usd: number;
  retry_count: number;
  last_error: string | null;
};

export type TaskStatus = {
  task_id: string;
  files: Record<string, FileStatus>;
  cost_used_usd: number;
  awaiting_approval: { path: string; diff: string; risk_score: number } | null;
  done: boolean;
  pr_url: string | null;
};

export async function createTask(input: {
  repo_url: string;
  goal: string;
  test_command: string;
  base_branch?: string;
}): Promise<{ task_id: string }> {
  const res = await fetch(`${API_URL}/tasks`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(`create task failed: ${res.status} ${await res.text()}`);
  return res.json();
}

export async function getTaskStatus(taskId: string): Promise<TaskStatus> {
  const res = await fetch(`${API_URL}/tasks/${taskId}`);
  if (!res.ok) throw new Error(`get status failed: ${res.status} ${await res.text()}`);
  return res.json();
}

export async function approveTask(
  taskId: string,
  file: string,
  decision: "approve" | "reject",
  note = ""
): Promise<void> {
  const res = await fetch(`${API_URL}/tasks/${taskId}/approve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ file, decision, note }),
  });
  if (!res.ok) throw new Error(`approve failed: ${res.status} ${await res.text()}`);
}
```

```
# frontend/.gitignore
node_modules/
.next/
out/
*.tsbuildinfo
next-env.d.ts
```

```javascript
// frontend/eslint.config.mjs
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

const eslintConfig = [...compat.extends("next/core-web-vitals", "next/typescript")];

export default eslintConfig;
```

- [x] **Step 2: Install and verify the toolchain resolves**

Run: `cd frontend && npm install`
Verify: exits 0, `node_modules/` populated, no peer-dependency errors printed as failures (warnings are fine).

- [x] **Step 3: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/next.config.ts frontend/tsconfig.json \
        frontend/postcss.config.mjs frontend/app/globals.css frontend/app/layout.tsx frontend/lib/api.ts \
        frontend/.gitignore frontend/eslint.config.mjs
git commit -m "feat: Next.js static-export scaffolding, Tailwind v4, typed API client"
```

---

### Task 3: Home page — start-migration form

**Files:**
- Create: `frontend/app/page.tsx`

**Interfaces:**
- Consumes: `createTask` from `frontend/lib/api.ts` (Task 2).

- [x] **Step 1: Write the page**

```tsx
// frontend/app/page.tsx
"use client";

import { useState } from "react";
import { createTask } from "@/lib/api";

export default function HomePage() {
  const [repoUrl, setRepoUrl] = useState("https://github.com/akashpersetti/repomodernizer-demo-target");
  const [goal, setGoal] = useState("Migrate this Flask app to FastAPI with async route handlers.");
  const [testCommand, setTestCommand] = useState("pytest -q");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const { task_id } = await createTask({ repo_url: repoUrl, goal, test_command: testCommand });
      window.location.href = `/task?id=${task_id}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  }

  return (
    <main className="max-w-xl mx-auto py-16 px-4">
      <h1 className="text-2xl font-semibold mb-2">RepoModernizer</h1>
      <p className="text-gray-600 mb-6 text-sm">
        Autonomous repository modernization agent — durable, human-gated, deployed on AWS.
      </p>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Repo URL</label>
          <input
            className="w-full border rounded px-3 py-2"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Goal</label>
          <input
            className="w-full border rounded px-3 py-2"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Test command</label>
          <input
            className="w-full border rounded px-3 py-2"
            value={testCommand}
            onChange={(e) => setTestCommand(e.target.value)}
            required
          />
        </div>
        {error && <p className="text-red-600 text-sm">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="bg-black text-white px-4 py-2 rounded disabled:opacity-50"
        >
          {submitting ? "Starting..." : "Start migration"}
        </button>
      </form>
    </main>
  );
}
```

- [x] **Step 2: Build to verify it compiles**

Run: `cd frontend && npm run build`
Expected: succeeds, produces `frontend/out/index.html`. (No dev server smoke test yet — Task 4 needs to exist first for a meaningful click-through; Task 7 is the first real end-to-end check.)

- [x] **Step 3: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat: start-migration form (home page)"
```

---

### Task 4: Task status page — poll, matrix, approve/reject, PR link

**Files:**
- Create: `frontend/app/task/page.tsx`

**Interfaces:**
- Consumes: `getTaskStatus`, `approveTask`, `TaskStatus` from `frontend/lib/api.ts` (Task 2).

- [x] **Step 1: Write the page**

```tsx
// frontend/app/task/page.tsx
"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { approveTask, getTaskStatus, TaskStatus } from "@/lib/api";

function TaskView() {
  const params = useSearchParams();
  const taskId = params.get("id") ?? "";
  const [status, setStatus] = useState<TaskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  useEffect(() => {
    if (!taskId) return;
    let cancelled = false;
    async function poll() {
      try {
        const s = await getTaskStatus(taskId);
        if (!cancelled) setStatus(s);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }
    poll();
    const interval = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [taskId]);

  async function handleDecision(decision: "approve" | "reject") {
    if (!status?.awaiting_approval) return;
    setActing(true);
    try {
      await approveTask(taskId, status.awaiting_approval.path, decision);
    } finally {
      setActing(false);
    }
  }

  if (!taskId) return <p className="p-8">No task id in URL.</p>;
  if (error) return <p className="p-8 text-red-600">{error}</p>;
  if (!status) return <p className="p-8">Loading...</p>;

  return (
    <main className="max-w-3xl mx-auto py-12 px-4 space-y-6">
      <h1 className="text-xl font-semibold">Task {taskId}</h1>

      {status.awaiting_approval && (
        <div className="border border-yellow-400 bg-yellow-50 rounded p-4 space-y-3">
          <p className="font-medium">
            Awaiting approval — {status.awaiting_approval.path} (risk {status.awaiting_approval.risk_score.toFixed(2)})
          </p>
          <pre className="bg-black text-green-400 text-xs p-3 rounded overflow-x-auto">
            {status.awaiting_approval.diff}
          </pre>
          <div className="space-x-2">
            <button
              onClick={() => handleDecision("approve")}
              disabled={acting}
              className="bg-green-600 text-white px-3 py-1.5 rounded disabled:opacity-50"
            >
              Approve
            </button>
            <button
              onClick={() => handleDecision("reject")}
              disabled={acting}
              className="bg-red-600 text-white px-3 py-1.5 rounded disabled:opacity-50"
            >
              Reject
            </button>
          </div>
        </div>
      )}

      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left border-b">
            <th className="py-2">File</th>
            <th>Status</th>
            <th>Tokens</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {Object.values(status.files).map((f) => (
            <tr key={f.path} className="border-b">
              <td className="py-2">{f.path}</td>
              <td>{f.status}</td>
              <td>{f.tokens}</td>
              <td>${f.cost_usd.toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="text-sm text-gray-600">Total cost: ${status.cost_used_usd.toFixed(4)}</p>

      {status.done && (
        <div className="border border-green-400 bg-green-50 rounded p-4">
          <p className="font-medium">Done.</p>
          {status.pr_url ? (
            <a href={status.pr_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">
              View pull request
            </a>
          ) : (
            <p className="text-sm text-gray-600">No files were migrated — no PR opened.</p>
          )}
        </div>
      )}
    </main>
  );
}

export default function TaskPage() {
  return (
    <Suspense fallback={<p className="p-8">Loading...</p>}>
      <TaskView />
    </Suspense>
  );
}
```

- [x] **Step 2: Build to verify it compiles**

Run: `cd frontend && npm run build`
Expected: succeeds, produces `frontend/out/task.html` (or `out/task/index.html`, depending on trailing-slash config — either is fine, S3+CloudFront serves both once `frontend.tf` sets `default_root_object`/error handling correctly in Task 5).

- [x] **Step 3: Commit**

```bash
git add frontend/app/task/page.tsx
git commit -m "feat: task status page — polling, interrupt/approve UI, PR link"
```

---

### Task 5: Terraform — S3 + CloudFront (OAC) for the frontend

**Files:**
- Create: `infra/frontend.tf`

**Interfaces:**
- Produces: `aws_s3_bucket.frontend`, `aws_cloudfront_distribution.frontend`, outputs `dashboard_url`, `dashboard_bucket_name`, `dashboard_distribution_id`.

- [x] **Step 1: Write frontend.tf**

```hcl
# infra/frontend.tf
resource "aws_s3_bucket" "frontend" {
  bucket = "repomodernizer-frontend-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "repomod-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# CloudFront+OAC uses S3's REST API origin, not the S3 website-hosting endpoint --
# it does NOT auto-resolve an extensionless request path like "/task" to "task.html"
# the way S3 website hosting would. Next.js static export produces task.html for
# the /task route. A CloudFront Function rewrites the viewer-request URI before
# it hits the origin: "/task" -> "/task.html", "/" -> "/index.html". Without this,
# navigating straight to /task?id=X (exactly what the home page's redirect does)
# would 403/404 against S3.
resource "aws_cloudfront_function" "url_rewrite" {
  name    = "repomod-frontend-url-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "append .html to extensionless paths for Next.js static export"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;

      if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
      } else if (!uri.includes('.')) {
        request.uri = uri + '.html';
      }
      return request;
    }
  EOT
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment              = "repomod-frontend"

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "frontend-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods          = ["GET", "HEAD"]
    target_origin_id       = "frontend-s3"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.url_rewrite.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontServicePrincipal"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.frontend.arn
        }
      }
    }]
  })
}

output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "dashboard_bucket_name" {
  value = aws_s3_bucket.frontend.bucket
}

output "dashboard_distribution_id" {
  value = aws_cloudfront_distribution.frontend.id
}
```

- [x] **Step 2: Plan and apply**

Run: `cd infra && terraform plan -var="budget_alert_email=<your-email>"` — review: S3 bucket, OAC, CloudFront distribution, bucket policy, three outputs, nothing touching existing resources.
Run: `terraform apply -var="budget_alert_email=<your-email>"` (CloudFront distributions take 5-15 minutes to fully deploy — the apply itself returns once creation is accepted, not once it's globally propagated).

Note: the unscoped plan also surfaced pre-existing drift on `aws_ecs_task_definition.worker`/`aws_lambda_function.api` (local `api_image_tag`/`worker_image_tag` vars default to `:initial`, live state has the real deployed git-SHA tag from CI) — unrelated to this task. Applied with `-target` scoped to only the 6 frontend resources to avoid touching those.

- [x] **Step 3: Verify**

Run: `aws s3api get-bucket-policy-status --bucket $(terraform output -raw dashboard_bucket_name) --query 'PolicyStatus.IsPublic'` → `false` (bucket itself is not public — only reachable via CloudFront).
Run: `aws cloudfront get-distribution --id $(terraform output -raw dashboard_distribution_id) --query 'Distribution.Status'` → eventually `"Deployed"` (may still say `"InProgress"` right after apply — that's expected, wait a few minutes before Task 7's live check).

- [x] **Step 4: Commit**

```bash
git add infra/frontend.tf
git commit -m "feat: S3 + CloudFront (OAC) for the static dashboard"
```

---

### Task 6: CORS on API Gateway

**Files:**
- Modify: `infra/apigateway.tf`

**Interfaces:**
- Modifies: `aws_apigatewayv2_api.main` gains a `cors_configuration` block.

- [x] **Step 1: Add the CORS block**

```hcl
# infra/apigateway.tf — change the aws_apigatewayv2_api.main resource to:
resource "aws_apigatewayv2_api" "main" {
  name          = "repomod-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["https://${aws_cloudfront_distribution.frontend.domain_name}"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type"]
  }
}
```

(Everything else in the file is unchanged.)

- [x] **Step 2: Plan and apply**

Run: `cd infra && terraform plan -var="budget_alert_email=<your-email>"` — should show only `aws_apigatewayv2_api.main` changing in place (CORS added), nothing destroyed.
Run: `terraform apply -var="budget_alert_email=<your-email>"`

Note: unscoped plan also showed the same pre-existing `:initial` image-tag drift as Task 5 (unrelated). Applied with `-target=aws_apigatewayv2_api.main` to avoid it.

- [x] **Step 3: Verify**

Run: `curl -s -X OPTIONS https://6yncgq73gk.execute-api.us-east-1.amazonaws.com/tasks -H "Origin: https://$(cd infra && terraform output -raw dashboard_url | sed 's|https://||')" -H "Access-Control-Request-Method: POST" -i | grep -i access-control-allow-origin` → the CloudFront origin echoed back.

Found + fixed during verification: the API's single `$default` route forwards OPTIONS straight to Lambda instead of letting API Gateway auto-answer preflight, so the app previously 405'd on OPTIONS. Fixed with a generic `@app.options("/{full_path:path}")` handler in `app/main.py` returning an empty 200 (API Gateway injects the CORS headers onto it regardless, same as it does on error responses) — see commit `20339ff`. User-approved as an out-of-plan fix; both this and the CORS terraform change reviewed clean.

- [x] **Step 4: Commit**

```bash
git add infra/apigateway.tf
git commit -m "feat: enable CORS on API Gateway for the dashboard's client-side origin"
```

---

### Task 7: First manual deploy and live browser-equivalent verification

**Files:** none — this task is pure verification/deployment, no new files.

- [x] **Step 1: Build and deploy**

```bash
cd frontend
npm run build
aws s3 sync out/ s3://$(cd ../infra && terraform output -raw dashboard_bucket_name) --delete
aws cloudfront create-invalidation \
  --distribution-id $(cd ../infra && terraform output -raw dashboard_distribution_id) \
  --paths "/*"
cd ..
```

- [x] **Step 2: Verify the static site serves correctly**

Run: `curl -s -o /dev/null -w "%{http_code}\n" $(cd infra && terraform output -raw dashboard_url)/` → `200`.
Run: `curl -s $(cd infra && terraform output -raw dashboard_url)/ | grep -o "RepoModernizer"` → prints `RepoModernizer` (confirms the real page content is being served, not a CloudFront error page).
Run: `curl -s -o /dev/null -w "%{http_code}\n" "$(cd infra && terraform output -raw dashboard_url)/task?id=anything"` → `200` (**this is the CloudFront Function URI-rewrite fix — if it 403s or 404s, the rewrite didn't attach correctly; re-check `function_association` on the cache behavior before continuing**).

- [x] **Step 3: Drive one real migration through the dashboard's own API calls**

This replicates exactly what clicking through the UI does, without needing an actual browser — the same `fetch()` calls the page makes, hitting the same real API:

```bash
API_URL=https://6yncgq73gk.execute-api.us-east-1.amazonaws.com

curl -s -X POST "$API_URL/tasks" -H 'content-type: application/json' \
  -H "Origin: $(cd infra && terraform output -raw dashboard_url)" \
  -d '{"repo_url":"https://github.com/akashpersetti/repomodernizer-demo-target","goal":"Migrate this Flask app to FastAPI with async route handlers.","test_command":"pytest -q"}' \
  | tee /tmp/dash_create.json

TASK_ID=$(jq -r .task_id /tmp/dash_create.json)

until curl -s "$API_URL/tasks/$TASK_ID" -H "Origin: $(cd infra && terraform output -raw dashboard_url)" \
  | tee /tmp/dash_status.json | jq -e '.done or .awaiting_approval' > /dev/null; do sleep 5; done
cat /tmp/dash_status.json

FILE=$(jq -r '.awaiting_approval.path // empty' /tmp/dash_status.json)
if [ -n "$FILE" ]; then
  curl -s -X POST "$API_URL/tasks/$TASK_ID/approve" -H 'content-type: application/json' \
    -H "Origin: $(cd infra && terraform output -raw dashboard_url)" \
    -d "{\"file\": \"$FILE\", \"decision\": \"approve\"}"
  until curl -s "$API_URL/tasks/$TASK_ID" -H "Origin: $(cd infra && terraform output -raw dashboard_url)" \
    | tee /tmp/dash_status.json | jq -e '.done' > /dev/null; do sleep 5; done
fi
cat /tmp/dash_status.json
```

- [x] **Step 4: Confirm `pr_url` is populated**

Run: `jq -r .pr_url /tmp/dash_status.json` → a real `https://github.com/akashpersetti/repomodernizer-demo-target/pull/N` URL, not `null`. This is the Task 1 fix proving out for real, not just in the unit test.

Result: `pr_url` = `https://github.com/akashpersetti/repomodernizer-demo-target/pull/4`, confirmed also via `gh pr view 4` (open, correct title/branch, `webapp.py` Flask→FastAPI diff). Also found + fixed a real backend gap during this step: OPTIONS preflight was 405ing (see Task 6's Step 3 note) — fixed and reverified live before this run.

- [x] **Step 5: Actually open the dashboard in a real browser once**

Navigate to the `dashboard_url` output, submit the same form, watch it through — the CLI steps above prove the API contract works; this step confirms the UI itself renders and wires up correctly (loading states, the diff `<pre>` block, button disable states) — something no curl script can verify. Note anything visually broken and fix before moving on.

**Note:** no interactive browser/screenshot tool was available in this execution environment (no Playwright MCP connected) — this step could not be performed as literally an interactive click-through. Substituted with: static-markup verification of both routes (fetched raw HTML — confirmed exact form fields/labels/default values/button on `/`, confirmed `/task?id=...` serves its Suspense "Loading..." shell), plus Step 3's full API-contract-driven migration exercising every state transition the `TaskView` component renders for (loading → awaiting_approval with real diff → approve → done with PR link), since the component's source was read directly and its rendering for each `TaskStatus` shape is known. This is a real gap against what the plan asked for — flagged here rather than claimed as done.

---

### Task 8: Frontend CI/CD

**Files:**
- Modify: `infra/github_oidc.tf`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Modifies: `github_deploy_perms` gains S3 write + CloudFront invalidation permissions for the frontend bucket/distribution. `deploy.yml` gains a `frontend` job.

- [x] **Step 1: Extend the IAM policy**

```hcl
# infra/github_oidc.tf — add these two statements inside the existing
# data "aws_iam_policy_document" "github_deploy_perms" block, alongside the others
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::repomodernizer-frontend-*", "arn:aws:s3:::repomodernizer-frontend-*/*"]
  }
  statement {
    actions   = ["cloudfront:CreateInvalidation"]
    resources = ["*"]
  }
```

- [x] **Step 2: Plan and apply**

Run: `cd infra && terraform plan -var="budget_alert_email=<your-email>"` — should show only `aws_iam_role_policy.github_deploy_perms` changing in place.
Run: `terraform apply -var="budget_alert_email=<your-email>"`

Note: same pre-existing image-tag drift as Tasks 5/6 present in the unscoped plan (by this point already resolved live by the deploy that landed after Task 6, so drift had actually cleared — applied with `-target=aws_iam_role_policy.github_deploy_perms` anyway to stay consistent/minimal). Verified live via `aws iam get-role-policy`.

- [x] **Step 3: Add the frontend job to deploy.yml**

```yaml
# .github/workflows/deploy.yml — add this job after the existing `deploy` job
  frontend:
    needs: deploy
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
        working-directory: frontend
      - run: npm run build
        working-directory: frontend
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: us-east-1
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init
        working-directory: infra
      - id: tf
        run: |
          echo "bucket=$(terraform output -raw dashboard_bucket_name)" >> "$GITHUB_OUTPUT"
          echo "dist_id=$(terraform output -raw dashboard_distribution_id)" >> "$GITHUB_OUTPUT"
        working-directory: infra
      - run: aws s3 sync out/ s3://${{ steps.tf.outputs.bucket }} --delete
        working-directory: frontend
      - run: aws cloudfront create-invalidation --distribution-id ${{ steps.tf.outputs.dist_id }} --paths "/*"
```

- [x] **Step 4: Verify with a real merge**

Commit this task's changes, push a branch, open a real PR against `main` on `akashpersetti/repo-modernizer`, confirm `test`/`plan` pass in the Actions tab, merge, confirm `deploy` then `frontend` both run and succeed. Then re-run Task 7 Step 2's curl checks against the dashboard URL to confirm the CI-deployed build serves correctly.

Result: PR #2 (`frontend-cicd-verify` → `main`), `test`+`plan` both SUCCESS (run 30465831214), merged (17bd8fb). Post-merge run 30466350809: `test` (19s) + `deploy` (2m17s) + `frontend` (46s, first real run of this job — npm build, s3 sync, cloudfront invalidation) all green. Re-verified `/` → 200 + "RepoModernizer" content, `/task?id=anything` → 200.

- [x] **Step 5: Commit**

```bash
git add infra/github_oidc.tf .github/workflows/deploy.yml
git commit -m "feat: wire frontend build+deploy into CI/CD"
```

---

### Task 9: Demo recording script

**Files:**
- Create: `docs/demo_script.md`

- [x] **Step 1: Write the script**

```markdown
# RepoModernizer Demo Script

Recording checklist for the demo GIF/video (~90 seconds).

1. Open the live dashboard: <dashboard_url — from `terraform output -raw dashboard_url` in infra/>
2. Fill the form:
   - Repo URL: `https://github.com/akashpersetti/repomodernizer-demo-target`
   - Goal: `Migrate this Flask app to FastAPI with async route handlers.`
   - Test command: `pytest -q`
3. Click "Start migration" — page redirects to `/task?id=<task_id>`
4. Wait for the risk-gate interrupt to appear (yellow "Awaiting approval" panel with the real diff shown inline)
5. Click "Approve"
6. Wait for status to reach "Done" — green panel appears with a "View pull request" link
7. Click through to the real PR on GitHub, show the diff there too

## Capture

macOS: Cmd+Shift+5 (or QuickTime Player → New Screen Recording), trim to the steps above, convert to GIF:

```bash
ffmpeg -i recording.mov -vf "fps=12,scale=1000:-1" demo.gif
```

or, for better quality/smaller size:

```bash
gifski --fps 12 -o demo.gif recording.mov
```

Save the result as `docs/demo.gif` — the README embeds it from there.
```

- [x] **Step 2: Commit**

```bash
git add docs/demo_script.md
git commit -m "docs: demo recording script"
```

---

### Task 10: README rewrite

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite it**

```markdown
# RepoModernizer

![demo](docs/demo.gif)

Autonomous repository modernization agent. Give it a GitHub repo and a goal ("Flask → FastAPI"),
and it plans a file-by-file migration, rewrites each file, runs the target repo's own test suite
after every change, pauses for human approval on risky diffs, survives crashes mid-migration,
fails over across model providers, enforces a hard cost cap, and opens a real pull request —
end to end, deployed on AWS.

**Live dashboard:** <dashboard_url>
**Live API:** <api_url>

## What this proves

| Scarce signal | How this project proves it |
|---|---|
| Autonomous execution (not chat) | The agent runs an unattended multi-step migration to completion |
| Durability / crash-recovery | LangGraph checkpointer → DynamoDB; a fresh Fargate task resumes from the last committed step, verified with a real kill-mid-run test (`tests/test_crash_recovery.py`) |
| Human-in-the-loop | `interrupt()` before applying high-risk diffs; approve/reject via the dashboard or the API |
| Fault tolerance | Bounded retries with backoff + Bedrock model failover, verified live |
| Cost governance | Per-task token+dollar budget with a hard stop |
| Guardrails | Path allowlist, no destructive ops, diff validation, forbidden-file list |
| Production deploy | API Gateway + Lambda, SQS-triggered Fargate worker with an EFS-backed workspace, Terraform, GitHub Actions OIDC — no long-lived AWS keys anywhere |
| Zero standing cost | No NAT Gateway, no ALB, no EC2, everything scales to zero when idle |

## Architecture

```
Browser (S3 + CloudFront, static Next.js export)
   │ client-side fetch, CORS
   ▼
API Gateway → Lambda (FastAPI + Mangum) — validates, enqueues to SQS, reads DynamoDB for status
                    │
                    ▼
              SQS (repomod-tasks)
                    │
        Lambda consumer → ecs:RunTask (one task per message)
                    │
                    ▼
        Fargate worker (one-shot, EFS-mounted workspace)
        clone → LangGraph agent (plan → migrate_file loop → finalize) → PR
                    │
        DynamoDB (checkpoints, survives across separate task runs)
```

Full design history: [`docs/superpowers/specs/`](docs/superpowers/specs/) and [`docs/superpowers/plans/`](docs/superpowers/plans/) — every sub-project's design doc and implementation plan, including the real bugs found and fixed at each stage.

## Run it yourself

The live dashboard above is the easiest way. To run locally instead:

```bash
uv sync
cp .env.example .env   # fill in AWS credentials with Bedrock access
uv run repomod run --repo ./fixtures/sample_repo --goal "Flask to FastAPI async" --test-cmd "pytest -q"
```

Or run the full local service (sub-project 2's path, no AWS infra beyond Bedrock+DynamoDB):

```bash
DDB_TABLE_CHECKPOINTS=repomod-checkpoints ./scripts/create_checkpoints_table.sh   # once
uv run uvicorn app.main:app --reload
```

```bash
curl -s -X POST localhost:8000/tasks -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/<you>/repomodernizer-demo-target","goal":"Flask to FastAPI async","test_command":"pytest -q"}' | jq

curl -s localhost:8000/tasks/<task_id> | jq
```

**Crash-recovery demo:** start a task, kill the process mid-run, restart, then `POST /tasks/<task_id>/resume` — it continues from the last checkpoint rather than restarting. See `tests/test_crash_recovery.py` for the automated proof.

## Tests

```bash
.venv/bin/python -m pytest -q                                    # fast suite, no network
RUN_LIVE_BEDROCK_TESTS=1 .venv/bin/python -m pytest -q -s         # + live migration against fixtures/sample_repo
RUN_LIVE_GITHUB_TESTS=1 DEMO_REPO_URL=<url> .venv/bin/python -m pytest tests/test_github_live.py -v -s
```

## Frontend dev

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000, talks to the real deployed API by default
```

## Cost

Deliberately no NAT Gateway, no ALB, no EC2, no standing Fargate service — everything scales to
zero. AWS Budgets tripwires at $5/$10. See `RepoModernizer-Spec.md` §8b for the full zero-idle-cost
design.
```

Fill in the literal `<dashboard_url>`/`<api_url>` placeholders with the real values from `terraform output` before committing — this is the one spot in the whole plan where a placeholder is correct, since the real values only exist after Task 5/7's apply.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README rewrite — demo, live links, full architecture, cost story"
```

---

### Task 11: Final live verification

**Files:** none.

- [ ] **Step 1: Confirm everything is actually live**

```bash
curl -s -o /dev/null -w "%{http_code}\n" $(cd infra && terraform output -raw dashboard_url)/
curl -s $(cd infra && terraform output -raw api_url)/health
```
Expected: `200` and `{"status":"ok"}`.

- [ ] **Step 2: Full test suite one more time**

Run: `.venv/bin/python -m pytest -q`
Expected: full suite passes.

- [ ] **Step 3: Confirm git is clean and everything is pushed**

Run: `git status --short` (expect empty) and `git log origin/main..main --oneline` (expect empty).

- [ ] **Step 4: Record the demo**

Follow `docs/demo_script.md`, save the result as `docs/demo.gif`, commit it, confirm it renders in the README on GitHub.

```bash
git add docs/demo.gif
git commit -m "docs: add demo GIF"
git push
```
