# LIVE (21 Nov 2026): Terraform manages the on-demand NAT controller,
# reserved EIP, and application route tables. Only the lifecycle Lambda owns
# ephemeral NAT Gateways and their runtime default routes. Static mode is an
# explicit, plan-reviewed rollback option; never recreate routes by hand.
data "archive_file" "phase6_nat_lifecycle" {
  count       = local.nat_lifecycle_count
  type        = "zip"
  # Canonicalize Windows CRLF before archiving so the ZIP is platform-stable.
  source_content          = replace(file("${path.module}/../../scripts/aws/phase6_nat_lifecycle_lambda.py"), "\r\n", "\n")
  source_content_filename = "phase6_nat_lifecycle_lambda.py"
  output_path             = "${path.module}/.terraform/phase6_nat_lifecycle.zip"
}

resource "aws_cloudwatch_log_group" "phase6_nat_lifecycle" {
  count             = local.nat_lifecycle_count
  name              = "/aws/lambda/ecommerce-phase6-nat-lifecycle-${var.environment}"
  retention_in_days = 14
}

resource "aws_iam_role" "phase6_nat_lifecycle" {
  count = local.nat_lifecycle_count
  name  = "ecommerce-phase6-nat-lifecycle-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "phase6_nat_logs" {
  count      = local.nat_lifecycle_count
  role       = aws_iam_role.phase6_nat_lifecycle[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "phase6_nat_lifecycle" {
  count = local.nat_lifecycle_count
  name  = "control-only-ephemeral-nat-and-verified-outbound-work"
  role  = aws_iam_role.phase6_nat_lifecycle[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeNatGateways", "ec2:DescribeRouteTables", "ec2:DescribeAddresses"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateNatGateway"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ec2:CreateTags"]
        Resource = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:natgateway/*"
        Condition = {
          StringEquals = { "ec2:CreateAction" = "CreateNatGateway" }
        }
      },
      {
        Effect = "Allow"
        Action = ["ec2:DeleteNatGateway"]
        Resource = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:natgateway/*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/Purpose" = "phase6-dynamic-nat" }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateRoute", "ec2:DeleteRoute"]
        Resource = aws_route_table.application[*].arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.phase5_controller.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = "${aws_dynamodb_table.phase5_events.arn}/index/by_state"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.events.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ecs:ListTasks"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["glue:GetJobRuns"]
        Resource = "arn:aws:glue:${var.aws_region}:${var.aws_account_id}:job/ecommerce-phase6-shopify-silver-${var.environment}"
      },
    ]
  })
}

resource "aws_lambda_function" "phase6_nat_lifecycle" {
  count            = local.nat_lifecycle_count
  function_name    = "ecommerce-phase6-nat-lifecycle-${var.environment}"
  filename         = data.archive_file.phase6_nat_lifecycle[0].output_path
  source_code_hash = data.archive_file.phase6_nat_lifecycle[0].output_base64sha256
  handler          = "phase6_nat_lifecycle_lambda.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.phase6_nat_lifecycle[0].arn
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      NAT_LIFECYCLE_ACTIVE         = tostring(var.nat_gateway_mode == "on_demand")
      # AWS_REGION is set automatically by Lambda and is a reserved environment key.
      NAT_ALLOCATION_ID            = aws_eip.nat[0].id
      NAT_PUBLIC_SUBNET_ID         = aws_subnet.public[0].id
      NAT_VPC_ID                   = aws_vpc.ecommerce.id
      NAT_APPLICATION_ROUTE_TABLES = join(",", aws_route_table.application[*].id)
      CONTROLLER_TABLE             = aws_dynamodb_table.phase5_controller.name
      EVENTS_TABLE                 = aws_dynamodb_table.phase5_events.name
      EVENT_QUEUE_URL              = aws_sqs_queue.events.url
      ECS_CLUSTER                  = aws_ecs_cluster.ecommerce.name
      GLUE_JOB_NAME                = "ecommerce-phase6-shopify-silver-${var.environment}"
      PHASE5_LEGACY_LIFECYCLE_ENABLED = tostring(var.enable_phase5_legacy_lifecycle_schedule)
    }
  }

  lifecycle {
    precondition {
      condition     = var.nat_gateway_count == 1
      error_message = "On-demand NAT supports exactly one reserved EIP; nat_gateway_count must equal 1."
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.phase6_nat_logs,
    aws_iam_role_policy.phase6_nat_lifecycle,
    aws_cloudwatch_log_group.phase6_nat_lifecycle,
  ]
}

# This is a NETWORK readiness tick; NOT a Shopify order generation schedule.
# Existing 08:00/16:00 generator schedules remain. The 16:30 end-of-day
# lifecycle is independently controlled by enable_phase5_legacy_lifecycle_schedule.
resource "aws_iam_role" "phase6_nat_schedule" {
  count = local.nat_lifecycle_count
  name  = "ecommerce-phase6-nat-scheduler-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "phase6_nat_schedule" {
  count = local.nat_lifecycle_count
  name  = "invoke-only-nat-lifecycle-lambda"
  role  = aws_iam_role.phase6_nat_schedule[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.phase6_nat_lifecycle[0].arn
    }]
  })
}

resource "aws_scheduler_schedule" "phase6_nat_lifecycle" {
  count               = local.nat_lifecycle_count
  name                = "ecommerce-phase6-nat-lifecycle-${var.environment}"
  schedule_expression = "rate(5 minutes)"
  state               = var.nat_gateway_mode == "on_demand" ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.phase6_nat_lifecycle[0].arn
    role_arn = aws_iam_role.phase6_nat_schedule[0].arn
    input    = jsonencode({ source = "nat-readiness-check" })
    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 300
    }
  }

  depends_on = [aws_iam_role_policy.phase6_nat_schedule]
}

output "nat_gateway_mode" {
  value = var.nat_gateway_mode
}

output "phase6_nat_lifecycle_function" {
  value = local.nat_lifecycle_count == 1 ? aws_lambda_function.phase6_nat_lifecycle[0].function_name : null
}
