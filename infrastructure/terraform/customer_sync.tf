# On-demand only: no ECS service, scheduler or Shopify customer writes on apply.
# Supply an immutable pushed image and pre-existing Shopify token secret ARN
# after reviewing IAM, costs and runtime verification.
locals {
  enable_customer_sync = var.customer_sync_image != "" && var.shopify_admin_secret_arn != "" && var.shopify_shop_domain != ""
}

resource "aws_ecr_repository" "customer_sync" {
  name                 = "ecommerce-customer-sync-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "customer_sync" {
  name              = "/ecs/ecommerce-${var.environment}/customer-sync"
  retention_in_days = 14
}

resource "aws_iam_role" "customer_sync_execution" {
  name = "ecommerce-sync-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "customer_sync_execution" {
  role       = aws_iam_role.customer_sync_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "customer_sync_execution_secrets" {
  count = local.enable_customer_sync ? 1 : 0
  name  = "read-sync-secrets"
  role  = aws_iam_role.customer_sync_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.api_database.arn, var.shopify_admin_secret_arn]
    }]
  })
}

resource "aws_iam_role" "customer_sync_task" {
  name = "ecommerce-sync-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "customer_sync_silver" {
  name = "read-immutable-silver"
  role = aws_iam_role.customer_sync_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.landing.arn,
        Condition = { StringLike = { "s3:prefix" = ["historical/online_retail_ii/silver/v1/*"] } } },
      { Effect = "Allow", Action = ["s3:GetObject"],
        Resource = "${aws_s3_bucket.landing.arn}/historical/online_retail_ii/silver/v1/*" }
    ]
  })
}

resource "aws_ecs_task_definition" "customer_sync" {
  count                    = local.enable_customer_sync ? 1 : 0
  family                   = "ecommerce-customer-sync-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.customer_sync_execution.arn
  task_role_arn            = aws_iam_role.customer_sync_task.arn
  container_definitions = jsonencode([{
    name      = "customer-sync"
    image     = var.customer_sync_image
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
      { name = "SHOPIFY_ADMIN_TOKEN", valueFrom = var.shopify_admin_secret_arn }
    ]
    command = ["--bucket", aws_s3_bucket.landing.bucket, "--limit", "5"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.customer_sync.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}
