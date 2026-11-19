# A single deployment must supply ALL Phase 5 runtime images and Shopify app
# secret together. The complete release enables all event-processing triggers.
check "phase5_atomic_configuration" {
  assert {
    condition = (
      (var.phase5_lambda_image == "" && var.phase5_projector_image == "" &&
       var.phase5_generator_image == "" && var.phase5_lifecycle_image == "") ||
      (var.phase5_lambda_image != "" && var.phase5_projector_image != "" &&
       var.phase5_generator_image != "" && var.phase5_lifecycle_image != "" &&
       var.shopify_webhook_secret_arn != "" &&
       var.shopify_client_credentials_secret_arn != "" &&
       var.shopify_shop_domain != "")
    )
    error_message = "Phase 5 deployment requires all four immutable images, Shopify credentials, and shop domain together."
  }
}
