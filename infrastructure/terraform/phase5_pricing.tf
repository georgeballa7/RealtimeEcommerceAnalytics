# One-off full-catalog GBP variant pricing. Task definition only; never
# launched automatically by Terraform. Reuses existing Shopify/DB app secrets.
variable "phase5_pricing_image" {
  description = "Immutable image digest for guarded price sync, empty disables task definition."
  type        = string
  default     = ""
}

resource "aws_ecr_repository" "phase5_pricing" {
  name                 = "ecommerce-phase5-pricing-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "phase5_pricing" {
  name              = "/ecs/ecommerce-${var.environment}/phase5-pricing"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "phase5_pricing" {
  count                    = var.phase5_pricing_image != "" ? 1 : 0
  family                   = "ecommerce-phase5-pricing-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.customer_sync_execution.arn
  container_definitions = jsonencode([{
    name      = "phase5-pricing"
    image     = var.phase5_pricing_image
    essential = true
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "SHOPIFY_SHOP_DOMAIN", value = var.shopify_shop_domain },
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_SSLROOTCERT", value = "/etc/ssl/certs/rds-global-bundle.pem" }
    ]
    secrets = [
      { name = "DB_USER", valueFrom = "${aws_secretsmanager_secret.api_database.arn}:username::" },
      { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.api_database.arn}:password::" },
      { name = "SHOPIFY_CLIENT_ID", valueFrom = "${var.shopify_client_credentials_secret_arn}:client_id::" },
      { name = "SHOPIFY_CLIENT_SECRET", valueFrom = "${var.shopify_client_credentials_secret_arn}:client_secret::" }
    ]
    # No command override => read-only preview of the whole approved population.
    command = ["--limit", "3408"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group = aws_cloudwatch_log_group.phase5_pricing.name
        awslogs-region = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
  depends_on = [aws_iam_role_policy.customer_sync_execution_secrets]
}

output "phase5_pricing_ecr_url" {
  value = aws_ecr_repository.phase5_pricing.repository_url
}

output "phase5_pricing_task_definition" {
  value = var.phase5_pricing_image == "" ? null : aws_ecs_task_definition.phase5_pricing[0].arn
}
