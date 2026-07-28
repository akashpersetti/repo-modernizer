resource "aws_sqs_queue" "tasks" {
  name                       = "repomod-tasks"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
}
