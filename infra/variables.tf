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
