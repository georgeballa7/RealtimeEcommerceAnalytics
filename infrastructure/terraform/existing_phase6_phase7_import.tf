import {
  to = aws_iam_role.phase7_dbt_orchestrator
  id = "ecommerce-phase7-dbt-orchestrator-demo"
}
import {
  to = aws_iam_role_policy.phase7_dbt_orchestrator
  id = "ecommerce-phase7-dbt-orchestrator-demo:completion-gated-dbt-ecs-and-dynamodb-only"
}
import {
  to = aws_iam_role_policy_attachment.phase7_dbt_orchestrator_logs
  id = "ecommerce-phase7-dbt-orchestrator-demo/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
import {
  to = aws_iam_role.phase6_redshift_loader
  id = "ecommerce-phase6-redshift-loader-demo"
}
import {
  to = aws_iam_role_policy.phase6_redshift_loader
  id = "ecommerce-phase6-redshift-loader-demo:phase6-read-silver-write-redshift"
}
import {
  to = aws_iam_role_policy.phase6_loader_eventbridge_publish
  id = "ecommerce-phase6-redshift-loader-demo:phase7-silver-loader-asynchronous-success-eventbridge-destination"
}
import {
  to = aws_iam_role_policy_attachment.phase6_redshift_loader_logs
  id = "ecommerce-phase6-redshift-loader-demo/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
import {
  to = aws_iam_role_policy.phase6_coordinator_marker_list
  id = "ecommerce-phase6-evening-coordinator-role-demo:phase6-coordinator-list-marker-prefixes"
}
import {
  to = aws_cloudwatch_event_rule.phase7_dbt["reconcile"]
  id = "ecommerce-phase7-dbt-reconcile-demo"
}
import {
  to = aws_cloudwatch_event_rule.phase7_dbt["ecs_task_stopped"]
  id = "ecommerce-phase7-dbt-task-stopped-demo"
}
import {
  to = aws_cloudwatch_event_rule.phase7_dbt["loader_success"]
  id = "ecommerce-phase7-loader-success-dbt-demo"
}
import {
  to = aws_cloudwatch_event_target.phase7_dbt["reconcile"]
  id = "ecommerce-phase7-dbt-reconcile-demo/dbt-reconciler"
}
import {
  to = aws_cloudwatch_event_target.phase7_dbt["ecs_task_stopped"]
  id = "ecommerce-phase7-dbt-task-stopped-demo/dbt-reconciler"
}
import {
  to = aws_cloudwatch_event_target.phase7_dbt["loader_success"]
  id = "ecommerce-phase7-loader-success-dbt-demo/dbt-reconciler"
}
import {
  to = aws_lambda_permission.phase7_dbt_events["reconcile"]
  id = "ecommerce-phase7-dbt-orchestrator-demo/allow-ecommerce-phase7-dbt-reconcile-demo"
}
import {
  to = aws_lambda_permission.phase7_dbt_events["ecs_task_stopped"]
  id = "ecommerce-phase7-dbt-orchestrator-demo/allow-ecommerce-phase7-dbt-task-stopped-demo"
}
import {
  to = aws_lambda_permission.phase7_dbt_events["loader_success"]
  id = "ecommerce-phase7-dbt-orchestrator-demo/allow-ecommerce-phase7-loader-success-dbt-demo"
}
import {
  to = aws_secretsmanager_secret.shopify_client_metadata
  id = "arn:aws:secretsmanager:eu-central-1:825414820081:secret:ecommerce/demo/shopify/client-credentials-0DNbRd"
}
