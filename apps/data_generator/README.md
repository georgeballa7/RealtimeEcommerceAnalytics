# Synthetic Shopify order generator

`scripts/generate_shopify_live_orders.py` creates **real orders in the development store with synthetic behavior** using approved, verified Shopify customers and variants. S3 operation journals and deterministic run IDs prevent unsafe duplicate creation.

Scheduled ECS/Fargate generation runs at **08:00 and 16:00 Europe/Berlin**, with a separate **16:30 lifecycle** task for eligible cancellations/refunds. On-demand NAT provides private-task outbound access. Operation completion must be verified independently of the schedule.

For a guarded manual run use `scripts/aws/run_phase5_generator.ps1`; review its arguments and the current ECS task definition before execution. Never rerun an ambiguous operation under a new run ID without reconciling Shopify and S3 state. Generated orders are synthetic, not real customer revenue.
