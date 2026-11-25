import {
  to = aws_cloudwatch_log_group.existing_lambda["controller"]
  id = "/aws/lambda/ecommerce-demo-phase5-controller"
}

import {
  to = aws_cloudwatch_log_group.existing_lambda["fastapi"]
  id = "/aws/lambda/ecommerce-demo-phase5-fastapi"
}

import {
  to = aws_cloudwatch_log_group.existing_lambda["landing"]
  id = "/aws/lambda/ecommerce-demo-phase5-landing"
}

import {
  to = aws_cloudwatch_log_group.existing_lambda["glue_coordinator"]
  id = "/aws/lambda/ecommerce-phase6-evening-glue-coordinator-demo"
}

import {
  to = aws_cloudwatch_log_group.existing_lambda["silver_loader"]
  id = "/aws/lambda/ecommerce-phase6-silver-redshift-loader-demo"
}

import {
  to = aws_cloudwatch_log_group.existing_lambda["dbt_orchestrator"]
  id = "/aws/lambda/ecommerce-phase7-dbt-orchestrator-demo"
}
