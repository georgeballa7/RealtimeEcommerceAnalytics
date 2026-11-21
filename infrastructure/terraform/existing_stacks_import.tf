import {
  to = aws_cloudformation_stack.phase6_coordinator
  id = "ecommerce-phase6-evening-coordinator-demo"
}

import {
  to = aws_cloudformation_stack.phase6_silver_loader
  id = "ecommerce-phase6-silver-loader-demo"
}

import {
  to = aws_cloudformation_stack.phase7_dbt_orchestrator
  id = "ecommerce-phase7-dbt-orchestrator-demo"
}

import {
  to = aws_cloudformation_stack.tableau_ssm_access
  id = "ecommerce-tableau-ssm-access-demo"
}

import {
  to = aws_ssm_document.session_manager_preferences
  id = "SSM-SessionManagerRunShell"
}
