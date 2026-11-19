# Phase 6 warehouse: opt-in, separate from the operational RDS controller.
# Redshift Serverless 4-RPU mode is supported in eu-central-1. Do not enable
# enhanced VPC routing with the existing two-AZ application subnet topology.
variable "enable_redshift_warehouse" {
  type        = bool
  default     = false
  description = "Explicit opt-in: provisioning incurs Redshift compute/storage charges."
}

variable "redshift_daily_rpu_hour_limit" {
  type        = number
  default     = 8
  description = "Stop queries after this many RPU-hours each day (not an account-wide dollar cap)."
  validation {
    condition     = var.redshift_daily_rpu_hour_limit > 0 && var.redshift_daily_rpu_hour_limit <= 24
    error_message = "Select an RPU-hour limit between 0 and 24 for this demo."
  }
}

resource "aws_iam_role" "redshift_s3_copy" {
  count = var.enable_redshift_warehouse ? 1 : 0
  name  = "ecommerce-analytics-redshift-copy-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "redshift.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "redshift_s3_copy" {
  count = var.enable_redshift_warehouse ? 1 : 0
  role  = aws_iam_role.redshift_s3_copy[0].id
  name  = "read-only-curated-s3-warehouse-sources"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.landing.arn}/historical/online_retail_ii/synthetic/v1/intervals/*",
          "${aws_s3_bucket.landing.arn}/warehouse/live_exports/v1/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.landing.arn
        Condition = {
          StringLike = {
            "s3:prefix" = [
              "historical/online_retail_ii/synthetic/v1/intervals/*",
              "warehouse/live_exports/v1/*",
            ]
          }
        }
      }
    ]
  })
}

resource "aws_redshiftserverless_namespace" "analytics" {
  count                 = var.enable_redshift_warehouse ? 1 : 0
  namespace_name        = "ecommerce-analytics-${var.environment}"
  db_name               = "analytics"
  manage_admin_password = true
  iam_roles             = [aws_iam_role.redshift_s3_copy[0].arn]
  default_iam_role_arn  = aws_iam_role.redshift_s3_copy[0].arn
  log_exports           = ["userlog", "connectionlog", "useractivitylog"]
}

resource "aws_security_group" "redshift_analytics" {
  count       = var.enable_redshift_warehouse ? 1 : 0
  name        = "ecommerce-redshift-${var.environment}"
  description = "Private warehouse; clients use IAM-authenticated Redshift Data API"
  vpc_id      = aws_vpc.ecommerce.id
  # No inbound port 5439 exposed.
}

resource "aws_redshiftserverless_workgroup" "analytics" {
  count                = var.enable_redshift_warehouse ? 1 : 0
  workgroup_name       = "ecommerce-analytics-${var.environment}"
  namespace_name       = aws_redshiftserverless_namespace.analytics[0].namespace_name
  base_capacity        = 4
  max_capacity         = 8
  publicly_accessible  = false
  enhanced_vpc_routing = false # Existing app subnets span two AZs.
  subnet_ids           = aws_subnet.application[*].id
  security_group_ids   = [aws_security_group.redshift_analytics[0].id]
  tags                 = { Project = "RealtimeEcommerceAnalytics", CostCenter = "demo" }
}

variable "redshift_monthly_rpu_hour_limit" {
  type        = number
  default     = 16
  description = "Maximum monthly compute RPU-hours before workgroup deactivation (not a total USD cap)."
  validation {
    condition     = var.redshift_monthly_rpu_hour_limit >= 4 && var.redshift_monthly_rpu_hour_limit <= 32 && floor(var.redshift_monthly_rpu_hour_limit) == var.redshift_monthly_rpu_hour_limit
    error_message = "Select an integer monthly limit between 4 and 32 RPU-hours."
  }
}

resource "aws_redshiftserverless_usage_limit" "analytics_daily" {
  count         = var.enable_redshift_warehouse ? 1 : 0
  resource_arn  = aws_redshiftserverless_workgroup.analytics[0].arn
  usage_type    = "serverless-compute"
  period        = "daily"
  amount        = var.redshift_daily_rpu_hour_limit
  breach_action = "deactivate"
}

# A separate monthly ceiling prevents daily reactivation from exhausting the
# overall demo account budget. This is not an absolute dollar spending cap.
resource "aws_redshiftserverless_usage_limit" "analytics_monthly" {
  count         = var.enable_redshift_warehouse ? 1 : 0
  resource_arn  = aws_redshiftserverless_workgroup.analytics[0].arn
  usage_type    = "serverless-compute"
  period        = "monthly"
  amount        = var.redshift_monthly_rpu_hour_limit
  breach_action = "deactivate"
}

output "analytics_redshift_workgroup" {
  value = var.enable_redshift_warehouse ? aws_redshiftserverless_workgroup.analytics[0].workgroup_name : null
}

output "analytics_redshift_copy_role_arn" {
  value = var.enable_redshift_warehouse ? aws_iam_role.redshift_s3_copy[0].arn : null
}

output "analytics_redshift_namespace" {
  value = var.enable_redshift_warehouse ? aws_redshiftserverless_namespace.analytics[0].namespace_name : null
}
