variable "customer_sync_image" {
  description = "Immutable ECR image reference for on-demand customer sync; empty disables task definition."
  type        = string
  default     = ""
}

variable "shopify_shop_domain" {
  description = "Shopify development-store myshopify.com hostname."
  type        = string
  default     = ""
}

variable "shopify_admin_secret_arn" {
  description = "Pre-existing Secrets Manager secret ARN holding the Shopify Admin API token as a plain string."
  type        = string
  default     = ""
}
