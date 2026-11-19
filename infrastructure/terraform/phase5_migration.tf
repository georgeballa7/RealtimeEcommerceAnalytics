# One-off Phase 5 GBP price state migration; NO persistent ECS service.
# Reuses the RDS-managed administrator secret, not the app user's credential.
# This task deliberately does not execute bootstrap_database_user.py.
variable "phase5_migration_image" {
  description = "Immutable ECR image digest URI for Phase 5 migration; empty disables task definition."
  type        = string
  default     = ""
}

resource "aws_ecr_repository" "phase5_migration" {
  name                 = "ecommerce-phase5-migration-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "phase5_migration" {
  name              = "/ecs/ecommerce-${var.environment}/phase5-migration"
  retention_in_days = 14
}

resource "aws_iam_role" "phase5_migration_execution" {
  name = "ecommerce-phase5-migration-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "phase5_migration_execution" {
  role       = aws_iam_role.phase5_migration_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "phase5_migration_read_admin" {
  count = var.phase5_migration_image != "" ? 1 : 0
  name  = "read-only-rds-master-secret"
  role  = aws_iam_role.phase5_migration_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = aws_db_instance.ecommerce.master_user_secret[0].secret_arn
    }]
  })
}

resource "aws_ecs_task_definition" "phase5_migration" {
  count                    = var.phase5_migration_image != "" ? 1 : 0
  family                   = "ecommerce-phase5-migration-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.phase5_migration_execution.arn
  container_definitions = jsonencode([{
    name      = "phase5-migration"
    image     = var.phase5_migration_image
    essential = true
    command   = []
    environment = [
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_ADMIN_USER", value = var.db_master_username },
      { name = "DB_APP_USER", value = var.db_app_username },
      { name = "DB_SSLROOTCERT", value = "/etc/ssl/certs/rds-global-bundle.pem" }
    ]
    secrets = [
      { name = "DB_ADMIN_PASSWORD", valueFrom = "${aws_db_instance.ecommerce.master_user_secret[0].secret_arn}:password::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group = aws_cloudwatch_log_group.phase5_migration.name
        awslogs-region = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
  depends_on = [aws_iam_role_policy.phase5_migration_read_admin]
}

output "phase5_migration_ecr_url" {
  value = aws_ecr_repository.phase5_migration.repository_url
}
output "phase5_migration_task_definition" {
  value = var.phase5_migration_image == "" ? null : aws_ecs_task_definition.phase5_migration[0].arn
}
