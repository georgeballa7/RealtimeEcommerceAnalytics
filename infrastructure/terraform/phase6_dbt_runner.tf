# Phase 6 dbt Core: on-demand Fargate runner for the EXISTING private
# Redshift Serverless workgroup. No ECS service and NO Scheduler rule.
# Release in two steps: opt in to provision role/SG/ECR, push an immutable
# image digest, then supply that digest to register the task definition.
variable "enable_phase6_dbt_runner" {
  description = "Explicit opt-in for the dbt IAM/SG/ECR foundation; no task is launched by Terraform."
  type        = bool
  default     = false
}

variable "phase6_dbt_image" {
  description = "Immutable dbt runner ECR image URI. Empty disables its ECS task definition."
  type        = string
  default     = ""

  validation {
    condition     = var.phase6_dbt_image == "" || can(regex("^([0-9]{12})[.]dkr[.]ecr[.]eu-central-1[.]amazonaws[.]com/ecommerce-phase6-dbt-demo@sha256:[a-f0-9]{64}$", var.phase6_dbt_image))
    error_message = "Pin the dbt runner to an immutable eu-central-1 ECR image digest."
  }
}

# For private Redshift Serverless networking the workgroup's public-form
# hostname can time out even when the VPC endpoint is reachable. Resolve
# the *regional* VPCE DNS name from EC2 and pass it explicitly on dbt rollout.
# Empty preserves the previously deployed task's original configuration,
# which is important during unrelated, state-guarded NAT cutover plans.
variable "phase6_dbt_private_host" {
  type        = string
  default     = ""
  description = "Verified regional Redshift Serverless VPC endpoint DNS host for ECS dbt."

  validation {
    condition = var.phase6_dbt_private_host == "" || can(regex(
      "^vpce-[a-z0-9-]+[.]vpce-svc-[a-z0-9-]+[.]eu-central-1[.]vpce[.]amazonaws[.]com$",
      var.phase6_dbt_private_host
    ))
    error_message = "Set a regional private VPCE DNS name, never an arbitrary host."
  }
}

resource "aws_ecr_repository" "phase6_dbt" {
  count                = var.enable_phase6_dbt_runner ? 1 : 0
  name                 = "ecommerce-phase6-dbt-${var.environment}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { CostCenter = "demo" }
}

resource "aws_cloudwatch_log_group" "phase6_dbt" {
  count             = var.enable_phase6_dbt_runner ? 1 : 0
  name              = "/ecs/ecommerce-${var.environment}/phase6-dbt"
  retention_in_days = 14
}

resource "aws_iam_role" "phase6_dbt_task" {
  count = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? 1 : 0
  name  = "ecommerce-phase6-dbt-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { CostCenter = "demo" }
}

# The task role cannot modify workgroup settings, list S3 objects, mutate
# IAM or access Secrets Manager. Redshift SQL rights are granted separately.
resource "aws_iam_role_policy" "phase6_dbt_credentials" {
  count = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? 1 : 0
  role  = aws_iam_role.phase6_dbt_task[0].id
  name  = "get-temporary-analytics-redshift-credentials"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["redshift-serverless:GetCredentials"]
      Resource = [aws_redshiftserverless_workgroup.analytics[0].arn]
    }]
  })
}

resource "aws_security_group" "phase6_dbt" {
  count       = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? 1 : 0
  name_prefix = "ecommerce-phase6-dbt-"
  description = "Ephemeral dbt task: Redshift 5439 and outbound AWS HTTPS"
  vpc_id      = aws_vpc.ecommerce.id
  tags        = { CostCenter = "demo" }
}

resource "aws_vpc_security_group_egress_rule" "phase6_dbt_redshift" {
  count                        = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? 1 : 0
  security_group_id            = aws_security_group.phase6_dbt[0].id
  referenced_security_group_id = aws_security_group.redshift_analytics[0].id
  ip_protocol                  = "tcp"
  from_port                    = 5439
  to_port                      = 5439
}

resource "aws_vpc_security_group_egress_rule" "phase6_dbt_https" {
  count             = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? 1 : 0
  security_group_id = aws_security_group.phase6_dbt[0].id
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
}

# Permit only this task security group to access the existing private warehouse.
# Do not open public Redshift ingress.
resource "aws_vpc_security_group_ingress_rule" "redshift_from_phase6_dbt" {
  count                        = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? 1 : 0
  security_group_id            = aws_security_group.redshift_analytics[0].id
  referenced_security_group_id = aws_security_group.phase6_dbt[0].id
  ip_protocol                  = "tcp"
  from_port                    = 5439
  to_port                      = 5439
}

resource "aws_ecs_task_definition" "phase6_dbt" {
  count                    = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse && var.phase6_dbt_image != "" ? 1 : 0
  family                   = "ecommerce-phase6-dbt-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.phase6_dbt_task[0].arn

  container_definitions = jsonencode([{
    name      = "phase6-dbt"
    image     = var.phase6_dbt_image
    essential = true
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "REDSHIFT_WORKGROUP", value = aws_redshiftserverless_workgroup.analytics[0].workgroup_name },
      { name = "REDSHIFT_HOST", value = var.phase6_dbt_private_host != "" ? var.phase6_dbt_private_host : aws_redshiftserverless_workgroup.analytics[0].endpoint[0].address },
      { name = "REDSHIFT_DATABASE", value = "analytics" },
    ]
    # Safe default: accidentally running the task does not modify Redshift.
    command = ["--action", "help"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.phase6_dbt[0].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])

  depends_on = [
    aws_iam_role_policy.phase6_dbt_credentials,
    aws_vpc_security_group_egress_rule.phase6_dbt_redshift,
    aws_vpc_security_group_ingress_rule.redshift_from_phase6_dbt,
  ]
}

output "phase6_dbt_ecr_url" {
  value = var.enable_phase6_dbt_runner ? aws_ecr_repository.phase6_dbt[0].repository_url : null
}

output "phase6_dbt_task_definition" {
  value = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse && var.phase6_dbt_image != "" ? aws_ecs_task_definition.phase6_dbt[0].arn : null
}

output "phase6_dbt_task_role_arn" {
  value = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? aws_iam_role.phase6_dbt_task[0].arn : null
}

output "phase6_dbt_security_group_id" {
  value = var.enable_phase6_dbt_runner && var.enable_redshift_warehouse ? aws_security_group.phase6_dbt[0].id : null
}
