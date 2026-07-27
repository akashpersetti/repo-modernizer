# RepoModernizer — Autonomous Repository Modernization Agent

A durable, human-gated coding agent. You give it a GitHub repo URL and a modernization goal ("Flask → FastAPI", "add type hints", "Python 3.8 → 3.12", "requests → httpx async"). It plans a file-by-file migration, rewrites each file, runs the test suite after every change, pauses for your approval on risky diffs, survives crashes and resumes from its last checkpoint, fails over across model providers, enforces a hard cost cap, and opens a PR with a full step-by-step trace.

**Resume line it earns:** *"Built a durable long-horizon coding agent on AWS with per-step DynamoDB checkpointing, human-in-the-loop approval gates, provider failover, and cost governance; resumes mid-migration after failure and opens auto-generated PRs."*

---

## 1. Why this fills the gap

| Scarce 2026 signal | How this project proves it |
|---|---|
| Autonomous execution (not chat) | Agent runs an unattended multi-step migration to completion |
| Durability / crash-recovery | LangGraph checkpointer → DynamoDB; `/resume` replays from last committed step |
| Human-in-the-loop | `interrupt()` before applying high-risk diffs; approve/reject via endpoint |
| Fault tolerance | Bounded retries with backoff + Bedrock-primary / fallback-provider failover |
| Cost governance | Per-task token+dollar budget with a hard stop |
| Guardrails | Path allowlist, no destructive ops, diff validation, forbidden-file list, escalation |
| Production deploy | API Gateway + Lambda front door, Fargate worker, Terraform, GitHub Actions OIDC |

Everything is in your existing stack. Nothing new to learn — you're assembling FastAPI, LangGraph, Bedrock, DynamoDB, S3, Terraform, and the GitHub API into one artifact.

---

## 2. Architecture

```
                       ┌─────────────────────────────────────────┐
  client / dashboard   │              AWS                          │
        │              │                                           │
        ▼              │   API Gateway ──► Lambda (FastAPI, Mangum)│
  POST /tasks ─────────┼──►  • validates + enqueues                │
  GET  /tasks/{id}     │        │                                  │
  POST /tasks/{id}/approve      ▼                                  │
  POST /tasks/{id}/resume     SQS (task queue)                     │
        ▲              │        │                                  │
        │              │        ▼                                  │
        │              │   ECS Fargate worker (long-running)       │
        │              │     • runs LangGraph migration graph      │
        │              │     • checkpoints every step ──► DynamoDB │
   matrix + trace ─────┼─────  • writes diffs/logs   ──► S3         │
                       │     • clone + PR            ──► GitHub API │
                       └─────────────────────────────────────────┘
```

**Why split Lambda + Fargate:** Lambda's 15-minute ceiling can't hold a long migration. The API stays serverless (fast, cheap, scales to zero); the actual graph runs on a Fargate worker pulled from SQS. This split is itself a strong architecture-design signal in interviews.

If you want to ship a v0 faster, you *can* run the whole thing on one Fargate service with FastAPI serving directly and an in-process background task — but lead with the queue-based version on your resume.

**Cost invariant — nothing bills while idle.** Every component in this diagram either scales to zero (Lambda, Fargate task count = 0 between jobs) or is pay-per-use with no hourly floor (API Gateway, SQS, DynamoDB on-demand, S3, Bedrock). There is deliberately **no ALB and no NAT Gateway** in this architecture — those are the only pieces that would bill continuously. See §8b for the full zero-standing-cost design; it is a hard requirement, not an optimization.

---

## 3. The LangGraph migration graph

State machine (each node checkpoints to DynamoDB on exit):

