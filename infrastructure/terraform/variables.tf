variable "aws_account_id" {
  description = "Expected AWS account ID. Terraform refuses another account."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Provide the 12-digit AWS account ID."
  }
}

variable "aws_profile" {
  description = "Local AWS CLI profile for administrative provisioning."
  type        = string
  default     = "ecommerce-admin"
}

variable "aws_region" {
  description = "AWS region for the development environment."
  type        = string
  default     = "eu-central-1"
}

variable "vpc_cidr" {
  description = "Address range for the dedicated VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "private_subnet_cidrs" {
  description = "Two nonoverlapping private CIDRs in separate AZs."
  type        = list(string)
  default     = ["10.20.10.0/24", "10.20.11.0/24"]

  validation {
    condition     = length(var.private_subnet_cidrs) == 2 && length(distinct(var.private_subnet_cidrs)) == 2
    error_message = "Provide two distinct subnet CIDRs."
  }
}

variable "db_instance_class" {
  description = "Review regional pricing and promotional-credit eligibility before apply."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS gp3 allocated storage in GiB."
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage >= 20
    error_message = "RDS PostgreSQL requires at least 20 GiB of allocated gp3 storage."
  }
}

variable "db_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = "ecommerce_db"
}

variable "db_master_username" {
  description = "Database administrator name; distinct from the AWS IAM admin."
  type        = string
  default     = "ecommerce_admin"
}

variable "nat_gateway_count" {
  description = "Number of zonal NAT Gateways. One shared gateway minimizes dev cost; two provides per-AZ egress resiliency."
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 2], var.nat_gateway_count)
    error_message = "Choose 1 (development) or 2 (per-AZ resilience)."
  }
}

variable "public_subnet_cidrs" {
  type = list(string)
  default = ["10.20.0.0/24", "10.20.1.0/24"]
}
variable "application_subnet_cidrs" {
  type = list(string)
  default = ["10.20.20.0/24", "10.20.21.0/24"]
}
variable "environment" {
  type = string
  default = "demo"
}
variable "api_image" {
  type = string
  default = ""
  description = "Existing working API image. Empty disables application resources."
}
variable "acm_certificate_arn" {
  type = string
  default = ""
  description = "Issued ACM certificate in the deployment region; required to enable public API."
}
variable "consumer_image" {
  type = string
  default = ""
}
variable "spark_image" {
  type = string
  default = ""
}
variable "spark_schedule_expression" {
  type = string
  default = "rate(1 hour)"
}
variable "sqs_visibility_timeout_seconds" {
  type = number
  default = 180
}
variable "api_container_port" {
  type = number
  default = 8000
}
variable "alert_email" {
  description = "Email endpoint for infrastructure CloudWatch alarm notifications; recipient must confirm SNS subscription."
  type        = string
  default     = "george.data.alerts@gmail.com"
}

variable "db_multi_az" {
  type = bool
  default = false
}
