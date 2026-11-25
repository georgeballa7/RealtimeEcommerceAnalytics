resource "aws_iam_role" "phase7_dbt_orchestrator" {
  name = "ecommerce-phase7-dbt-orchestrator-demo"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
  tags = {
    Project   = "RealtimeEcommerceAnalytics"
    CostCenter = "demo"
    ManagedBy = "temporary-operator-bootstrap"
  }
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "phase7_dbt_orchestrator" {
  name = "completion-gated-dbt-ecs-and-dynamodb-only"
  role = aws_iam_role.phase7_dbt_orchestrator.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], Resource = aws_dynamodb_table.phase5_controller.arn },
      { Effect = "Allow", Action = ["ecs:ListTasks", "ecs:DescribeTasks", "ecs:DescribeTaskDefinition"], Resource = "*" },
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = "arn:aws:ecs:${var.aws_region}:${var.aws_account_id}:task-definition/ecommerce-phase6-dbt-${var.environment}:*", Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.ecommerce.arn } } },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.ecs_execution.arn, aws_iam_role.phase6_dbt_task[0].arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
      { Effect = "Allow", Action = ["ec2:DescribeRouteTables", "ec2:DescribeNatGateways"], Resource = "*" }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "phase7_dbt_orchestrator_logs" {
  role = aws_iam_role.phase7_dbt_orchestrator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role" "phase6_redshift_loader" {
  name = "ecommerce-phase6-redshift-loader-demo"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "phase6_redshift_loader" {
  name = "phase6-read-silver-write-redshift"
  role = aws_iam_role.phase6_redshift_loader.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "ReadPublishedSilver", Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.landing.arn}/warehouse/shopify_snapshots/v1/*"] },
      { Sid = "ListPublishedSilver", Effect = "Allow", Action = ["s3:ListBucket"], Resource = [aws_s3_bucket.landing.arn], Condition = { StringLike = { "s3:prefix" = ["warehouse/shopify_snapshots/v1/", "warehouse/shopify_snapshots/v1/*"] } } },
      { Sid = "WarehouseDataApi", Effect = "Allow", Action = ["redshift-data:ExecuteStatement", "redshift-data:BatchExecuteStatement", "redshift-data:DescribeStatement", "redshift-data:GetStatementResult"], Resource = "*" },
      { Sid = "RedshiftDbCredentials", Effect = "Allow", Action = ["redshift-serverless:GetCredentials"], Resource = ["arn:aws:redshift-serverless:${var.aws_region}:${var.aws_account_id}:workgroup/*"] },
      { Sid = "WarehouseCopyRole", Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.redshift_s3_copy[0].arn },
      { Sid = "SerializeWarehouseLoads", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = aws_dynamodb_table.phase5_controller.arn }
    ]
  })
}

resource "aws_iam_role_policy" "phase6_loader_eventbridge_publish" {
  name = "phase7-silver-loader-asynchronous-success-eventbridge-destination"
  role = aws_iam_role.phase6_redshift_loader.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "events:PutEvents", Resource = "arn:aws:events:${var.aws_region}:${var.aws_account_id}:event-bus/default" }]
  })
}

resource "aws_iam_role_policy_attachment" "phase6_redshift_loader_logs" {
  role = aws_iam_role.phase6_redshift_loader.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

locals {
  phase7_dbt_event_rules = {
    reconcile = {
      name = "ecommerce-phase7-dbt-reconcile-demo"
      schedule = "rate(5 minutes)"
      pattern = null
    }
    ecs_task_stopped = {
      name = "ecommerce-phase7-dbt-task-stopped-demo"
      schedule = null
      pattern = jsonencode({
        source = ["aws.ecs"]
        "detail-type" = ["ECS Task State Change"]
        detail = { lastStatus = ["STOPPED"], group = ["family:ecommerce-phase6-dbt-demo"] }
      })
    }
    loader_success = {
      name = "ecommerce-phase7-loader-success-dbt-demo"
      schedule = null
      pattern = jsonencode({
        source = ["lambda"]
        "detail-type" = ["Lambda Function Invocation Result - Success"]
        detail = { requestContext = { functionArn = [{ prefix = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:ecommerce-phase6-silver-redshift-loader-demo" }] } }
      })
    }
  }
}

resource "aws_cloudwatch_event_rule" "phase7_dbt" {
  for_each = local.phase7_dbt_event_rules
  name = each.value.name
  state = "ENABLED"
  schedule_expression = each.value.schedule
  event_pattern = each.value.pattern
}

resource "aws_cloudwatch_event_target" "phase7_dbt" {
  for_each = local.phase7_dbt_event_rules
  target_id = "dbt-reconciler"
  rule = aws_cloudwatch_event_rule.phase7_dbt[each.key].name
  arn = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:ecommerce-phase7-dbt-orchestrator-demo"
}

resource "aws_lambda_permission" "phase7_dbt_events" {
  for_each = local.phase7_dbt_event_rules
  statement_id = "allow-${each.value.name}"
  action = "lambda:InvokeFunction"
  function_name = "ecommerce-phase7-dbt-orchestrator-demo"
  principal = "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.phase7_dbt[each.key].arn
}

resource "aws_secretsmanager_secret" "shopify_client_metadata" {
  name = "ecommerce/demo/shopify/client-credentials"
  description = "Shopify API client credentials for automatic token generation"
  lifecycle { prevent_destroy = true }
}