1. **ingest** — shallow-clone repo into the worker's ephemeral volume, read `goal` + `options`.
2. **plan** — LLM analyzes the tree, emits an ordered list of target files + rationale + per-file risk score. Checkpoint the plan.
3. **migrate_file** (loop, one file per iteration — this is the long horizon):
   - read file + minimal dependency context
   - generate a unified diff toward the goal
   - **guardrail gate** — reject diffs touching forbidden paths, deleting files, or exceeding size caps
   - **risk gate** — if risk ≥ threshold, `interrupt()` and wait for human approval
   - apply diff on a working branch
   - run the test command in a subprocess
   - if tests fail → bounded retry with the error fed back in; if the model call itself errors → provider failover
   - checkpoint (file status, tokens, cost) to DynamoDB
   - **budget gate** — if cumulative cost > cap, hard-stop and finalize early
4. **finalize** — push the branch, open a PR via GitHub API with a summary table + per-file trace; write full artifacts to S3.

Use LangGraph's `interrupt` + a DynamoDB-backed checkpointer so `/resume` and `/approve` both re-enter the graph at the exact saved node.

---

## 4. Folder structure

```
repomodernizer/
├── app/
│   ├── main.py                 # FastAPI app + Mangum handler
│   ├── api/
│   │   ├── routes_tasks.py     # POST/GET /tasks, /approve, /resume
│   │   └── routes_health.py
│   ├── agent/
│   │   ├── graph.py            # LangGraph graph definition + wiring
│   │   ├── nodes.py            # ingest / plan / migrate_file / finalize
│   │   ├── state.py            # TypedDict graph state
│   │   ├── checkpointer.py     # DynamoDB checkpointer
│   │   ├── guardrails.py       # path allowlist, destructive-op + size checks
│   │   ├── risk.py             # per-diff risk scoring
│   │   └── providers.py        # Bedrock primary + failover + retry/backoff
│   ├── services/
│   │   ├── github.py           # clone, branch, commit, open PR
│   │   ├── tests_runner.py     # subprocess test execution + parsing
│   │   ├── budget.py           # token/dollar accounting + hard stop
│   │   ├── storage_s3.py       # artifact read/write
│   │   └── registry.py         # DynamoDB task registry CRUD
│   ├── worker/
│   │   └── consumer.py         # SQS poll loop → run graph
│   ├── models/schemas.py       # Pydantic request/response models
│   └── config.py               # pydantic-settings, reads .env
├── dashboard/                  # simple React matrix view (files × status)
├── infra/                      # Terraform
│   ├── main.tf  variables.tf  outputs.tf
│   ├── vpc.tf                  # public subnet, NO NAT Gateway, S3+DDB gateway endpoints
│   ├── dynamodb.tf  s3.tf  sqs.tf  lambda.tf  fargate.tf  iam.tf
│   ├── ecr.tf                  # image repo + keep-latest-3 lifecycle policy
│   ├── budgets.tf             # $5 alert + $10 ceiling (zero-cost tripwires)
│   ├── logs.tf                # CloudWatch log groups, retention_in_days = 7
│   └── github_oidc.tf
├── tests/
│   ├── test_guardrails.py  test_risk.py  test_budget.py
│   ├── test_routes.py
│   └── fixtures/sample_repo/   # tiny repo to migrate in CI
├── .github/workflows/deploy.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 5. `.env` handling

Commit `.env.example` only; never the real `.env`. Load with `pydantic-settings`; in AWS, inject real values from SSM Parameter Store / Secrets Manager, not from a file.

`.env.example`:
```
# --- App ---
ENV=local
LOG_LEVEL=INFO

# --- AWS ---
AWS_REGION=us-east-1
DDB_TABLE_TASKS=repomod-tasks
DDB_TABLE_CHECKPOINTS=repomod-checkpoints
S3_BUCKET_ARTIFACTS=repomod-artifacts-<youraccountid>
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<acct>/repomod-tasks

# --- Models (Bedrock primary, failover secondary) ---
BEDROCK_MODEL_PRIMARY=anthropic.claude-... 
PROVIDER_FALLBACK_ENABLED=true
FALLBACK_API_KEY=            # only if using a non-Bedrock fallback

# --- GitHub ---
GITHUB_APP_TOKEN=            # fine-grained PAT or GitHub App installation token
GITHUB_DEFAULT_BASE_BRANCH=main

