# infra/github_oidc.tf
#
# The GitHub Actions OIDC provider is an AWS-account-global singleton (one
# per account, regardless of which repos trust it). One already exists here,
# owned by a different project's Terraform state (tagged Project=terraform-agent).
# We look it up instead of managing it, so this stack never creates/destroys a
# resource another project's state also believes it owns.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # GitHub's sub claim now embeds numeric org/repo database IDs
      # (repo:OWNER@ORG_ID/REPO@REPO_ID:...), not the classic
      # repo:OWNER/REPO:* format -- confirmed via CloudTrail's
      # principalId on a rejected AssumeRoleWithWebIdentity call.
      values = ["repo:${var.github_org}@*/${var.github_repo}@*:*"]
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
    # DeregisterTaskDefinition: bumping the image tag forces a replace (not
    # in-place revision update) of aws_ecs_task_definition.worker -- found
    # live in Task 11's first real deploy run.
    # TagResource: the provider auto-applies default_tags (project=repomodernizer)
    # to every new task def revision it registers -- found live in the retry.
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:DeregisterTaskDefinition", "ecs:TagResource"]
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
    # test_checkpointer.py round-trips real rows through the live checkpoints
    # table (no mocking) -- the test job needs the same access the API Lambda
    # has, plus UpdateItem for put_run_summary/note_resume (ecs_task's access),
    # plus DeleteItem so test_finalize_stores_pr_url_when_migration_completes
    # can clean up the SUMMARY row it writes (180-day TTL, doesn't self-expire
    # like this test's other rows).
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = ["arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/repomod-checkpoints"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = ["*"]
  }
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::repomodernizer-frontend-*", "arn:aws:s3:::repomodernizer-frontend-*/*"]
  }
  statement {
    actions   = ["cloudfront:CreateInvalidation"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy_perms" {
  name   = "repomod-github-deploy-perms"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy_perms.json
}

# terraform plan/apply refreshes every resource in state before doing
# anything -- that's a read (Describe/Get/List) across every service this
# stack touches (ec2, efs, dynamodb, sqs, iam, logs, apigateway, budgets...),
# not just the narrow write actions above. Found live in Task 11: `plan`
# failed piling up AccessDenied one service at a time (iam:GetRole,
# logs:DescribeLogGroups, sqs:GetQueueAttributes, ec2:DescribeVpcs...).
# ReadOnlyAccess is read-only by definition, so this can't grant any mutation.
resource "aws_iam_role_policy_attachment" "github_deploy_read" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}
