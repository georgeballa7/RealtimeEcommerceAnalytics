# Staged only. Do not enable until the Phase 5 Lambda image containing
# Glue-aware RdsController is built, deployed and verified. This adds no AWS
# resource or expense while disabled (default false).
variable "enable_phase6_glue_rds_lease" {
  type        = bool
  default     = false
  description = "Opt in only after deploying lease-aware Phase 5 controller Lambda image."
}

# The existing Lambda role receives read-only permission to verify the status
# of an expired Glue job run. No additional compute resource is provisioned.
resource "aws_iam_role_policy" "phase6_controller_glue_inspect" {
  count = var.enable_phase6_glue_rds_lease ? 1 : 0
  name  = "phase6-read-glue-status-for-safe-rds-stop"
  role  = aws_iam_role.phase5_controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["glue:GetJobRun"]
      Resource = aws_glue_job.phase6_shopify_silver[0].arn
    }]
  })
}
