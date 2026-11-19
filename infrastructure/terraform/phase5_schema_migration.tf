# One-off schema migration ECS task. The deployment script runs this once
# between infrastructure apply and project-app Shopify webhook registration.
resource "aws_cloudwatch_log_group" "phase5_schema_migration" {
  name              = "/ecs/ecommerce-${var.environment}/phase5-schema"
  retention_in_days = 14
}

resource "aws_iam_role" "phase5_schema_execution" {
  name = "ecommerce-phase5-schema-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "phase5_schema_execution" {
  role       = aws_iam_role.phase5_schema_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "phase5_schema_admin_secret" {
  name = "phase5-schema-read-rds-admin"
  role = aws_iam_role.phase5_schema_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_db_instance.ecommerce.master_user_secret[0].secret_arn
    }]
  })
}

resource "aws_ecs_task_definition" "phase5_schema" {
  count                    = local.phase5_live_ready ? 1 : 0
  family                   = "ecommerce-phase5-schema-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.phase5_schema_execution.arn
  container_definitions = jsonencode([{
    name       = "phase5-schema"
    image      = var.phase5_projector_image
    essential  = true
    entryPoint = ["python", "-m", "ecommerce.interfaces.workers.phase5_schema_migrate"]
    environment = [
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_PORT", value = "5432" },
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
        awslogs-group         = aws_cloudwatch_log_group.phase5_schema_migration.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
  depends_on = [aws_iam_role_policy.phase5_schema_admin_secret]
}
output "phase5_schema_task_definition" {
  value = local.phase5_live_ready ? aws_ecs_task_definition.phase5_schema[0].arn : null
}
