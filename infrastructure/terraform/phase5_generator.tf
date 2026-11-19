variable "phase5_generator_image" {
  description = "Immutable Phase 5 generator ECR image; empty disables task definition"
  type = string
  default = ""
}

resource "aws_ecr_repository" "phase5_generator" {
  name = "ecommerce-phase5-generator-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "phase5_generator" {
  name = "/ecs/ecommerce-${var.environment}/phase5-generator"
  retention_in_days = 14
}

resource "aws_iam_role" "phase5_generator_task" {
  name = "ecommerce-phase5-generator-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "phase5_generator_s3" {
  name = "write-phase5-live-operation-ledger"
  role = aws_iam_role.phase5_generator_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject"]
      Resource = "${aws_s3_bucket.landing.arn}/simulation/phase5/live/v1/*"
    }]
  })
}

resource "aws_ecs_task_definition" "phase5_generator" {
  count = var.phase5_generator_image != "" ? 1 : 0
  family = "ecommerce-phase5-generator-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "512"
  memory = "1024"
  execution_role_arn = aws_iam_role.customer_sync_execution.arn
  task_role_arn = aws_iam_role.phase5_generator_task.arn
  container_definitions = jsonencode([{
    name = "phase5-generator"
    image = var.phase5_generator_image
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
        awslogs-group = aws_cloudwatch_log_group.phase5_generator.name
        awslogs-region = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
  depends_on = [aws_iam_role_policy.customer_sync_execution_secrets]
}

output "phase5_generator_ecr_url" {
  value = aws_ecr_repository.phase5_generator.repository_url
}
output "phase5_generator_task_definition" {
  value = var.phase5_generator_image == "" ? null : aws_ecs_task_definition.phase5_generator[0].arn
}