# --- Governance ---
MAX_TASK_COST_USD=2.00
MAX_FILE_RETRIES=2
RISK_APPROVAL_THRESHOLD=0.6
FORBIDDEN_PATHS=.github/,migrations/,*.lock
```

`.gitignore` must include `.env`, `*.pem`, `.venv/`, `__pycache__/`, `dashboard/node_modules/`.

---

## 6. API endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/tasks` | Start a migration. Body: `{repo_url, goal, test_command, options}` → returns `task_id` |
| GET | `/tasks/{id}` | Status + matrix (files × status, tokens, cost) |
| GET | `/tasks/{id}/trace` | Full per-step trace (from S3) |
| POST | `/tasks/{id}/approve` | `{file, decision: approve|reject, note}` — resumes an interrupted task |
| POST | `/tasks/{id}/resume` | Resume a crashed/stopped task from last checkpoint |
| GET | `/health` | Liveness |

`POST /tasks` request model:
```json
{
  "repo_url": "https://github.com/owner/repo",
  "goal": "Migrate Flask routes to FastAPI with async handlers",
  "test_command": "pytest -q",
  "options": { "include_tests": false, "max_files": 40 }
}
```

---

## 7. DynamoDB schema

**Tasks table** (`repomod-tasks`) — single-table, task registry + per-file rows:
- PK `TASK#{task_id}`, SK `META` → status, goal, repo, cost_used, created_at
- PK `TASK#{task_id}`, SK `FILE#{path}` → file status (pending/migrated/approved/rejected/failed/skipped), tokens, cost, retry_count
- GSI on `status` for a "running tasks" view.

**Checkpoints table** (`repomod-checkpoints`) — LangGraph checkpointer:
- PK `TASK#{task_id}`, SK `CKPT#{step_seq}` → serialized graph state + next node. `/resume` reads the latest SK.

Enable point-in-time recovery on both. TTL on checkpoints (e.g. 14 days) to control storage.

---

## 8. Guardrails, risk, and cost (the parts that impress)

- **Input guardrails:** repo-URL allowlist/denylist, max repo size, max file count, forbidden paths from `FORBIDDEN_PATHS`.
- **Output guardrails:** every generated diff is parsed and validated before apply — reject file deletions, reject writes outside the target file, reject diffs over a size cap, reject any hunk touching a forbidden path.
- **Risk scoring:** cheap heuristic (lines changed, presence of auth/db/security-sensitive tokens, whether tests exist for the file) → 0–1 score. `≥ RISK_APPROVAL_THRESHOLD` forces `interrupt()`.
- **Failover:** wrap model calls with bounded retry + exponential backoff; on repeated failure, switch to the fallback provider and log the switch in the trace.
- **Cost governance:** `budget.py` accumulates tokens→dollars per task; before each file, if projected cost > `MAX_TASK_COST_USD`, hard-stop, finalize with a partial PR, mark remaining files `skipped`.

---

## 8b. Zero-standing-cost design (hard requirement)

The goal: **$0 billed while the system sits deployed and idle.** The only money you ever spend is per-run — model tokens plus a few seconds of Fargate compute — and that is capped. Achieving this is entirely a matter of *what you provision* and *what you deliberately do not*. Follow every rule below; each maps to a specific charge you're eliminating.

### Things you must NOT create
| Do not provision | Why — what it would cost |
|---|---|
| **NAT Gateway** | ~$32/mo standing + $0.045/GB. The single biggest trap. Eliminated by running Fargate in a **public subnet with `assign_public_ip = true`**, plus free S3 + DynamoDB **gateway VPC endpoints** for AWS-API traffic. |
| **Application Load Balancer** | ~$22/mo standing. Not needed — the API is API Gateway + Lambda, and the worker is pulled from SQS. No load balancer anywhere. |
| **EC2 / NAT instance / Elastic IP left unattached** | Hourly or idle-IP charges. This is a fully serverless + on-demand-container design; no EC2 at all. |
| **Provisioned-capacity DynamoDB** | Reserved throughput bills hourly. Use **on-demand (pay-per-request)** mode on both tables. |
| **DynamoDB Global Tables / streams to Lambda you don't need** | Extra replicated write + stream costs. Single-region only. |

