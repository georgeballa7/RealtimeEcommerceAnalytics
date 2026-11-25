locals {
  existing_lambda_log_groups = {
    controller = "/aws/lambda/ecommerce-demo-phase5-controller"
    fastapi = "/aws/lambda/ecommerce-demo-phase5-fastapi"
    landing = "/aws/lambda/ecommerce-demo-phase5-landing"
    glue_coordinator = "/aws/lambda/ecommerce-phase6-evening-glue-coordinator-demo"
    silver_loader = "/aws/lambda/ecommerce-phase6-silver-redshift-loader-demo"
    dbt_orchestrator = "/aws/lambda/ecommerce-phase7-dbt-orchestrator-demo"
  }
}

resource "aws_cloudwatch_log_group" "existing_lambda" {
  for_each = local.existing_lambda_log_groups
  name = each.value
  retention_in_days = 0
  log_group_class = "STANDARD"

  lifecycle {
    prevent_destroy = true
  }
}
