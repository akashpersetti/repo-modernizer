# Infra + Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy RepoModernizer to AWS for real — Terraform infra (VPC, DynamoDB, SQS, ECR, Lambda, Fargate, EFS, IAM, budgets), the Lambda(API)/Fargate(worker) split via SQS, and a real GitHub Actions OIDC CI/CD pipeline.

**Architecture:** API Gateway → Lambda (FastAPI+Mangum, container image) validates and enqueues to SQS, returning immediately. An SQS-triggered consumer Lambda calls `ecs:RunTask` once per message. A one-shot Fargate task (`app/worker/entrypoint.py`) does the actual clone/graph-invoke/finalize work, using an EFS-mounted volume so the git workspace survives across separate task invocations for the same `task_id` (start → interrupt → later approve/resume are genuinely separate containers).

**Tech Stack:** Terraform (AWS provider ~5.0), Docker (amd64), boto3, Mangum, GitHub Actions OIDC.

## Global Constraints

- Every resource tagged `project = repomodernizer` (via provider `default_tags`) so Cost Explorer can isolate spend.
- No NAT Gateway, no ALB, no EC2, no provisioned-capacity DynamoDB, no ECS service with a standing `desired_count` — per spec §8b.
- Every Docker build uses `--platform linux/amd64`; every task/function's `cpu_architecture`/`architectures` says `X86_64` — per spec §8c.
- CloudWatch log group `retention_in_days = 7` everywhere logs are produced (set inline on each log group resource — no separate `logs.tf`, there's no shared logic to centralize).
- `GITHUB_APP_TOKEN` lives in SSM Parameter Store (SecureString), referenced via the ECS task definition's `secrets` block — never a plain Terraform variable baked into `environment`, matching spec §5's "inject from SSM/Secrets Manager, not a file."
- AWS account: `914697327092`, region `us-east-1`. GitHub repo for OIDC trust: `akashpersetti/repo-modernizer` (this project's own repo, distinct from the `repomodernizer-demo-target` repo being migrated).
- Every `docker buildx build ... --push` MUST include `--provenance=false --sbom=false`. Without them, modern buildx attaches a provenance/SBOM attestation by default, turning a single-platform image into an OCI image *index* — Lambda's `CreateFunction`/`UpdateFunctionCode` API rejects that outright with "image manifest, config or layer media type ... is not supported," even though the image itself is perfectly valid amd64. Found live in Task 7 (see the `aws ecr batch-get-image ... imageManifest` verification below, added to confirm `mediaType` is `application/vnd.docker.distribution.manifest.v2+json`, not `application/vnd.oci.image.index.v1+json`).

---

### Task 1: Bootstrap Terraform backend, providers, variables

**Files:**
- Create: `scripts/bootstrap_tf_backend.sh`
- Create: `infra/versions.tf`
- Create: `infra/backend.tf`
- Create: `infra/variables.tf`
- Create: `.gitignore` (extend)

**Interfaces:**
- Produces: an S3 bucket + DynamoDB lock table for Terraform's own state, and the `infra/` directory's provider/variable scaffolding every later task builds on.

- [x] **Step 1: Write and run the backend bootstrap script**

```bash
#!/usr/bin/env bash
# scripts/bootstrap_tf_backend.sh
set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="repomodernizer-tfstate-${ACCOUNT_ID}"
REGION="${AWS_REGION:-us-east-1}"

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=project,Value=repomodernizer}]'

aws dynamodb create-table \
  --table-name repomod-tf-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" \
  --tags Key=project,Value=repomodernizer

aws dynamodb wait table-exists --table-name repomod-tf-lock --region "$REGION"
echo "Backend bucket: ${BUCKET}"
echo "Lock table: repomod-tf-lock"
```

Run: `chmod +x scripts/bootstrap_tf_backend.sh && ./scripts/bootstrap_tf_backend.sh`
Verify: `aws s3api head-bucket --bucket repomodernizer-tfstate-914697327092` exits 0; `aws dynamodb describe-table --table-name repomod-tf-lock --query 'Table.TableStatus'` prints `"ACTIVE"`.

- [x] **Step 2: Write versions.tf, backend.tf, variables.tf**

```hcl
# infra/versions.tf
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      project = "repomodernizer"
    }
  }
}

data "aws_caller_identity" "current" {}
```

```hcl
# infra/backend.tf
terraform {
  backend "s3" {
    bucket         = "repomodernizer-tfstate-914697327092"
    key            = "repomodernizer/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "repomod-tf-lock"
    encrypt        = true
  }
}
```

```hcl
# infra/variables.tf
variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "github_org" {
  type    = string
  default = "akashpersetti"
}

variable "github_repo" {
  type    = string
  default = "repo-modernizer"
}

variable "bedrock_model_primary" {
  type    = string
  default = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "bedrock_model_fallback" {
  type    = string
  default = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "budget_alert_email" {
  type = string
}

variable "api_image_tag" {
  type    = string
  default = "initial"
}

variable "worker_image_tag" {
  type    = string
  default = "initial"
}
```

```
# .gitignore — add these lines
infra/.terraform/
infra/*.tfstate*
infra/.terraform.lock.hcl
infra/consumer_handler.zip
```

- [x] **Step 3: Initialize Terraform**

Run: `cd infra && terraform init`
Verify: output ends with `Terraform has been successfully initialized!` and shows the S3 backend configured (not local state).

- [x] **Step 4: Commit**

```bash
git add scripts/bootstrap_tf_backend.sh infra/versions.tf infra/backend.tf infra/variables.tf .gitignore
git commit -m "feat: bootstrap Terraform S3 backend, provider and variable scaffolding"
```

---

### Task 2: VPC and EFS

**Files:**
- Create: `infra/vpc.tf`
- Create: `infra/efs.tf`

**Interfaces:**
- Produces: `aws_vpc.main`, `aws_subnet.public[*]`, `aws_security_group.worker`, `aws_vpc_endpoint.s3`/`.dynamodb`, `aws_efs_file_system.workspace`, `aws_efs_access_point.workspace`.

- [x] **Step 1: Write vpc.tf (spec §9b, verbatim — already reviewed and correct)**

```hcl
# infra/vpc.tf
data "aws_availability_zones" "available" { state = "available" }

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
}

# NOTE: deliberately NO aws_nat_gateway and NO aws_eip anywhere in this stack.

resource "aws_security_group" "worker" {
  name        = "repomod-worker"
  description = "Fargate worker: egress-only, plus self-referencing NFS for EFS"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "worker_nfs" {
  type                     = "ingress"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  security_group_id        = aws_security_group.worker.id
  source_security_group_id = aws_security_group.worker.id
}
```

- [x] **Step 2: Write efs.tf**

```hcl
# infra/efs.tf
resource "aws_efs_file_system" "workspace" {
  encrypted = true
}

resource "aws_efs_mount_target" "workspace" {
  count           = length(aws_subnet.public)
  file_system_id  = aws_efs_file_system.workspace.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.worker.id]
}

resource "aws_efs_access_point" "workspace" {
  file_system_id = aws_efs_file_system.workspace.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/workspace"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }
}
```

- [x] **Step 3: Plan and apply**

Run: `cd infra && terraform plan -var="budget_alert_email=<your-email>"` — review the plan (should show only VPC/subnet/IGW/route/endpoints/SG/EFS resources, nothing else yet).
Run: `terraform apply -var="budget_alert_email=<your-email>"`

- [x] **Step 4: Verify**

Run: `aws ec2 describe-vpcs --filters "Name=tag:project,Values=repomodernizer" --query 'Vpcs[0].State'` → `"available"`.
Run: `aws efs describe-file-systems --query "FileSystems[?Tags[?Key=='project']].LifeCycleState" --output text` → `available`.
Run: `aws efs describe-mount-targets --file-system-id <id-from-above> --query 'MountTargets[*].LifeCycleState'` → both `["available", "available"]` (may take a minute after apply).

- [x] **Step 5: Commit**

```bash
git add infra/vpc.tf infra/efs.tf
git commit -m "feat: NAT-free VPC with gateway endpoints, EFS for cross-task workspace persistence"
```

---

### Task 3: DynamoDB, SQS, ECR

**Files:**
- Create: `infra/dynamodb.tf`
- Create: `infra/sqs.tf`
- Create: `infra/ecr.tf`

**Interfaces:**
- Produces: `aws_dynamodb_table.checkpoints`, `aws_sqs_queue.tasks`, `aws_ecr_repository.api`/`.worker`.

- [x] **Step 1: Write the three files**

```hcl
# infra/dynamodb.tf
resource "aws_dynamodb_table" "checkpoints" {
  name         = "repomod-checkpoints"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
```

```hcl
# infra/sqs.tf
resource "aws_sqs_queue" "tasks" {
  name                       = "repomod-tasks"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
}
```

```hcl
# infra/ecr.tf
resource "aws_ecr_repository" "api" {
  name                 = "repomod-api"
  image_tag_mutability = "MUTABLE"
}

resource "aws_ecr_repository" "worker" {
  name                 = "repomod-worker"
  image_tag_mutability = "MUTABLE"
}

locals {
  keep_last_3_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy     = local.keep_last_3_policy
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy     = local.keep_last_3_policy
}
```

- [x] **Step 2: Import the existing checkpoints table, then plan and apply**

Run: `cd infra && terraform import aws_dynamodb_table.checkpoints repomod-checkpoints`
Run: `terraform plan -var="budget_alert_email=<your-email>"` — should show **no changes** for `aws_dynamodb_table.checkpoints` (schema matches what's already live) and **create** for the SQS queue and two ECR repos.
Run: `terraform apply -var="budget_alert_email=<your-email>"`

- [x] **Step 3: Verify**

Run: `aws sqs get-queue-attributes --queue-url $(aws sqs get-queue-url --queue-name repomod-tasks --query QueueUrl --output text) --attribute-names VisibilityTimeout --query 'Attributes.VisibilityTimeout'` → `"900"`.
Run: `aws ecr describe-repositories --repository-names repomod-api repomod-worker --query 'repositories[*].repositoryName'` → both present.

- [x] **Step 4: Commit**

```bash
git add infra/dynamodb.tf infra/sqs.tf infra/ecr.tf
git commit -m "feat: DynamoDB checkpoints table (imported), SQS task queue, ECR repos"
```

---

### Task 4: IAM roles

**Files:**
- Create: `infra/iam.tf`

**Interfaces:**
- Produces: `aws_iam_role.api_lambda`, `.consumer_lambda`, `.ecs_task_execution`, `.ecs_task` with their policies.

**Note:** `aws_iam_policy_document.consumer_lambda_perms`'s `ecs:RunTask` resource is written as an interpolated ARN string (`arn:aws:ecs:...task-definition/repomod-worker:*`), not a direct reference to `aws_ecs_task_definition.worker.arn` — that resource is declared in Task 7, which comes later. Using the interpolated string here avoids a forward-reference across tasks; the ARN pattern matches the task definition family name we've already fixed (`repomod-worker`), so it resolves correctly once that resource exists.

- [x] **Step 1: Write iam.tf**

```hcl
# infra/iam.tf
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ecs_task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---- API Lambda ----
resource "aws_iam_role" "api_lambda" {
  name               = "repomod-api-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "api_lambda_basic" {
  role       = aws_iam_role.api_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "api_lambda_perms" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.checkpoints.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.tasks.arn]
  }
}

resource "aws_iam_role_policy" "api_lambda_perms" {
  name   = "repomod-api-lambda-perms"
  role   = aws_iam_role.api_lambda.id
  policy = data.aws_iam_policy_document.api_lambda_perms.json
}

# ---- Consumer Lambda ----
resource "aws_iam_role" "consumer_lambda" {
  name               = "repomod-consumer-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "consumer_lambda_basic" {
  role       = aws_iam_role.consumer_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "consumer_lambda_perms" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.tasks.arn]
  }
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/repomod-worker:*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_task_execution.arn, aws_iam_role.ecs_task.arn]
  }
}

resource "aws_iam_role_policy" "consumer_lambda_perms" {
  name   = "repomod-consumer-lambda-perms"
  role   = aws_iam_role.consumer_lambda.id
  policy = data.aws_iam_policy_document.consumer_lambda_perms.json
}

# ---- ECS task execution role (pulls image, writes logs, reads SSM secret) ----
resource "aws_iam_role" "ecs_task_execution" {
  name               = "repomod-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_task_execution_ssm" {
  statement {
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/repomodernizer/*"]
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_ssm" {
  name   = "repomod-ecs-task-execution-ssm"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_ssm.json
}

# ---- ECS task role (app runtime permissions: DynamoDB, Bedrock, EFS) ----
resource "aws_iam_role" "ecs_task" {
  name               = "repomod-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

data "aws_iam_policy_document" "ecs_task_perms" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.checkpoints.arn]
  }
  statement {
    actions   = ["bedrock:InvokeModel"]
    resources = ["*"]
  }
  statement {
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [aws_efs_file_system.workspace.arn]
  }
}

resource "aws_iam_role_policy" "ecs_task_perms" {
  name   = "repomod-ecs-task-perms"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_perms.json
}
```

- [x] **Step 2: Plan and apply**

Run: `cd infra && terraform plan -var="budget_alert_email=<your-email>"` — should show only the new IAM roles/policies/attachments.
Run: `terraform apply -var="budget_alert_email=<your-email>"`

- [x] **Step 3: Verify**

Run: `aws iam get-role --role-name repomod-api-lambda --query 'Role.RoleName'` → `"repomod-api-lambda"`. Repeat for `repomod-consumer-lambda`, `repomod-ecs-task-execution`, `repomod-ecs-task`.

- [x] **Step 4: Commit**

```bash
git add infra/iam.tf
git commit -m "feat: IAM roles for API/consumer Lambdas and ECS task execution/runtime"
```

---

### Task 5: App code rework — stateless worker entrypoint, thin API routes

**Files:**
- Modify: `app/agent/state.py`
- Create: `app/worker/consumer_handler.py`
- Create: `app/worker/entrypoint.py`
- Create: `app/lambda_handler.py`
- Modify: `app/api/routes_tasks.py`
- Modify: `pyproject.toml` (add `mangum`)
- Delete: `app/worker/runner.py` (superseded — its logic is split between `entrypoint.py` and `routes_tasks.py`)
- Test: `tests/test_entrypoint.py`
- Test: `tests/test_consumer_handler.py`
- Test: `tests/test_routes.py` (rewritten)
- Delete: `tests/test_runner.py` (tested code no longer exists)

**Interfaces:**
- Modifies: `GraphState` gains `repo_url: str`, `branch: str`, `base_branch: str`.
- Produces: `app.worker.entrypoint.run() -> None` (reads `ACTION`/`TASK_ID`/etc. from `os.environ`); `app.worker.consumer_handler.handler(event, context) -> dict`; `app.lambda_handler.handler` (Mangum ASGI adapter).

- [x] **Step 1: Write the failing tests**

```python
# tests/test_state.py  (new, small — pins the GraphState field addition)
from app.agent.state import GraphState


def test_graph_state_has_repo_context_fields():
    state: GraphState = {
        "task_id": "t", "repo_path": "/tmp/x", "goal": "g", "test_command": "true",
        "plan": [], "files": {}, "cursor": 0, "cost_used_usd": 0.0, "trace": [],
        "repo_url": "https://github.com/x/y", "branch": "repomodernizer/t", "base_branch": "main",
    }
    assert state["repo_url"] == "https://github.com/x/y"
```

```python
# tests/test_entrypoint.py
import json
import os

from langgraph.checkpoint.memory import MemorySaver

from app.agent.budget import BudgetTracker
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.worker import entrypoint


class FakeProviderRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt):
        text = self.responses[self.calls]
        self.calls += 1
        return text, 100, 50, "bedrock-primary"


def _make_bare_remote(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "webapp.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True)
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "-q", "-m", "init"],
        cwd=seed, check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
    return bare


def test_start_then_separate_approve_survives_fresh_container(tmp_path, monkeypatch):
    """The exact scenario that motivated EFS: 'start' and 'approve' run as two
    separate calls (simulating two separate containers) sharing only a checkpointer
    and an on-disk workspace root -- not any in-memory object."""
    import app.worker.entrypoint as ep

    remote = _make_bare_remote(tmp_path)
    checkpointer = MemorySaver()
    pr_calls = []
    monkeypatch.setattr(ep.github, "push_branch", lambda *a, **k: None)
    monkeypatch.setattr(ep.github, "open_pull_request", lambda *a, **k: pr_calls.append(a) or "https://x/pull/1")

    task_id = "entrypoint-test-1"
    responses_start = [
        json.dumps([{"path": "webapp.py", "rationale": "t", "risk_score": 0.9}]),
        "x = 2\n",
    ]

    def deps_factory():
        return NodeDeps(
            providers=FakeProviderRouter(responses_start), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        )

    env = {
        "ACTION": "start", "TASK_ID": task_id, "REPO_URL": str(remote),
        "GOAL": "bump x", "TEST_COMMAND": "true", "WORKSPACE_ROOT": str(tmp_path / "workspace_root"),
    }
    monkeypatch.setattr(os, "environ", {**os.environ, **env})
    ep.run(checkpointer_factory=lambda: checkpointer, deps_factory=deps_factory, github_token="")

    # "approve" now runs as a fresh call with a fresh NodeDeps/FakeProviderRouter
    # (simulating a brand new container) -- only the checkpointer and the
    # WORKSPACE_ROOT filesystem path are shared, exactly as EFS provides in prod.
    env2 = {
        "ACTION": "approve", "TASK_ID": task_id, "DECISION": "approve", "NOTE": "",
        "WORKSPACE_ROOT": str(tmp_path / "workspace_root"),
    }
    monkeypatch.setattr(os, "environ", {**os.environ, **env2})
    ep.run(
        checkpointer_factory=lambda: checkpointer,
        deps_factory=lambda: NodeDeps(
            providers=FakeProviderRouter([]), budget=BudgetTracker(cap_usd=10.0),
            forbidden_paths=[], max_diff_lines=400, risk_threshold=0.6, max_retries=2,
            estimated_cost_per_file=0.01,
        ),
        github_token="",
    )

    graph = build_graph(deps_factory(), checkpointer=checkpointer)
    snapshot = graph.get_state({"configurable": {"thread_id": task_id}})
    assert snapshot.values["files"]["webapp.py"]["status"] == "approved"
    assert len(pr_calls) == 1
```

```python
# tests/test_consumer_handler.py
import json
from unittest.mock import MagicMock, patch

from app.worker.consumer_handler import handler


@patch("app.worker.consumer_handler._ecs")
def test_handler_calls_run_task_per_message(mock_ecs, monkeypatch):
    monkeypatch.setenv("ECS_CLUSTER", "repomod")
    monkeypatch.setenv("ECS_TASK_DEFINITION", "repomod-worker")
    monkeypatch.setenv("SUBNET_IDS", "subnet-1,subnet-2")
    monkeypatch.setenv("SECURITY_GROUP_ID", "sg-1")

    event = {
        "Records": [
            {"body": json.dumps({"action": "start", "task_id": "abc123", "repo_url": "https://x/y"})}
        ]
    }

    result = handler(event, None)

    assert result == {"statusCode": 200}
    mock_ecs.run_task.assert_called_once()
    call_kwargs = mock_ecs.run_task.call_args.kwargs
    assert call_kwargs["cluster"] == "repomod"
    assert call_kwargs["launchType"] == "FARGATE"
    env_overrides = call_kwargs["overrides"]["containerOverrides"][0]["environment"]
    assert {"name": "ACTION", "value": "start"} in env_overrides
    assert {"name": "TASK_ID", "value": "abc123"} in env_overrides
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_state.py tests/test_entrypoint.py tests/test_consumer_handler.py -v`
Expected: FAIL — `app.worker.entrypoint`/`app.worker.consumer_handler` don't exist yet; `GraphState` doesn't have the new fields (TypedDict fields aren't runtime-checked, but the test itself constructs a dict literal, so it'll only fail once `entrypoint.py` actually reads `state["repo_url"]` and similar — the real signal here is the `ModuleNotFoundError` for the two new modules).

- [x] **Step 3: Update state.py, write entrypoint.py, consumer_handler.py, lambda_handler.py; rewrite routes_tasks.py; delete runner.py**

```python
# app/agent/state.py — GraphState gains three fields
from typing import Literal, Optional, TypedDict


class FileResult(TypedDict):
    path: str
    status: Literal["pending", "migrated", "approved", "rejected", "failed", "skipped"]
    tokens: int
    cost_usd: float
    retry_count: int
    last_error: Optional[str]


class PlanEntry(TypedDict):
    path: str
    rationale: str
    risk_score: float


class GraphState(TypedDict):
    task_id: str
    repo_path: str
    goal: str
    test_command: str
    plan: list[PlanEntry]
    files: dict[str, FileResult]
    cursor: int
    cost_used_usd: float
    trace: list[dict]
    repo_url: str
    branch: str
    base_branch: str
```

```python
# app/worker/consumer_handler.py
import json
import os

import boto3

_ecs = boto3.client("ecs")


def handler(event: dict, context) -> dict:
    for record in event["Records"]:
        body = json.loads(record["body"])
        _ecs.run_task(
            cluster=os.environ["ECS_CLUSTER"],
            taskDefinition=os.environ["ECS_TASK_DEFINITION"],
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": os.environ["SUBNET_IDS"].split(","),
                    "securityGroups": [os.environ["SECURITY_GROUP_ID"]],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "worker",
                        "environment": [
                            {"name": str(k).upper(), "value": str(v)}
                            for k, v in body.items() if v is not None
                        ],
                    }
                ]
            },
        )
    return {"statusCode": 200}
```

```python
# app/worker/entrypoint.py
import os
from pathlib import Path

import boto3
from langgraph.types import Command

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.agent.providers import BedrockProvider, ProviderRouter
from app.config import Settings
from app.services import github


def _default_deps_factory(settings: Settings) -> NodeDeps:
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    providers = ProviderRouter(
        BedrockProvider(settings.bedrock_model_primary, "bedrock-primary", client),
        BedrockProvider(settings.bedrock_model_fallback, "bedrock-fallback", client),
    )
    return NodeDeps(
        providers=providers,
        budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=settings.forbidden_paths_list(),
        max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold,
        max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )


def _finalize_if_done(result: dict, token: str) -> None:
    if "__interrupt__" in result:
        return
    if any(f["status"] in ("migrated", "approved") for f in result["files"].values()):
        workspace = Path(result["repo_path"])
        github.commit_all(workspace, f"RepoModernizer: {result['goal']}")
        github.push_branch(workspace, result["branch"], token)
        github.open_pull_request(
            result["repo_url"], result["branch"], result["base_branch"],
            title=f"RepoModernizer: {result['goal']}",
            body="Opened automatically by RepoModernizer.",
            token=token,
        )


def run(checkpointer_factory=None, deps_factory=None, github_token=None) -> None:
    settings = Settings()
    checkpointer = (checkpointer_factory or (lambda: DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)))()
    deps = (deps_factory or (lambda: _default_deps_factory(settings)))()
    token = github_token if github_token is not None else settings.github_app_token

    action = os.environ["ACTION"]
    task_id = os.environ["TASK_ID"]
    workspace_root = Path(os.environ.get("WORKSPACE_ROOT", "/mnt/workspace"))
    graph = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": task_id}}

    if action == "start":
        repo_url = os.environ["REPO_URL"]
        goal = os.environ["GOAL"]
        base_branch = os.environ.get("BASE_BRANCH") or settings.github_default_base_branch
        branch = f"repomodernizer/{task_id}"
        workspace = workspace_root / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        github.clone_repo(repo_url, workspace, token)
        github.create_branch(workspace, branch)
        initial_state = {
            "task_id": task_id, "repo_path": str(workspace), "goal": goal,
            "test_command": os.environ["TEST_COMMAND"], "plan": [], "files": {},
            "cursor": 0, "cost_used_usd": 0.0, "trace": [],
            "repo_url": repo_url, "branch": branch, "base_branch": base_branch,
        }
        result = graph.invoke(initial_state, config=config)
    elif action == "approve":
        result = graph.invoke(
            Command(resume={"decision": os.environ["DECISION"], "note": os.environ.get("NOTE", "")}),
            config=config,
        )
    elif action == "resume":
        result = graph.invoke(None, config=config)
    else:
        raise ValueError(f"unknown action: {action}")

    _finalize_if_done(result, token)


if __name__ == "__main__":
    run()
```

```python
# app/lambda_handler.py
from mangum import Mangum

from app.main import app

handler = Mangum(app)
```

```python
# app/api/routes_tasks.py — full rewrite: enqueue-only for mutating routes,
# read-only graph.get_state() for status
import json
import uuid
from typing import Optional

import boto3
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.budget import BudgetTracker
from app.agent.checkpointer import DynamoDBCheckpointer
from app.agent.graph import build_graph
from app.agent.nodes import NodeDeps
from app.config import Settings

router = APIRouter()
_settings: Optional[Settings] = None
_sqs = None


class CreateTaskRequest(BaseModel):
    repo_url: str
    goal: str
    test_command: str
    base_branch: str = "main"


class CreateTaskResponse(BaseModel):
    task_id: str


class ApproveRequest(BaseModel):
    file: str
    decision: str
    note: str = ""


class TaskStatusResponse(BaseModel):
    task_id: str
    files: dict
    cost_used_usd: float
    awaiting_approval: Optional[dict]
    done: bool


def configure(settings: Settings, sqs_client=None) -> None:
    global _settings, _sqs
    _settings = settings
    _sqs = sqs_client or boto3.client("sqs", region_name=settings.aws_region)


def _get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("routes_tasks not configured — call configure() first")
    return _settings


def _send(message: dict) -> None:
    _sqs.send_message(QueueUrl=_get_settings().sqs_queue_url, MessageBody=json.dumps(message))


@router.post("/tasks", response_model=CreateTaskResponse)
def create_task(request: CreateTaskRequest):
    if not request.repo_url.startswith("https://github.com/"):
        raise HTTPException(status_code=422, detail="repo_url must be a github.com https URL")
    task_id = uuid.uuid4().hex[:8]
    _send({
        "action": "start", "task_id": task_id, "repo_url": request.repo_url,
        "goal": request.goal, "test_command": request.test_command, "base_branch": request.base_branch,
    })
    return CreateTaskResponse(task_id=task_id)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str):
    settings = _get_settings()
    checkpointer = DynamoDBCheckpointer(table_name=settings.ddb_table_checkpoints)
    dummy_deps = NodeDeps(
        providers=None, budget=BudgetTracker(cap_usd=settings.max_task_cost_usd),
        forbidden_paths=[], max_diff_lines=settings.max_diff_lines,
        risk_threshold=settings.risk_approval_threshold, max_retries=settings.max_file_retries,
        estimated_cost_per_file=settings.estimated_cost_per_file_usd,
    )
    graph = build_graph(dummy_deps, checkpointer=checkpointer)
    snapshot = graph.get_state({"configurable": {"thread_id": task_id}})
    awaiting_approval = None
    for task in snapshot.tasks:
        if task.interrupts:
            awaiting_approval = task.interrupts[0].value
    return TaskStatusResponse(
        task_id=task_id,
        files=snapshot.values.get("files", {}),
        cost_used_usd=snapshot.values.get("cost_used_usd", 0.0),
        awaiting_approval=awaiting_approval,
        done=not snapshot.next,
    )


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, request: ApproveRequest):
    _send({"action": "approve", "task_id": task_id, "decision": request.decision, "note": request.note})
    return {"status": "enqueued"}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str):
    _send({"action": "resume", "task_id": task_id})
    return {"status": "enqueued"}
```

Add `sqs_queue_url: str = ""` to `app/config.py`'s `Settings` (small addition alongside this task — the API Lambda's environment provides it via Terraform).

Delete `app/worker/runner.py` and `tests/test_runner.py` (superseded).

Add to `pyproject.toml`'s `dependencies`: `"mangum>=0.17"`. Run `uv sync`.

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_state.py tests/test_entrypoint.py tests/test_consumer_handler.py -v`
Expected: PASS (3 tests, including the EFS-motivating "two separate calls sharing only a checkpointer + filesystem path" scenario).

Run: `.venv/bin/python -m pytest -q` (full suite — `test_routes.py` needs updating for the new `configure()`/enqueue-based contract; adjust its fakes to a fake SQS client with a `send_message` method instead of a fake `TaskRunner`, following the same pattern as `test_entrypoint.py`'s and sub-project 2's route tests).
Expected: PASS, full suite green.

- [x] **Step 5: Commit**

```bash
git add app/agent/state.py app/worker/consumer_handler.py app/worker/entrypoint.py app/lambda_handler.py \
        app/api/routes_tasks.py app/config.py pyproject.toml uv.lock \
        tests/test_state.py tests/test_entrypoint.py tests/test_consumer_handler.py tests/test_routes.py
git rm app/worker/runner.py tests/test_runner.py
git commit -m "feat: stateless worker entrypoint + enqueue-only API routes for the Lambda/Fargate split"
```

---

### Task 6: Docker images

**Files:**
- Create: `Dockerfile.api`
- Create: `Dockerfile.worker`

**Interfaces:**
- Produces: two amd64 container images, pushed to the ECR repos from Task 3.

- [x] **Step 1: Write the Dockerfiles**

```dockerfile
# Dockerfile.api
FROM public.ecr.aws/lambda/python:3.12

COPY pyproject.toml uv.lock ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir uv \
    && cd ${LAMBDA_TASK_ROOT} \
    && uv export --frozen --no-dev -o requirements.txt \
    && pip install --no-cache-dir -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

COPY app ${LAMBDA_TASK_ROOT}/app

CMD ["app.lambda_handler.handler"]
```

```dockerfile
# Dockerfile.worker
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv export --frozen --no-dev -o requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENTRYPOINT ["python", "-m", "app.worker.entrypoint"]
```

- [x] **Step 2: Build both, amd64, and push to ECR**

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

docker buildx build --platform linux/amd64 --provenance=false --sbom=false -f Dockerfile.api -t "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/repomod-api:initial" --push .
docker buildx build --platform linux/amd64 --provenance=false --sbom=false -f Dockerfile.worker -t "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/repomod-worker:initial" --push .
```

- [x] **Step 3: Verify**

Run: `aws ecr describe-images --repository-name repomod-api --query 'imageDetails[*].imageTags'` → shows `["initial"]`.
Run: `aws ecr describe-images --repository-name repomod-worker --query 'imageDetails[*].imageTags'` → shows `["initial"]`.
Run: `docker inspect "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/repomod-api:initial" --format '{{.Architecture}}'` → `amd64` (confirms the amd64 discipline actually held, not just the flag being passed).
Run: `aws ecr batch-get-image --repository-name repomod-api --image-ids imageTag=initial --query 'images[0].imageManifest' --output text | python3 -c "import json,sys; print(json.load(sys.stdin).get('mediaType'))"` → `application/vnd.docker.distribution.manifest.v2+json`, **not** `application/vnd.oci.image.index.v1+json`. If you see the OCI index type, Task 7's Lambda creation will fail — re-run the build with `--provenance=false --sbom=false` (already in the command above) before proceeding.

- [x] **Step 4: Commit**

```bash
git add Dockerfile.api Dockerfile.worker
git commit -m "feat: amd64 container images for API Lambda and Fargate worker"
```

---

### Task 7: Lambda, Fargate, SSM secret

**Files:**
- Create: `infra/lambda.tf`
- Create: `infra/fargate.tf`

**Interfaces:**
- Produces: `aws_lambda_function.api`/`.consumer`, `aws_lambda_event_source_mapping.consumer`, `aws_ecs_cluster.main`, `aws_ecs_task_definition.worker`.

- [x] **Step 1: Put the GitHub token in SSM (one-off, not managed by Terraform)**

```bash
aws ssm put-parameter --name /repomodernizer/github_app_token --type SecureString --value "$(gh auth token)" --overwrite
```
Verify: `aws ssm get-parameter --name /repomodernizer/github_app_token --with-decryption --query 'Parameter.Name'` → the name back (confirms it's readable with your creds; the ECS task execution role's read access was granted in Task 4).

- [x] **Step 2: Write fargate.tf**

```hcl
# infra/fargate.tf
resource "aws_ecs_cluster" "main" {
  name = "repomod"
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/repomod-worker"
  retention_in_days = 7
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "repomod-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "workspace"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.workspace.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.workspace.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
    essential = true
    environment = [
      { name = "DDB_TABLE_CHECKPOINTS",  value = aws_dynamodb_table.checkpoints.name },
      { name = "AWS_REGION",             value = var.aws_region },
      { name = "BEDROCK_MODEL_PRIMARY",  value = var.bedrock_model_primary },
      { name = "BEDROCK_MODEL_FALLBACK", value = var.bedrock_model_fallback },
      { name = "WORKSPACE_ROOT",         value = "/mnt/workspace" }
    ]
    secrets = [
      {
        name      = "GITHUB_APP_TOKEN"
        valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/repomodernizer/github_app_token"
      }
    ]
    mountPoints = [
      { sourceVolume = "workspace", containerPath = "/mnt/workspace" }
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
}
```

- [x] **Step 3: Write lambda.tf**

```hcl
# infra/lambda.tf
resource "aws_cloudwatch_log_group" "api_lambda" {
  name              = "/aws/lambda/repomod-api"
  retention_in_days = 7
}

resource "aws_lambda_function" "api" {
  function_name = "repomod-api"
  role          = aws_iam_role.api_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
  timeout       = 30
  memory_size   = 512

  environment {
    variables = {
      DDB_TABLE_CHECKPOINTS  = aws_dynamodb_table.checkpoints.name
      SQS_QUEUE_URL          = aws_sqs_queue.tasks.url
      BEDROCK_MODEL_PRIMARY  = var.bedrock_model_primary
      BEDROCK_MODEL_FALLBACK = var.bedrock_model_fallback
    }
  }

  depends_on = [aws_cloudwatch_log_group.api_lambda]
}

resource "aws_cloudwatch_log_group" "consumer_lambda" {
  name              = "/aws/lambda/repomod-consumer"
  retention_in_days = 7
}

data "archive_file" "consumer" {
  type        = "zip"
  source_file = "${path.module}/../app/worker/consumer_handler.py"
  output_path = "${path.module}/consumer_handler.zip"
}

resource "aws_lambda_function" "consumer" {
  function_name    = "repomod-consumer"
  role             = aws_iam_role.consumer_lambda.arn
  runtime          = "python3.12"
  handler          = "consumer_handler.handler"
  filename         = data.archive_file.consumer.output_path
  source_code_hash = data.archive_file.consumer.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      ECS_CLUSTER         = aws_ecs_cluster.main.name
      ECS_TASK_DEFINITION = aws_ecs_task_definition.worker.family
      SUBNET_IDS          = join(",", aws_subnet.public[*].id)
      SECURITY_GROUP_ID   = aws_security_group.worker.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.consumer_lambda]
}

resource "aws_lambda_event_source_mapping" "consumer" {
  event_source_arn = aws_sqs_queue.tasks.arn
  function_name    = aws_lambda_function.consumer.arn
  batch_size       = 1
}
```

- [x] **Step 4: Plan and apply**

Run: `cd infra && terraform plan -var="budget_alert_email=<your-email>"` — review: two Lambda functions, one ECS cluster, one task definition, one event source mapping, two log groups.
Run: `terraform apply -var="budget_alert_email=<your-email>"`

- [x] **Step 5: Verify**

Run: `aws lambda get-function --function-name repomod-api --query 'Configuration.State'` → `"Active"`.
Run: `aws lambda get-function --function-name repomod-consumer --query 'Configuration.State'` → `"Active"`.
Run: `aws ecs describe-task-definition --task-definition repomod-worker --query 'taskDefinition.status'` → `"ACTIVE"`.
Run: `aws lambda get-event-source-mapping --uuid $(aws lambda list-event-source-mappings --function-name repomod-consumer --query 'EventSourceMappings[0].UUID' --output text) --query State` → `"Enabled"`.

- [x] **Step 6: Commit**

```bash
git add infra/fargate.tf infra/lambda.tf
git commit -m "feat: API/consumer Lambdas, Fargate task definition with EFS-mounted workspace"
```

---

### Task 8: API Gateway

**Files:**
- Create: `infra/apigateway.tf`

**Interfaces:**
- Produces: `aws_apigatewayv2_api.main`, output `api_url`.

- [x] **Step 1: Write apigateway.tf**

```hcl
# infra/apigateway.tf
resource "aws_apigatewayv2_api" "main" {
  name          = "repomod-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

output "api_url" {
  value = aws_apigatewayv2_api.main.api_endpoint
}
```

- [x] **Step 2: Plan and apply**

Run: `cd infra && terraform apply -var="budget_alert_email=<your-email>"`

- [x] **Step 3: Verify**

Run: `curl -s $(terraform output -raw api_url)/health` → `{"status":"ok"}`. This is the first real end-to-end proof the API Lambda is reachable and running.

- [x] **Step 4: Commit**

```bash
git add infra/apigateway.tf
git commit -m "feat: API Gateway HTTP API fronting the API Lambda"
```

---

### Task 9: Budgets

**Files:**
- Create: `infra/budgets.tf`

**Interfaces:**
- Produces: `aws_budgets_budget.alert` ($5), `.ceiling` ($10).

- [x] **Step 1: Write budgets.tf**

```hcl
# infra/budgets.tf
locals {
  budget_cost_filter = {
    name   = "TagKeyValue"
    values = ["user:project$repomodernizer"]
  }
}

resource "aws_budgets_budget" "alert" {
  name         = "repomodernizer-alert"
  budget_type  = "COST"
  limit_amount = "5"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = local.budget_cost_filter.name
    values = local.budget_cost_filter.values
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

resource "aws_budgets_budget" "ceiling" {
  name         = "repomodernizer-ceiling"
  budget_type  = "COST"
  limit_amount = "10"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = local.budget_cost_filter.name
    values = local.budget_cost_filter.values
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
```

- [x] **Step 2: Plan and apply**

Run: `cd infra && terraform apply -var="budget_alert_email=<your-email>"`

- [x] **Step 3: Verify**

Run: `aws budgets describe-budgets --account-id 914697327092 --query 'Budgets[*].BudgetName'` → both `repomodernizer-alert` and `repomodernizer-ceiling` present.

- [x] **Step 4: Commit**

```bash
git add infra/budgets.tf
git commit -m "feat: \$5 alert and \$10 ceiling AWS Budgets tripwires"
```

---

### Task 10: Live end-to-end verification through the deployed stack

**Files:** none — this task is pure verification, no new files.

- [x] **Step 1: Run a real migration through the deployed API**

```bash
API_URL=$(cd infra && terraform output -raw api_url)

curl -s -X POST "$API_URL/tasks" -H 'content-type: application/json' -d '{
  "repo_url": "https://github.com/akashpersetti/repomodernizer-demo-target",
  "goal": "Migrate this Flask app to FastAPI with async route handlers.",
  "test_command": "pytest -q"
}' | tee /tmp/create_response.json

TASK_ID=$(jq -r .task_id /tmp/create_response.json)
echo "task_id=$TASK_ID"
```

- [x] **Step 2: Poll status, approve if needed**

```bash
until curl -s "$API_URL/tasks/$TASK_ID" | tee /tmp/status.json | jq -e '.done or .awaiting_approval' > /dev/null; do
  sleep 5
done
cat /tmp/status.json

# if awaiting_approval is non-null:
FILE=$(jq -r '.awaiting_approval.path' /tmp/status.json)
curl -s -X POST "$API_URL/tasks/$TASK_ID/approve" -H 'content-type: application/json' \
  -d "{\"file\": \"$FILE\", \"decision\": \"approve\"}"

until curl -s "$API_URL/tasks/$TASK_ID" | tee /tmp/status.json | jq -e '.done' > /dev/null; do
  sleep 5
done
cat /tmp/status.json
```

- [x] **Step 3: Check CloudWatch logs if anything looks wrong**

```bash
aws logs tail /ecs/repomod-worker --since 10m
aws logs tail /aws/lambda/repomod-consumer --since 10m
aws logs tail /aws/lambda/repomod-api --since 10m
```

- [x] **Step 4: Verify the real PR**

Run: `gh pr list --repo akashpersetti/repomodernizer-demo-target --state open` — a new PR should be present, opened by this deployed-stack run (distinct from sub-project 2's PRs, which came from the local path).

**This is the task most likely to surface real bugs** — treat any failure here as a signal to fix code/infra and re-run from Step 1, not as something to work around. Expect IAM permission errors, environment variable name mismatches between `consumer_handler.py`'s uppercasing and `entrypoint.py`'s expected keys, or EFS mount timing issues, based on the realistic-expectation note in the design doc.

---

### Task 11: GitHub Actions OIDC + CI/CD

**Files:**
- Create: `infra/github_oidc.tf`
- Create: `.github/workflows/deploy.yml`

**Interfaces:**
- Produces: `aws_iam_openid_connect_provider.github`, `aws_iam_role.github_deploy`, output `github_deploy_role_arn`.

- [x] **Step 1: Write github_oidc.tf**

```hcl
# infra/github_oidc.tf
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_org}/${var.github_repo}:*"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "repomod-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
}

data "aws_iam_policy_document" "github_deploy_perms" {
  statement {
    actions = [
      "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:PutImage",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:BatchGetImage",
    ]
    resources = ["*"]
  }
  statement {
    actions   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:UpdateFunctionConfiguration"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::repomodernizer-tfstate-*", "arn:aws:s3:::repomodernizer-tfstate-*/*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = ["arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/repomod-tf-lock"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy_perms" {
  name   = "repomod-github-deploy-perms"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy_perms.json
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}
```

- [x] **Step 2: Apply, then set the repo secret**

Run: `cd infra && terraform apply -var="budget_alert_email=<your-email>"`
Run: `gh secret set AWS_DEPLOY_ROLE_ARN --repo akashpersetti/repo-modernizer --body "$(terraform output -raw github_deploy_role_arn)"`
Run: `gh secret set BUDGET_ALERT_EMAIL --repo akashpersetti/repo-modernizer --body "<your-email>"`

- [x] **Step 3: Write the workflow**

```yaml
# .github/workflows/deploy.yml
name: deploy

on:
  pull_request:
  push:
    branches: [main]

permissions:
  id-token: write
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: .venv/bin/python -m pytest -q

  plan:
    needs: test
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: us-east-1
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init
        working-directory: infra
      - run: terraform plan -var="budget_alert_email=${{ secrets.BUDGET_ALERT_EMAIL }}"
        working-directory: infra

  deploy:
    needs: test
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: us-east-1
      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr
      - run: |
          docker buildx build --platform linux/amd64 --provenance=false --sbom=false -f Dockerfile.api \
            -t ${{ steps.ecr.outputs.registry }}/repomod-api:${{ github.sha }} --push .
          docker buildx build --platform linux/amd64 --provenance=false --sbom=false -f Dockerfile.worker \
            -t ${{ steps.ecr.outputs.registry }}/repomod-worker:${{ github.sha }} --push .
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init
        working-directory: infra
      - run: |
          terraform apply -auto-approve \
            -var="budget_alert_email=${{ secrets.BUDGET_ALERT_EMAIL }}" \
            -var="api_image_tag=${{ github.sha }}" \
            -var="worker_image_tag=${{ github.sha }}"
        working-directory: infra
```

- [x] **Step 4: Verify with a real merge**

Commit this task, push a branch, open a real PR against `main` on `akashpersetti/repo-modernizer`, confirm the `test` and `plan` jobs run and pass in the Actions tab, then merge and confirm the `deploy` job runs and succeeds — new image tags pushed, `terraform apply` completes, `curl $API_URL/health` still returns `{"status":"ok"}` afterward.

- [x] **Step 5: Commit**

```bash
git add infra/github_oidc.tf .github/workflows/deploy.yml
git commit -m "feat: GitHub Actions OIDC deploy pipeline — test on PR, build+push+apply on merge to main"
```
