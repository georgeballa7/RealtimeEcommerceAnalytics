# Phase 6: optional, on-demand AWS Glue PySpark RDS -> Bronze -> Silver.
# NO schedule/trigger: enabling the job only provisions definitions. StartJobRun
# requires explicit --batch-id and is gated on completed Phase 5 RDS ingestion.
variable "enable_phase6_glue_silver" {
  type        = bool
  default     = false
  description = "Deploy on-demand Glue PySpark Bronze-to-Silver job; no automatic runs."
}

resource "aws_security_group" "phase6_glue" {
  count       = var.enable_phase6_glue_silver ? 1 : 0
  name_prefix = "ecommerce-phase6-glue-"
  description = "Private Glue Spark JDBC access to RDS"
  vpc_id      = aws_vpc.ecommerce.id
}

# Glue Spark executors need full TCP connectivity within their own SG.
resource "aws_vpc_security_group_ingress_rule" "phase6_glue_self" {
  count                        = var.enable_phase6_glue_silver ? 1 : 0
  security_group_id            = aws_security_group.phase6_glue[0].id
  referenced_security_group_id = aws_security_group.phase6_glue[0].id
  ip_protocol                  = "tcp"
  from_port                    = 0
  to_port                      = 65535
}

resource "aws_vpc_security_group_egress_rule" "phase6_glue_outbound" {
  count             = var.enable_phase6_glue_silver ? 1 : 0
  security_group_id = aws_security_group.phase6_glue[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_phase6_glue" {
  count                        = var.enable_phase6_glue_silver ? 1 : 0
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.phase6_glue[0].id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_iam_role" "phase6_glue" {
  count = var.enable_phase6_glue_silver ? 1 : 0
  name  = "ecommerce-phase6-glue-silver-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "phase6_glue_service" {
  count      = var.enable_phase6_glue_silver ? 1 : 0
  role       = aws_iam_role.phase6_glue[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "phase6_glue_data" {
  count = var.enable_phase6_glue_silver ? 1 : 0
  name  = "warehouse-bronze-silver-and-rds-credential"
  role  = aws_iam_role.phase6_glue[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = [
          "${aws_s3_bucket.landing.arn}/warehouse/shopify_snapshots/v1/*",
          "${aws_s3_bucket.landing.arn}/glue/phase6_shopify_silver.py",
          "${aws_s3_bucket.landing.arn}/glue/tmp/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.landing.arn
        Condition = {
          StringLike = { "s3:prefix" = [
            "warehouse/shopify_snapshots/v1/*",
            "glue/phase6_shopify_silver.py",
            "glue/tmp/*"
          ] }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.api_database.arn
      }
    ]
  })
}

# NETWORK connection provides the private VPC ENI; credentials are fetched
# by the script from Secrets Manager (not embedded in connection properties).
resource "aws_glue_connection" "phase6_rds_network" {
  count           = var.enable_phase6_glue_silver ? 1 : 0
  name            = "ecommerce-phase6-rds-network-${var.environment}"
  connection_type = "NETWORK"
  physical_connection_requirements {
    subnet_id              = aws_subnet.application[0].id
    security_group_id_list = [aws_security_group.phase6_glue[0].id]
    availability_zone      = aws_subnet.application[0].availability_zone
  }
  depends_on = [
    aws_vpc_security_group_ingress_rule.phase6_glue_self,
    aws_vpc_security_group_egress_rule.phase6_glue_outbound,
    aws_vpc_security_group_ingress_rule.rds_from_phase6_glue
  ]
}

resource "aws_s3_object" "phase6_glue_silver_script" {
  count  = var.enable_phase6_glue_silver ? 1 : 0
  bucket = aws_s3_bucket.landing.id
  key    = "glue/phase6_shopify_silver.py"
  source = "${path.module}/../../scripts/glue/phase6_shopify_silver.py"
  etag   = filemd5("${path.module}/../../scripts/glue/phase6_shopify_silver.py")
}

resource "aws_glue_job" "phase6_shopify_silver" {
  count             = var.enable_phase6_glue_silver ? 1 : 0
  name              = "ecommerce-phase6-shopify-silver-${var.environment}"
  role_arn          = aws_iam_role.phase6_glue[0].arn
  glue_version      = "5.0"
  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 20
  max_retries       = 0
  connections       = [aws_glue_connection.phase6_rds_network[0].name]
  execution_property { max_concurrent_runs = 1 }
  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${aws_s3_bucket.landing.bucket}/${aws_s3_object.phase6_glue_silver_script[0].key}"
  }
  default_arguments = {
    "--job-language"   = "python"
    "--enable-metrics" = "true"
    "--bucket"         = aws_s3_bucket.landing.bucket
    "--db-host"        = aws_db_instance.ecommerce.address
    "--db-name"        = var.db_name
    "--secret-arn"     = aws_secretsmanager_secret.api_database.arn
    "--region"         = var.aws_region
    "--TempDir"        = "s3://${aws_s3_bucket.landing.bucket}/glue/tmp/"
  }
  depends_on = [
    aws_iam_role_policy_attachment.phase6_glue_service,
    aws_iam_role_policy.phase6_glue_data
  ]
}

output "phase6_glue_silver_job_name" {
  value = var.enable_phase6_glue_silver ? aws_glue_job.phase6_shopify_silver[0].name : null
}
