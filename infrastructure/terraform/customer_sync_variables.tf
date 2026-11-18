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

variable "shopify_client_credentials_secret_arn" {
  description = "Pre-existing Secrets Manager secret ARN holding Shopify JSON client_id and client_secret fields."
  type        = string
  default     = ""
}
