# AWS resource ownership and live architecture audit

**Scope:** `RealtimeEcommerceAnalytics`, account `825414820081`, `eu-central-1`.  
**Evidence:** Live AWS read-only discovery, CloudFormation stack resources/templates, live S3 Terraform state `ecommerce/demo/terraform.tfstate` (serial 53, last modified 2026-11-21), and the PR #8 feature branch.  
**Status:** Audit snapshot, **not** permission to deploy. Runtime and state may change independently.

## Ownership boundaries

| Component | Intended control plane | Evidence / current status |
| --- | --- | --- |
| Project VPC `vpc-076127eb4c8bb7437`, six subnets, route tables, IGW, S3 Gateway endpoint, security groups | Terraform | Tagged `ManagedBy=Terraform`; managed resources present in remote state |
| RDS `ecommerce-postgres-dev`, ECS `ecommerce-demo`, Phase 5 Lambdas, SQS/DLQ, DynamoDB, landing S3, Glue job and Redshift Serverless `ecommerce-analytics-demo` | Terraform | Their resource families and deployed identifiers are represented in remote state; runtime verified separately |
| On-demand NAT Gateway and its application default routes | **Ephemeral runtime:** Terraform-owned NAT lifecycle Lambda | No NAT or default NAT routes at audit time; reserved EIP, route tables, controller Lambda and schedule are Terraform-managed. Do not import ephemeral NAT/routes into Terraform |
| Phase 6 evening coordinator: Lambda, role and schedule | CloudFormation stack `ecommerce-phase6-evening-coordinator-demo` | Four stack resources verified; **live schedule differs from the stored CloudFormation stack template** |
| Phase 6 Silver loader: Lambda, EventBridge rule, Lambda permission | CloudFormation stack `ecommerce-phase6-silver-loader-demo` | Three stack resources; live stack template equals branch template by parsed JSON |
| Phase 7 dbt orchestrator Lambda | CloudFormation stack `ecommerce-phase7-dbt-orchestrator-demo` | One stack resource; live stack template equals branch template by parsed JSON; launch flag true |
| Tableau EC2/SSM access and its security group, roles and ingress | CloudFormation stack `ecommerce-tableau-ssm-access-demo` | Six stack resources; live stack template equals branch template by parsed JSON; EC2 currently stopped |
| Phase 7 EventBridge reconciliation/task/loader-success rules and targets, external IAM roles and policies | Directly provisioned; **proposed Terraform adoption** | Staged `existing_phase6_phase7_import.tf` entries exist but corresponding resources **not in Terraform state**; inspect ownership individually before imports |
| Four CloudFormation stacks as whole objects | CloudFormation presently; **proposed Terraform stack-level adoption** | `existing_stacks.tf` and `existing_stacks_import.tf` declare/import stacks, but state has no `aws_cloudformation_stack` instances; adoption would be a separate, reviewed decision |
| AWS governance: ChatGPT role, CloudTrail, budgets and other account-wide settings | Independently administered AWS APIs | Not part of this project's Terraform/CloudFormation state; avoid asserting Terraform ownership |
| Datadog integration CloudFormation stacks | Datadog integration | Separate external stacks; do not adopt without explicit approval |

**State overlap check:** 185 Terraform-managed resource instances and 14 enumerated child resources across the four e-commerce CloudFormation stacks. No matching physical IDs between these sets were found. This is not a full drift-free certification: existing Terraform inline policies may extend IAM roles owned by CloudFormation, and some resource boundaries require separate review.

## Live topology verified

- `ecommerce-vpc` CIDR `10.20.0.0/16`.
- Public subnets `10.20.0.0/24` and `10.20.1.0/24` use an internet gateway route. `MapPublicIpOnLaunch=false` does not make the routed subnets private; public addressing must be explicitly enabled on resources that need it.
- Private application subnets `10.20.20.0/24` and `10.20.21.0/24` have an S3 gateway endpoint route and no internet default route while NAT is absent.
- Isolated DB subnets `10.20.10.0/24` and `10.20.11.0/24` have only the VPC local route; RDS is private and stopped at audit time.
- Redshift Serverless is available, private, and attached to both application subnets. Glue Silver has an RDS network connection.
- EventBridge Scheduler: generators at **08:00 / 16:00**, lifecycle at **16:30** (`Europe/Berlin`); NAT controller every **5 minutes**.
- Live Phase 6 coordinator schedule: `cron(0/5 0-2,8-15,16-23 * * ? *)`, `Europe/Berlin`. This supports morning, evening, and overnight polling.
- No NAT gateways and no running ECS tasks at audit time. Redshift available, RDS stopped, Tableau EC2 stopped. These are runtime observations, not Terraform desired-state changes.

## Reconciliation required before PR #8 merge

1. **Coordinator CloudFormation drift:** The stack template recorded by CloudFormation still has `cron(0/5 16-21 * * ? *)` and older afternoon-only embedded code; the **actual live schedule** is the extended morning/evening/overnight expression. The branch template has the extended schedule and newer logic. Do not equate matching branch and live schedule with proof that the *deployed Lambda code* equals the branch; compare Lambda deployment package/source and IAM policy differences before CloudFormation updates.
2. **Cross-tool IAM boundary:** Terraform state contains `aws_iam_role_policy.phase6_coordinator_slot_readiness[0]` attached to `ecommerce-phase6-evening-coordinator-role-demo`, a role owned by CloudFormation. This is a separate inline policy, but CloudFormation policy management and Terraform policy management must not overwrite one another. Confirm effective role policies and make the boundary explicit.
3. **Staged imports:** `existing_stacks_import.tf` and `existing_phase6_phase7_import.tf` are future-state proposals, not current ownership. Never run `terraform apply` with these declarations and default/partial tfvars. Decide whether Terraform should manage CloudFormation stacks as *stack objects*; CloudFormation will still own their child resources. Avoid managing any child resource through both.
4. **CI:** Python CI tests still reflect the former 22:00 lifecycle and dbt-disabled rollout. Update tests to verified live 16:30 operation and enabled dbt with fail-closed guards, then rerun all checks. Do not relax safety gates merely to obtain green CI.
5. **Drift / deployment review:** Compare live CloudFormation stack resource properties, Lambda code digests, IAM policies, ECS task definition image digests, runtime schedules and protected Terraform plan before any apply; evaluate drift using read-only APIs first.

## Safety rules

- This audit makes **no changes** to AWS resources and does not authorize a Terraform/CloudFormation deployment.
- Do not use a Terraform import declaration as evidence of an existing state-managed resource.
- Do not use a CloudFormation template as proof of the current runtime if direct API changes have occurred.
- Treat NAT Gateway creation/deletion as controlled runtime state; Terraform controls the controller and durable infrastructure, not the ephemeral gateway.
- Repository source, deployed package, CloudFormation template and Terraform state are distinct artifacts and must be compared separately.
