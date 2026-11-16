variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Provide the intended 12-digit AWS account ID."
  }
}
variable "aws_profile" {
  type    = string
  default = "ecommerce-admin"
}
variable "aws_region" {
  type    = string
  default = "eu-central-1"
}
variable "state_bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name, chosen before bootstrap apply."
}
