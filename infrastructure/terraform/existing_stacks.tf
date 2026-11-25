# CloudFormation stacks manage their own resources and remain outside Terraform state.
# Their templates are versioned in infrastructure/cloudformation/.
# The account-level Session Manager preferences document is separate.

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
