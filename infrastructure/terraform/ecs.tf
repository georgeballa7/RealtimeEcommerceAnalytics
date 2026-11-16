locals {
  enable_api = var.api_image != "" && var.acm_certificate_arn != ""
}
resource "aws_ecs_cluster" "ecommerce" {
  name = "ecommerce-${var.environment}"
}
resource "aws_ecr_repository" "api" {
  name = "ecommerce-api-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_cloudwatch_log_group" "api" {
  name = "/ecs/ecommerce-${var.environment}/api"
  retention_in_days = 14
}
resource "aws_iam_role" "ecs_execution" {
  name = "ecommerce-ecs-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role" "api_task" {
  name = "ecommerce-api-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "api_publish" {
  role = aws_iam_role.api_task.id
  name = "send-events"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.events.arn }]
  })
}
resource "aws_lb" "api" {
  count = local.enable_api ? 1 : 0
  name = "ecommerce-${var.environment}-alb"
  internal = false
  load_balancer_type = "application"
  security_groups = [aws_security_group.alb.id]
  subnets = aws_subnet.public[*].id
  drop_invalid_header_fields = true
}
resource "aws_lb_target_group" "api" {
  count = local.enable_api ? 1 : 0
  name = "ecommerce-${var.environment}-api"
  port = var.api_container_port
  protocol = "HTTP"
  target_type = "ip"
  vpc_id = aws_vpc.ecommerce.id
  health_check { path = "/health" }
}
resource "aws_lb_listener" "https" {
  count = local.enable_api ? 1 : 0
  load_balancer_arn = aws_lb.api[0].arn
  port = 443
  protocol = "HTTPS"
  certificate_arn = var.acm_certificate_arn
  ssl_policy = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  default_action {
    type = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
}
resource "aws_ecs_task_definition" "api" {
  count = local.enable_api ? 1 : 0
  family = "ecommerce-api-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "256"
  memory = "512"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn = aws_iam_role.api_task.arn
  container_definitions = jsonencode([{
    name = "api", image = var.api_image, essential = true,
    portMappings = [{ containerPort = var.api_container_port, protocol = "tcp" }],
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_SSLMODE", value = "verify-full" }
    ],
    logConfiguration = {
      logDriver = "awslogs",
      options = {
        awslogs-group = aws_cloudwatch_log_group.api.name,
        awslogs-region = var.aws_region,
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}
resource "aws_ecs_service" "api" {
  count = local.enable_api ? 1 : 0
  name = "ecommerce-api"
  cluster = aws_ecs_cluster.ecommerce.id
  task_definition = aws_ecs_task_definition.api[0].arn
  desired_count = 1
  launch_type = "FARGATE"
  network_configuration {
    subnets = aws_subnet.application[*].id
    security_groups = [aws_security_group.ecs.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api[0].arn
    container_name = "api"
    container_port = var.api_container_port
  }
  depends_on = [aws_lb_listener.https]
}
