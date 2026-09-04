resource "aws_ecs_cluster" "main" {
  name = "repomod"
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/repomod-worker"
  retention_in_days = 30
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
