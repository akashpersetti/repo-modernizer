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