### Things you must configure to scale to zero / stay in free tier
- **Fargate task count = 0 when idle.** The worker is *not* a long-lived service. Either run it as a **one-off task launched per job** (ECS `RunTask` triggered from the queue), or an ECS service with **desired count 0** that scales up on queue depth and back to 0 when the queue drains. No running task = no compute bill.
- **Lambda** — scales to zero natively; free tier covers 1M requests/mo. You'll use hundreds.
- **API Gateway** — HTTP API (cheaper than REST API), pay-per-request, no hourly floor. First 1M/mo effectively free-tier territory at your volume.
- **SQS** — 1M requests/mo free. You'll use thousands.
- **S3** — lifecycle rule to **expire artifacts after 30 days**; you'll hold a few MB. Cents at most, and prevents slow accretion.
- **DynamoDB** — on-demand; 25 GB storage free-tier. Add a **TTL of 14 days on the checkpoints table** (already in §7) so old state auto-deletes at no cost.
- **CloudWatch Logs** — set **`retention_in_days = 7`** on every log group. Never leave the default (never-expire), which slowly accrues storage charges.
- **ECR** — one small image; 500 MB/mo is free-tier. Add a lifecycle policy to **keep only the latest 3 images** so old layers don't accumulate.
- **Bedrock** — pure pay-per-token, no standing fee. Tokens are your only meaningful cost, bounded by `MAX_TASK_COST_USD` per run.

### Belt-and-suspenders: make overspend impossible
- **AWS Budgets alert at $5** (email + optional SNS) created *in Terraform* on day one. Zero-cost to have; catches anything unexpected immediately.
- **Per-task cost cap** already enforced in-app (`MAX_TASK_COST_USD`, §8).
- **`aws_budgets_budget` with a $10 hard ceiling** as a second tripwire.
- **Tag every resource** `project = repomodernizer` so Cost Explorer shows this project's spend in isolation and you can verify "$0 idle" is actually true.
- **A `make teardown` target** (`terraform destroy`) so that between demo sessions you can tear the whole stack down to literal zero and re-apply in minutes when you need it. Since there's no standing cost, you don't *need* to — but it's the ultimate guarantee, and re-provisioning is a one-command CI step.

### Net result
- **Idle, deployed, doing nothing:** $0.00/mo. Nothing in the stack has an hourly floor.
- **Per full migration run:** roughly $0.01–0.02 Fargate compute + model tokens (a few cents to under the $2 cap). A whole build-and-demo phase realistically lands in the **$5–25 range, essentially all tokens** — and not one dollar of that is wasted on idle infrastructure.

---

## 8c. Container architecture — build for amd64, not arm64 (Mac trap)

You're on Apple Silicon, so `docker build` defaults to **`linux/arm64`**. If the image architecture and the Fargate task definition's `cpuArchitecture` don't match, the container fails to start with an `exec format error` — a confusing failure that looks like a code bug but isn't. Pick one architecture and make it consistent everywhere. **Standardize on `amd64` (x86_64)** unless you deliberately choose Graviton/arm end-to-end.

Anything that gets packaged into an image and shipped to AWS must be built for the target platform, explicitly:

- **Dockerfile build** — always pass the platform, never rely on the host default:
  ```bash
  docker build --platform linux/amd64 -t repomod-worker .
  ```
  For multi-arch safety, use `docker buildx build --platform linux/amd64 --push ...`.
- **Base image** — pin a linux/amd64-compatible base (e.g. `python:3.12-slim`); it resolves per-platform, so the `--platform` flag is what actually controls the output.
- **Fargate task definition** (`fargate.tf`) — set `runtime_platform` explicitly to match:
  ```hcl
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"   # must match the image you push
  }
  ```
