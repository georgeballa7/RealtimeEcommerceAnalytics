# One-off catalog reconciliation. No scheduler, service or automatic Shopify writes.
# Review Terraform plan and approved unique-product metafield before enabling.
locals {
  enable_catalog_sync = var.catalog_sync_image != "" && var.shopify_client_credentials_secret_arn != "" && var.shopify_shop_domain != ""
}

variable "catalog_sync_image" {
  description = "Immutable ECR digest URI for on-demand catalog sync; empty disables task definition."
  type        = string
  default     = ""
}

resource "aws_ecr_repository" "catalog_sync" {
  name                 = "ecommerce-catalog-sync-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "catalog_sync" {
  name              = "/ecs/ecommerce-${var.environment}/catalog-sync"
  retention_in_days = 14
}

resource "aws_iam_role" "catalog_sync_task" {
  name = "ecommerce-catalog-sync-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "catalog_sync_read" {
  name = "read-canonical-silver-and-approved-catalog"
  role = aws_iam_role.catalog_sync_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = aws_s3_bucket.landing.arn
        Condition = { StringLike = { "s3:prefix" = [
          "historical/online_retail_ii/silver/v1/*",
          "historical/online_retail_ii/catalog_approval/v1/*"
        ] } }
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.landing.arn}/historical/online_retail_ii/silver/v1/*",
          "${aws_s3_bucket.landing.arn}/historical/online_retail_ii/bronze/v1/manifest.json",
          "${aws_s3_bucket.landing.arn}/historical/online_retail_ii/catalog_approval/v1/*"
        ]
      }
    ]
  })
}

resource "aws_ecs_task_definition" "catalog_sync" {
  count                    = local.enable_catalog_sync ? 1 : 0
  family                   = "ecommerce-catalog-sync-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.customer_sync_execution.arn
  task_role_arn            = aws_iam_role.catalog_sync_task.arn
  container_definitions = jsonencode([{
    name      = "catalog-sync"
    image     = var.catalog_sync_image
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
    command = ["--bucket", aws_s3_bucket.landing.bucket, "--limit", "5"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.catalog_sync.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}
