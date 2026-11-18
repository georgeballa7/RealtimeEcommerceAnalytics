# One-off, operator-triggered task: Terraform apply does NOT run this task.
# AWS Secrets Manager GetRandomPassword and PutSecretValue happen at runtime,
# so neither the generated app password nor RDS master password enters TF state.
variable "db_bootstrap_image" {
  description = "Immutable image digest URI for a one-off database bootstrap; empty disables task definition."
  type        = string
  default     = ""
}

variable "db_app_username" {
  description = "Dedicated PostgreSQL application role, never the RDS master user."
  type        = string
  default     = "ecommerce_app"
}

resource "aws_ecr_repository" "db_bootstrap" {
  name                 = "ecommerce-db-bootstrap-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "db_bootstrap" {
  name              = "/ecs/ecommerce-${var.environment}/db-bootstrap"
  retention_in_days = 14
}

resource "aws_iam_role" "db_bootstrap_execution" {
  name = "ecommerce-db-bootstrap-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "db_bootstrap_execution" {
  role       = aws_iam_role.db_bootstrap_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "db_bootstrap_master_secret" {
  count = var.db_bootstrap_image != "" ? 1 : 0
  name  = "read-rds-master-secret"
  role  = aws_iam_role.db_bootstrap_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_db_instance.ecommerce.master_user_secret[0].secret_arn
    }]
  })
}

resource "aws_iam_role" "db_bootstrap_task" {
  name = "ecommerce-db-bootstrap-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "db_bootstrap_task_secret" {
  name = "generate-and-write-app-secret"
  role = aws_iam_role.db_bootstrap_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetRandomPassword"], Resource = "*" },
      { Effect = "Allow", Action = ["secretsmanager:PutSecretValue"],
        Resource = aws_secretsmanager_secret.api_database.arn }
    ]
  })
}

resource "aws_ecs_task_definition" "db_bootstrap" {
  count                    = var.db_bootstrap_image != "" ? 1 : 0
  family                   = "ecommerce-db-bootstrap-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.db_bootstrap_execution.arn
  task_role_arn            = aws_iam_role.db_bootstrap_task.arn
  container_definitions = jsonencode([{
    name      = "db-bootstrap"
    image     = var.db_bootstrap_image
    essential = true
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_ADMIN_USER", value = var.db_master_username },
      { name = "DB_APP_USER", value = var.db_app_username },
      { name = "APP_SECRET_ID", value = aws_secretsmanager_secret.api_database.arn },
      { name = "DB_SSLROOTCERT", value = "/etc/ssl/certs/rds-global-bundle.pem" }
    ]
    secrets = [
      { name = "DB_ADMIN_PASSWORD", valueFrom = "${aws_db_instance.ecommerce.master_user_secret[0].secret_arn}:password::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.db_bootstrap.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
  depends_on = [aws_iam_role_policy.db_bootstrap_master_secret]
}
