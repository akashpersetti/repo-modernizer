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
