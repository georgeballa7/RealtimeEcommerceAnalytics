# Terraform state bootstrap

This independent Terraform root creates the encrypted, versioned, private S3 bucket for the E-Commerce application's remote Terraform state. It uses **local state** during bootstrap; never commit or upload this state file.

1. Copy `terraform.tfvars.example` to `terraform.tfvars`, specify the verified AWS account ID and a globally unique bucket name.
2. Run `terraform init`, `terraform fmt -check`, `terraform validate` and `terraform plan -out=bootstrap.tfplan` in this directory.
3. Review the plan and costs; apply only after approval. Back up the resulting local bootstrap state securely outside GitHub.
4. In `../terraform/`, copy `backend.tf.example` to `backend.tf`, fill in the resulting bucket name, region and key, then run `terraform init -migrate-state` only if a local application state already exists. For a new app root without existing state, use `terraform init -reconfigure`.
5. Verify backend reads and lockfile support before planning the main stack.

The bucket is intentionally independent of the application stack. It uses S3-managed encryption, versioning, public-access blocking and a deny-insecure-transport policy. Its bucket resource has `prevent_destroy`; restrict backend read/write and `.tflock` operations with tightly scoped IAM permissions assigned to the Terraform deployment principal outside this root.

Do not remove the local bootstrap state or change its bucket configuration without a recovery plan. Backend state and lock files contain sensitive infrastructure information and must never be placed in GitHub.
