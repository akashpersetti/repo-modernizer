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
