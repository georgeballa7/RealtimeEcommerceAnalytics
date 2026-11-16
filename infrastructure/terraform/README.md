# VPC and RDS PostgreSQL

Terraform configuration for the e-commerce project's AWS operational database in `eu-central-1`.

## Scope

- A dedicated VPC (`10.20.0.0/16`) with two private database subnets in separate Availability Zones.
- A private route table without an Internet Gateway or NAT Gateway.
- A Single-AZ, encrypted PostgreSQL RDS instance with a DB subnet group spanning both AZs.
- An RDS security group with **no inbound rules** until the private-client access path is approved.
- AWS-managed RDS master credentials in Secrets Manager rather than a password in Terraform variables.

This is a proposed infrastructure configuration, not a deployment. **The RDS database will be unreachable from local Kubernetes until a secure connection path is approved.** That solution may need extra AWS resources and additional costs.

## Before deployment

1. Confirm the AWS Budget and regional cost of RDS instance-hours, provisioned storage, backups and Secrets Manager. Budget alerts are not hard spending limits.
2. Confirm `aws sts get-caller-identity --profile ecommerce-admin` returns the expected AWS account.
3. Copy `terraform.tfvars.example` to `terraform.tfvars`, replace the account placeholder and review the region, instance class and storage allocation.
4. Agree on a secure connection method from local Kubernetes to private RDS before any apply.
5. Run the following on your computer:

```powershell
cd infrastructure/terraform
terraform init
terraform fmt -check
terraform validate
terraform plan -out=ecommerce.tfplan
```

Review the plan, expected costs and network access approach before running `terraform apply ecommerce.tfplan`. Pull requests do not automatically deploy infrastructure.

## Security and teardown

- The database is private, its security group has no ingress, and the private route table has only the local VPC route.
- The RDS master secret is created by AWS; do not paste or commit credentials.
- The template does not deploy EC2, NAT, a VPN, VPC endpoints, S3, or Redshift. Those services are separate work packages.
- Local Terraform state must be protected and never committed. Introduce a secured remote backend before unattended deployments or multi-machine use.
- Deletion protection is enabled. To tear down development RDS, first change `deletion_protection` to `false` and apply that change; then review the destroy plan. The configuration skips a final snapshot, so preserve required data separately.

**Validation status:** Terraform syntax, provider initialization and deployment plan have not been executed or verified in AWS.
