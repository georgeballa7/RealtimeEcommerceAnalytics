# Metadata only. Populate secret JSON securely and provision a matching
# least-privilege PostgreSQL role before enabling the API.
resource "aws_secretsmanager_secret" "api_database" {
  name                    = "ecommerce/${var.environment}/api/database"
  description             = "Application PostgreSQL credentials; JSON keys: username, password."
  recovery_window_in_days = 7
}
resource "aws_iam_role_policy" "api_execution_secret" {
  name = "read-api-database-secret"
  role = aws_iam_role.api_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.api_database.arn
    }]
  })
}
