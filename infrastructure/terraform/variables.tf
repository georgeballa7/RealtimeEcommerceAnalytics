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
