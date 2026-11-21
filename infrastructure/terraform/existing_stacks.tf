resource "aws_cloudformation_stack" "phase6_coordinator" {
  name          = "ecommerce-phase6-evening-coordinator-demo"
  template_body = file("${path.module}/../cloudformation/phase6_evening_coordinator.json")
  capabilities  = ["CAPABILITY_NAMED_IAM"]

  tags = {
    Project     = "RealtimeEcommerceAnalytics"
    Environment = "demo"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudformation_stack" "phase6_silver_loader" {
  name          = "ecommerce-phase6-silver-loader-demo"
  template_body = file("${path.module}/../cloudformation/phase6_silver_loader.json")

  tags = {
    Project     = "RealtimeEcommerceAnalytics"
    Environment = "demo"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudformation_stack" "phase7_dbt_orchestrator" {
  name          = "ecommerce-phase7-dbt-orchestrator-demo"
  template_body = file("${path.module}/../cloudformation/phase7_dbt_orchestrator.json")

  tags = {
    Project   = "RealtimeEcommerceAnalytics"
    ManagedBy = "CloudFormation"
    CostCenter = "demo"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudformation_stack" "tableau_ssm_access" {
  name          = "ecommerce-tableau-ssm-access-demo"
  template_body = file("${path.module}/../cloudformation/tableau-ssm-on-demand-access.json")
  capabilities  = ["CAPABILITY_NAMED_IAM"]

  parameters = {
    AmazonLinuxArm64Ami = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
  }

  tags = {
    Project     = "RealtimeEcommerceAnalytics"
    ManagedBy   = "CloudFormation"
    CostCenter  = "demo"
    Environment = "dev"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_document" "session_manager_preferences" {
  name          = "SSM-SessionManagerRunShell"
  document_type = "Session"
  content = jsonencode({
    schemaVersion = "1.0"
    description = "Account Session Manager preferences: allow up to 60 minutes idle; no maximum session duration."
    sessionType = "Standard_Stream"
    inputs = {
      idleSessionTimeout = "60"
    }
  })

  lifecycle {
    prevent_destroy = true
  }
}
