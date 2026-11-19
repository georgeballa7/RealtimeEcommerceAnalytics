# Phase 5 producer/lifecycle schedules (created DISABLED until integration gates pass).
# Requires the existing phase5_generator.tf resources in the GitHub repository.
# Generator container uses Dockerfile.phase5-generator and slot runner.
variable "phase5_lifecycle_image" {
  description = "Immutable lifecycle ECS digest; empty prevents lifecycle ECS task and schedule"
  type        = string
  default     = ""
}

resource "aws_ecr_repository" "phase5_lifecycle" {
  name                 = "ecommerce-phase5-lifecycle-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "phase5_lifecycle" {
  name              = "/ecs/ecommerce-${var.environment}/phase5-lifecycle"
  retention_in_days = 14
}

resource "aws_iam_role" "phase5_lifecycle_task" {
  name = "ecommerce-phase5-lifecycle-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "phase5_lifecycle_journal" {
  name = "read-generator-records-write-lifecycle-records"
  role = aws_iam_role.phase5_lifecycle_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = aws_s3_bucket.landing.arn
        Condition = { StringLike = { "s3:prefix" = [
          "simulation/phase5/live/v1/", "simulation/phase5/live/v1/*"
        ] } }
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.landing.arn}/simulation/phase5/live/v1/*"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.landing.arn}/simulation/phase5/lifecycle/v1/*"
      }
    ]
  })
}

resource "aws_ecs_task_definition" "phase5_lifecycle" {
  count = var.phase5_lifecycle_image != "" ? 1 : 0
  family = "ecommerce-phase5-lifecycle-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "512"
  memory = "1024"
  execution_role_arn = aws_iam_role.customer_sync_execution.arn
  task_role_arn = aws_iam_role.phase5_lifecycle_task.arn
  depends_on = [aws_iam_role_policy.customer_sync_execution_secrets]
  container_definitions = jsonencode([{
    name = "phase5-lifecycle"
    image = var.phase5_lifecycle_image
    essential = true
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "SHOPIFY_SHOP_DOMAIN", value = var.shopify_shop_domain },
      { name = "S3_LANDING_BUCKET", value = aws_s3_bucket.landing.bucket }
    ]
    secrets = [
      { name = "SHOPIFY_CLIENT_ID", valueFrom = "${var.shopify_client_credentials_secret_arn}:client_id::" },
      { name = "SHOPIFY_CLIENT_SECRET", valueFrom = "${var.shopify_client_credentials_secret_arn}:client_secret::" }
    ]
    command = ["--help"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group = aws_cloudwatch_log_group.phase5_lifecycle.name
        awslogs-region = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

resource "aws_iam_role" "phase5_scheduler" {
  name = "ecommerce-phase5-scheduler-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "phase5_scheduler" {
  name = "run-phase5-only"
  role = aws_iam_role.phase5_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = concat(
          aws_ecs_task_definition.phase5_generator[*].arn,
          aws_ecs_task_definition.phase5_lifecycle[*].arn
        )
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.ecommerce.arn } }
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.customer_sync_execution.arn,
          aws_iam_role.phase5_generator_task.arn,
          aws_iam_role.phase5_lifecycle_task.arn
        ]
      }
    ]
  })
}

locals {
  phase5_generation_slots = {
    morning = { cron = "cron(0 8 * * ? *)", count = 20 }
    afternoon = { cron = "cron(0 15 * * ? *)", count = 25 }
  }
}

resource "aws_scheduler_schedule" "phase5_generation" {
  for_each = var.phase5_generator_image == "" ? {} : local.phase5_generation_slots
  name = "ecommerce-${var.environment}-phase5-${each.key}"
  state = "ENABLED"
  schedule_expression = each.value.cron
  schedule_expression_timezone = "Europe/Berlin"
  flexible_time_window { mode = "OFF" }
  target {
    arn = aws_ecs_cluster.ecommerce.arn
    role_arn = aws_iam_role.phase5_scheduler.arn
    input = jsonencode({
      containerOverrides = [{
        name = "phase5-generator"
        command = ["--slot", each.key, "--apply"]
      }]
    })
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.phase5_generator[0].arn
      launch_type = "FARGATE"
      task_count = 1
      network_configuration {
        subnets = aws_subnet.application[*].id
        security_groups = [aws_security_group.worker.id]
        assign_public_ip = false
      }
    }
  }
}

resource "aws_scheduler_schedule" "phase5_lifecycle" {
  count = var.phase5_lifecycle_image != "" ? 1 : 0
  name = "ecommerce-${var.environment}-phase5-lifecycle"
  state = "DISABLED"
  schedule_expression = "cron(0 18 * * ? *)"
  schedule_expression_timezone = "Europe/Berlin"
  flexible_time_window { mode = "OFF" }
  target {
    arn = aws_ecs_cluster.ecommerce.arn
    role_arn = aws_iam_role.phase5_scheduler.arn
    input = jsonencode({
      containerOverrides = [{ name = "phase5-lifecycle", command = ["--apply", "--confirm-apply"] }]
    })
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.phase5_lifecycle[0].arn
      launch_type = "FARGATE"
      task_count = 1
      network_configuration {
        subnets = aws_subnet.application[*].id
        security_groups = [aws_security_group.worker.id]
        assign_public_ip = false
      }
    }
  }
}
