# Staged, non-applied Phase 5: FastAPI -> SQS -> S3 -> on-demand RDS.
# The internal periodic reconciliation rule is NOT a Shopify EventBridge connector.
variable "phase5_lambda_image" {
  type = string
  default = ""
}
variable "phase5_projector_image" {
  type = string
  default = ""
}
variable "shopify_webhook_secret_arn" {
  type = string
  default = ""
}
locals {
  phase5_live_ready = var.phase5_lambda_image != "" && var.phase5_projector_image != "" && var.shopify_webhook_secret_arn != ""
}
resource "aws_ecr_repository" "phase5_lambda" {
  name = "ecommerce-phase5-live-lambda-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_repository" "phase5_projector" {
  name = "ecommerce-phase5-projector-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_dynamodb_table" "phase5_events" {
  name = "ecommerce-${var.environment}-phase5-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key = "event_key"
  attribute {
    name = "event_key"
    type = "S"
  }
  attribute {
    name = "state"
    type = "S"
  }
  global_secondary_index {
    name = "by_state"
    hash_key = "state"
    projection_type = "ALL"
  }
  point_in_time_recovery { enabled = true }
}
resource "aws_dynamodb_table" "phase5_controller" {
  name = "ecommerce-${var.environment}-phase5-controller"
  billing_mode = "PAY_PER_REQUEST"
  hash_key = "id"
  attribute {
    name = "id"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
}
resource "aws_cloudwatch_log_group" "phase5_projector" {
  name = "/ecs/ecommerce-${var.environment}/phase5-projector"
  retention_in_days = 14
}
resource "aws_iam_role" "phase5_lambda" {
  name = "ecommerce-phase5-lambda-${var.environment}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "phase5_lambda_logs" {
  role = aws_iam_role.phase5_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role" "phase5_projector" {
  name = "ecommerce-phase5-projector-${var.environment}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy" "phase5_projector" {
  role = aws_iam_role.phase5_projector.id
  name = "read-landed-events-update-checkpoints"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.landing.arn}/events/shopify/v1/*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.phase5_events.arn },
    { Effect = "Allow", Action = ["dynamodb:Query"], Resource = "${aws_dynamodb_table.phase5_events.arn}/index/by_state" }
  ] })
}
resource "aws_ecs_task_definition" "phase5_projector" {
  count = local.phase5_live_ready ? 1 : 0
  family = "ecommerce-phase5-projector-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "512"
  memory = "1024"
  execution_role_arn = aws_iam_role.customer_sync_execution.arn
  task_role_arn = aws_iam_role.phase5_projector.arn
  depends_on = [aws_iam_role_policy.customer_sync_execution_secrets]
  container_definitions = jsonencode([{
    name = "phase5-projector", image = var.phase5_projector_image, essential = true,
    environment = [
      { name = "DB_HOST", value = aws_db_instance.ecommerce.address },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_SSLROOTCERT", value = "/etc/ssl/certs/rds-global-bundle.pem" },
      { name = "PENDING_EVENTS_TABLE", value = aws_dynamodb_table.phase5_events.name }
    ],
    secrets = [
      { name = "DB_USER", valueFrom = "${aws_secretsmanager_secret.api_database.arn}:username::" },
      { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.api_database.arn}:password::" }
    ],
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group = aws_cloudwatch_log_group.phase5_projector.name,
      awslogs-region = var.aws_region, awslogs-stream-prefix = "ecs"
    } }
  }])
}
# Separate trust boundaries: public webhook API, S3 landing, RDS controller.
# Never give the public-facing FastAPI handler RDS/ECS control permissions.
resource "aws_iam_role" "phase5_landing" {
  name = "ecommerce-phase5-landing-${var.environment}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role" "phase5_controller" {
  name = "ecommerce-phase5-controller-${var.environment}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "phase5_landing_logs" {
  role = aws_iam_role.phase5_landing.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy_attachment" "phase5_controller_logs" {
  role = aws_iam_role.phase5_controller.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "phase5_webhook_ingress" {
  name = "phase5-only-verify-and-enqueue"
  role = aws_iam_role.phase5_lambda.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.events.arn },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.shopify_webhook_secret_arn }
  ] })
}
resource "aws_iam_role_policy" "phase5_event_landing" {
  name = "phase5-land-and-signal"
  role = aws_iam_role.phase5_landing.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.events.arn },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.landing.arn}/events/shopify/v1/*" },
    { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = [aws_dynamodb_table.phase5_events.arn, aws_dynamodb_table.phase5_controller.arn] },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = local.phase5_live_ready ? [aws_lambda_function.phase5_controller[0].arn] : [] }
  ] })
}
resource "aws_iam_role_policy" "phase5_rds_controller" {
  name = "phase5-control-db-and-ecs"
  role = aws_iam_role.phase5_controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.phase5_controller.arn },
    { Effect = "Allow", Action = ["dynamodb:Query"], Resource = "${aws_dynamodb_table.phase5_events.arn}/index/by_state" },
    { Effect = "Allow", Action = ["rds:DescribeDBInstances"], Resource = "*" },
    { Effect = "Allow", Action = ["rds:StartDBInstance", "rds:StopDBInstance"], Resource = aws_db_instance.ecommerce.arn },
    { Effect = "Allow", Action = ["ecs:RunTask"], Resource = local.phase5_live_ready ? [aws_ecs_task_definition.phase5_projector[0].arn] : [] },
    { Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:ListTasks"], Resource = "*" },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.customer_sync_execution.arn, aws_iam_role.phase5_projector.arn] }
  ] })
}
resource "aws_lambda_function" "phase5_fastapi" {
  count = local.phase5_live_ready ? 1 : 0
  function_name = "ecommerce-${var.environment}-phase5-fastapi"
  package_type = "Image"
  image_uri = var.phase5_lambda_image
  role = aws_iam_role.phase5_lambda.arn
  timeout = 5
  memory_size = 512
  image_config { command = ["ecommerce.interfaces.http.shopify_webhooks_lambda.handler"] }
  environment { variables = {
    EVENT_QUEUE_URL = aws_sqs_queue.events.url,
    SHOPIFY_SHOP_DOMAIN = var.shopify_shop_domain,
    SHOPIFY_WEBHOOK_SECRET_ARN = var.shopify_webhook_secret_arn
  } }
}
resource "aws_lambda_function" "phase5_landing" {
  count = local.phase5_live_ready ? 1 : 0
  function_name = "ecommerce-${var.environment}-phase5-landing"
  package_type = "Image"
  image_uri = var.phase5_lambda_image
  role = aws_iam_role.phase5_landing.arn
  timeout = 30
  memory_size = 512
  image_config { command = ["ecommerce.interfaces.workers.shopify_s3_lambda.handler"] }
  environment { variables = {
    EVENT_QUEUE_URL = aws_sqs_queue.events.url,
    S3_LANDING_BUCKET = aws_s3_bucket.landing.bucket,
    PENDING_EVENTS_TABLE = aws_dynamodb_table.phase5_events.name,
    CONTROLLER_TABLE = aws_dynamodb_table.phase5_controller.name,
    RDS_CONTROLLER_FUNCTION = aws_lambda_function.phase5_controller[0].function_name
  } }
}
resource "aws_lambda_event_source_mapping" "phase5_landing" {
  count = local.phase5_live_ready ? 1 : 0
  event_source_arn = aws_sqs_queue.events.arn
  function_name = aws_lambda_function.phase5_landing[0].arn
  batch_size = 1
  enabled = true # Subscriber is registered only after schema migration completes
  function_response_types = ["ReportBatchItemFailures"]
  depends_on = [aws_iam_role_policy.phase5_event_landing]
}
resource "aws_apigatewayv2_api" "phase5" {
  count = local.phase5_live_ready ? 1 : 0
  name = "ecommerce-${var.environment}-phase5-webhook"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "phase5" {
  count = local.phase5_live_ready ? 1 : 0
  api_id = aws_apigatewayv2_api.phase5[0].id
  integration_type = "AWS_PROXY"
  integration_uri = aws_lambda_function.phase5_fastapi[0].invoke_arn
  payload_format_version = "2.0"
}
resource "aws_apigatewayv2_route" "phase5" {
  count = local.phase5_live_ready ? 1 : 0
  api_id = aws_apigatewayv2_api.phase5[0].id
  route_key = "POST /webhooks/shopify"
  target = "integrations/${aws_apigatewayv2_integration.phase5[0].id}"
}
resource "aws_apigatewayv2_stage" "phase5" {
  count = local.phase5_live_ready ? 1 : 0
  api_id = aws_apigatewayv2_api.phase5[0].id
  name = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 30
    throttling_rate_limit = 15
  }
}
resource "aws_lambda_permission" "phase5_api" {
  count = local.phase5_live_ready ? 1 : 0
  statement_id = "AllowApiGatewayPhase5"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.phase5_fastapi[0].function_name
  principal = "apigateway.amazonaws.com"
  source_arn = "${aws_apigatewayv2_api.phase5[0].execution_arn}/*/POST/webhooks/shopify"
}
resource "aws_lambda_function" "phase5_controller" {
  count = local.phase5_live_ready ? 1 : 0
  function_name = "ecommerce-${var.environment}-phase5-controller"
  package_type = "Image"
  image_uri = var.phase5_lambda_image
  role = aws_iam_role.phase5_controller.arn
  timeout = 40 # Controller lease lasts 50s; invocation must finish before lease expiration
  memory_size = 256
  image_config { command = ["ecommerce.interfaces.workers.phase5_rds_controller_lambda.handler"] }
  environment { variables = {
    PENDING_EVENTS_TABLE = aws_dynamodb_table.phase5_events.name,
    CONTROLLER_TABLE = aws_dynamodb_table.phase5_controller.name,
    DB_INSTANCE_IDENTIFIER = aws_db_instance.ecommerce.identifier,
    ECS_CLUSTER = aws_ecs_cluster.ecommerce.name,
    PROJECTOR_TASK_DEFINITION = aws_ecs_task_definition.phase5_projector[0].arn,
    PROJECTOR_SUBNETS = join(",", aws_subnet.application[*].id),
    PROJECTOR_SECURITY_GROUPS = aws_security_group.ecs.id,
    RDS_IDLE_MINUTES = "30",
    ALLOW_AUTOMATIC_RDS_STOP = "true" # 30-minute idle shutdown with lock, backlog and ECS guards
  } }
}
resource "aws_cloudwatch_event_rule" "phase5_reconcile" {
  count = local.phase5_live_ready ? 1 : 0
  name = "ecommerce-${var.environment}-phase5-reconcile"
  schedule_expression = "rate(1 minute)"
  state = "ENABLED" # Recovery reconciliation must never be disabled
}
resource "aws_cloudwatch_event_target" "phase5_reconcile" {
  count = local.phase5_live_ready ? 1 : 0
  rule = aws_cloudwatch_event_rule.phase5_reconcile[0].name
  arn = aws_lambda_function.phase5_controller[0].arn
}
resource "aws_lambda_permission" "phase5_reconcile" {
  count = local.phase5_live_ready ? 1 : 0
  statement_id = "AllowPhase5ReconcileRule"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.phase5_controller[0].function_name
  principal = "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.phase5_reconcile[0].arn
}
output "phase5_webhook_url" {
  value = local.phase5_live_ready ? "${aws_apigatewayv2_api.phase5[0].api_endpoint}/webhooks/shopify" : null
}