- **Lambda** (the FastAPI API packaged as a container image or zip) — same rule. If you ship the Lambda as a container image, build it `--platform linux/amd64` and set the function's `architectures = ["x86_64"]`. If any native wheels are involved (e.g. `pydantic-core`), a zip built on your Mac will pull arm64 wheels — build the Lambda artifact in an amd64 context (CI, or `--platform linux/amd64` in Docker) so the wheels match.
- **CI is the clean fix** — GitHub Actions runners are amd64 by default, so building and pushing the image *in CI* rather than from your laptop sidesteps the whole mismatch. Local `--platform linux/amd64` builds are for when you want to test before pushing.

Rule of thumb: **the `--platform` flag on every build and the `cpu_architecture` in Terraform must always say the same thing.** Decide `amd64` once, write it in both places, and the Mac default can't bite you.

> If you later want the ~20% cheaper Graviton path, you *can* go arm64 end-to-end — build `--platform linux/arm64` **and** set `cpu_architecture = "ARM64"`. Just never mix them. For a portfolio project, amd64 is the safer default because every example, base image, and wheel you'll copy assumes it.

---

## 9. Testing the endpoints locally

Run the API and worker locally against LocalStack (or real AWS dev resources):

```bash
uvicorn app.main:app --reload            # terminal 1: API
python -m app.worker.consumer            # terminal 2: worker

# start a task
curl -s -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/you/sample-flask-app",
       "goal":"Flask to FastAPI async",
       "test_command":"pytest -q",
       "options":{"include_tests":false,"max_files":10}}' | jq

# poll status / matrix
curl -s localhost:8000/tasks/<task_id> | jq

# approve a risky file when it interrupts
curl -s -X POST localhost:8000/tasks/<task_id>/approve \
  -H 'content-type: application/json' \
  -d '{"file":"app/routes.py","decision":"approve","note":"looks correct"}' | jq

# simulate crash-recovery: kill the worker mid-run, restart, then
curl -s -X POST localhost:8000/tasks/<task_id>/resume | jq
```

The crash-recovery demo (kill worker → `/resume` → it continues from the exact file) is your interview money shot. Record it.

---

## 9b. Key Terraform: `vpc.tf` and `fargate.tf`

The two files that actually *enforce* the $0-idle guarantee (§8b) and the amd64 rule (§8c). Everything else in `infra/` is standard; these two are where the traps get closed in code.

### `infra/vpc.tf` — NAT-free, public-subnet VPC with free gateway endpoints

```hcl
data "aws_availability_zones" "available" { state = "available" }

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { project = "repomodernizer" }
}

# Public subnets only — no private subnets, so no reason for a NAT Gateway.
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags = { project = "repomodernizer" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { project = "repomodernizer" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id   # egress via IGW, NOT a NAT Gateway
  }
  tags = { project = "repomodernizer" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---- Free gateway VPC endpoints: keep S3 + DynamoDB traffic off the internet
#      at zero cost (gateway endpoints have no hourly charge, unlike interface endpoints).
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags = { project = "repomodernizer" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags = { project = "repomodernizer" }
}

# NOTE: deliberately NO aws_nat_gateway and NO aws_eip anywhere in this stack.
# The Fargate task reaches ECR/Bedrock over its public IP via the IGW; S3 and
# DynamoDB go through the free gateway endpoints above.

resource "aws_security_group" "worker" {
  name        = "repomod-worker"
  description = "Egress-only for the Fargate worker"
  vpc_id      = aws_vpc.main.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { project = "repomodernizer" }
}
```

### `infra/fargate.tf` — amd64 task, launched on demand (desired count 0 when idle)

