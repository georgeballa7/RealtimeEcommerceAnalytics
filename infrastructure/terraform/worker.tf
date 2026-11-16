locals {
  enable_consumer = var.consumer_image != ""
  enable_spark = var.spark_image != ""
}
resource "aws_ecr_repository" "consumer" {
  name = "ecommerce-consumer-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_repository" "spark" {
  name = "ecommerce-spark-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_cloudwatch_log_group" "consumer" {
  name = "/ecs/ecommerce-${var.environment}/consumer"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "spark" {
  name = "/ecs/ecommerce-${var.environment}/spark"
  retention_in_days = 14
}
resource "aws_iam_role" "consumer_task" {
  name = "ecommerce-consumer-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "consumer" {
  name = "consume-events-to-s3"
  role = aws_iam_role.consumer_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.events.arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.landing.arn}/events/*" }
    ]
  })
}
resource "aws_ecs_task_definition" "consumer" {
  count = local.enable_consumer ? 1 : 0
  family = "ecommerce-consumer-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "256"
  memory = "512"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn = aws_iam_role.consumer_task.arn
  container_definitions = jsonencode([{
    name = "consumer", image = var.consumer_image, essential = true,
    environment = [
      { name = "EVENT_QUEUE_URL", value = aws_sqs_queue.events.url },
      { name = "S3_LANDING_BUCKET", value = aws_s3_bucket.landing.bucket }
    ],
    logConfiguration = {
      logDriver = "awslogs",
      options = {
        awslogs-group = aws_cloudwatch_log_group.consumer.name,
        awslogs-region = var.aws_region,
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}
resource "aws_ecs_service" "consumer" {
  count = local.enable_consumer ? 1 : 0
  name = "ecommerce-consumer"
  cluster = aws_ecs_cluster.ecommerce.id
  task_definition = aws_ecs_task_definition.consumer[0].arn
  desired_count = 1
  launch_type = "FARGATE"
  network_configuration {
    subnets = aws_subnet.application[*].id
    security_groups = [aws_security_group.worker.id]
    assign_public_ip = false
  }
}
resource "aws_iam_role" "spark_task" {
  name = "ecommerce-spark-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "spark_s3" {
  name = "spark-events-and-analytics"
  role = aws_iam_role.spark_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.landing.arn },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.landing.arn}/events/*" },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.landing.arn}/analytics/*" }
    ]
  })
}
resource "aws_ecs_task_definition" "spark" {
  count = local.enable_spark ? 1 : 0
  family = "ecommerce-spark-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "2048"
  memory = "4096"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn = aws_iam_role.spark_task.arn
  container_definitions = jsonencode([{
    name = "spark", image = var.spark_image, essential = true,
    environment = [
      { name = "S3_LANDING_BUCKET", value = aws_s3_bucket.landing.bucket },
      { name = "SPARK_MASTER", value = "local[*]" }
    ],
    logConfiguration = {
      logDriver = "awslogs",
      options = {
        awslogs-group = aws_cloudwatch_log_group.spark.name,
        awslogs-region = var.aws_region,
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}
resource "aws_iam_role" "spark_scheduler" {
  count = local.enable_spark ? 1 : 0
  name = "ecommerce-spark-scheduler-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "spark_scheduler" {
  count = local.enable_spark ? 1 : 0
  name = "run-spark-task"
  role = aws_iam_role.spark_scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow",
        Action = "ecs:RunTask",
        Resource = aws_ecs_task_definition.spark[0].arn,
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.ecommerce.arn } }
      },
      {
        Effect = "Allow",
        Action = "iam:PassRole",
        Resource = [aws_iam_role.ecs_execution.arn, aws_iam_role.spark_task.arn]
      }
    ]
  })
}
resource "aws_scheduler_schedule" "spark" {
  count = local.enable_spark ? 1 : 0
  name = "ecommerce-${var.environment}-spark-microbatch"
  schedule_expression = var.spark_schedule_expression
  state = "DISABLED"
  flexible_time_window { mode = "OFF" }
  target {
    arn = aws_ecs_cluster.ecommerce.arn
    role_arn = aws_iam_role.spark_scheduler[0].arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.spark[0].arn
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
