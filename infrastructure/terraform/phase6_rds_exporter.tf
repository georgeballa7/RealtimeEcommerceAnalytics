# Phase 6 on-demand exporter (NO automatic schedule until full run validated).
variable "phase6_rds_exporter_image" {
  type        = string
  default     = ""
  description = "Pinned ECR image digest. Empty disables exporter task."
}

resource "aws_ecr_repository" "phase6_rds_exporter" {
  name                 = "ecommerce-phase6-rds-exporter-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "phase6_rds_exporter" {
  name              = "/ecs/ecommerce-${var.environment}/phase6-rds-exporter"
  retention_in_days = 14
}

resource "aws_iam_role" "phase6_rds_exporter" {
  name = "ecommerce-phase6-rds-exporter-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "phase6_rds_exporter_s3" {
  role = aws_iam_role.phase6_rds_exporter.id
  name = "write-only-versioned-rds-export"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = "${aws_s3_bucket.landing.arn}/warehouse/live_exports/v1/*"
      }, {
      Effect   = "Allow"
      Action   = ["s3:ListBucket"]
      Resource = aws_s3_bucket.landing.arn
      Condition = {
        StringLike = { "s3:prefix" = ["warehouse/live_exports/v1/*"] }
      }
    }]
  })
}

resource "aws_ecs_task_definition" "phase6_rds_exporter" {
  count                    = var.phase6_rds_exporter_image == "" ? 0 : 1
  family                   = "ecommerce-phase6-rds-exporter-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.customer_sync_execution.arn
  task_role_arn            = aws_iam_role.phase6_rds_exporter.arn
  depends_on               = [aws_iam_role_policy.customer_sync_execution_secrets]
  container_definitions = jsonencode([{
    name      = "phase6-rds-exporter"
    image     = var.phase6_rds_exporter_image
    essential = true
    environment = [
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_SSLROOTCERT", value = "/etc/ssl/certs/rds-global-bundle.pem" },
      { name = "S3_LANDING_BUCKET", value = aws_s3_bucket.landing.bucket },
    ]
    secrets = [
      { name = "DB_USER", valueFrom = "${aws_secretsmanager_secret.api_database.arn}:username::" },
      { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.api_database.arn}:password::" },
    ]
    command = ["--help"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.phase6_rds_exporter.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

output "phase6_rds_exporter_task_definition" {
  value = var.phase6_rds_exporter_image == "" ? null : aws_ecs_task_definition.phase6_rds_exporter[0].arn
}