```hcl
resource "aws_ecs_cluster" "main" {
  name = "repomod"
  tags = { project = "repomodernizer" }
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "repomod-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"   # 1 vCPU
  memory                   = "2048"   # 2 GB
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # THE amd64 RULE (§8c): must match the --platform linux/amd64 image you push.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.worker.repository_url}:latest"
    essential = true
    environment = [
      { name = "DDB_TABLE_TASKS",       value = aws_dynamodb_table.tasks.name },
      { name = "DDB_TABLE_CHECKPOINTS", value = aws_dynamodb_table.checkpoints.name },
      { name = "S3_BUCKET_ARTIFACTS",   value = aws_s3_bucket.artifacts.bucket },
      { name = "SQS_QUEUE_URL",         value = aws_sqs_queue.tasks.url }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
  tags = { project = "repomodernizer" }
}

# No aws_ecs_service with a standing desired_count. The worker is launched
# per job via RunTask (from the queue consumer / a small launcher Lambda),
# so task count is 0 whenever nothing is running = $0 idle compute.
# When you launch it, pass the public subnets + SG and enable a public IP:
#
#   network_configuration {
#     subnets          = aws_subnet.public[*].id
#     security_groups  = [aws_security_group.worker.id]
#     assign_public_ip = true          # required in a public subnet, no NAT
#   }
```

The two lines that make or break the guarantees: **`gateway_id = aws_internet_gateway.igw.id`** (never a NAT Gateway) in `vpc.tf`, and **`cpu_architecture = "X86_64"`** in `fargate.tf` matching your `--platform linux/amd64` build. Keep both and the $0-idle + no-arch-mismatch promises hold.

---

## 10. GitHub repository setup

1. **Init & protect.** Create the repo, protect `main` (require PR + passing CI). Add a clean README with the architecture diagram from §2 and a 30-second demo GIF at the top.
2. **Secrets via OIDC — no long-lived keys.** Configure GitHub Actions OIDC to assume an AWS deploy role (`github_oidc.tf`). Store `GITHUB_APP_TOKEN` and any fallback key in repo secrets / AWS Secrets Manager, never in code.
3. **CI on PR** (`.github/workflows/deploy.yml`):
   - lint (ruff) + typecheck (mypy) + `pytest` against `fixtures/sample_repo`
   - `terraform plan` on PR, `terraform apply` on merge to `main`
   - build + push the worker container image, update the Fargate service
4. **PR-to-deploy** matches your existing workflow: merge = deploy.
5. **Commits:** conventional commits, small and reviewable — the repo itself is a work sample.
6. **README must show, not tell:** the demo GIF, the reliability feature table (§1), one architecture diagram, and a "Run it yourself" section. Recruiters skim; make the durability + human-in-the-loop story unmissable in the first screen.

---

## 11. Build order (so it's demoable fast)

1. Graph + nodes running **locally, in-process**, migrating `fixtures/sample_repo` — no AWS yet.
2. Add guardrails, risk gate, `interrupt()` approval, cost cap — prove the reliability story on your laptop.
3. Add the DynamoDB checkpointer + `/resume`; demo crash-recovery.
4. Wrap in FastAPI; add GitHub clone + PR.
5. Terraform: **vpc.tf first** (public subnet, no NAT, gateway endpoints), then DynamoDB, S3, SQS, Lambda, Fargate, IAM, OIDC, budgets.tf, logs.tf, ecr.tf.
6. Split API (Lambda) from worker (Fargate via SQS); wire CI/CD. **Build/push all images `--platform linux/amd64` and set `cpu_architecture = "X86_64"` (§8c) — you're on a Mac, so this won't happen by default.**
7. Build the matrix dashboard, record the demo GIF, polish the README.
8. **Verify $0 idle:** deploy, run one migration, then leave it 48h untouched and check Cost Explorer (filtered by tag `project = repomodernizer`). Idle cost must read $0.00. If anything nonzero shows, it's almost certainly a NAT Gateway or a running Fargate task — hunt it down before you move on.

Ship steps 1–3 first — that alone is a stronger portfolio piece than anything in that carousel. Steps 4–8 turn it into the production artifact that actually clears the 2026 screen.
