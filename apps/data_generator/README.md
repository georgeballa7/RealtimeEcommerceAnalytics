# Phase 5 live Shopify order generator

Generator implementation: scripts/generate_shopify_live_orders.py.
It creates REAL Shopify orders with SYNTHETIC customer behavior, by calling
draftOrderCreate and draftOrderComplete on synchronized Shopify variants
and customers. Existing approved 3,408-SKU manifest and Phase 4 GBP policy are
used, with default 45 orders per run and 90% retail selection.

A versioned S3 operation journal tracks reserved, draft, and completed Shopify
operations. Ambiguous create outcomes stop execution to prevent duplicate orders.
All generated orders are tagged ecommerce-synthetic and phase5-live.
No always-on services, schedules, new customers, or new products.

WARNINGS: The Shopify catalog has draft products and zero inventory. Shopify
may refuse completing a draft. The runner will stop on Shopify rejection rather
than silently create an unrelated custom item. Also, creating Shopify orders
does NOT automatically deploy webhook subscriptions or the API/outbox pipeline;
that wiring remains separate Phase 5 work.

Windows commands (project root):

    git pull --ff-only origin feature/shopify-live-event-generator
    .\scripts\aws\publish_ecr_image.ps1 -Workload phase5_generator -Execute -ConfirmPublish
    $generatorImage = "<full ECR immutable URI printed by publisher>"
    .\scripts\aws\register_phase5_generator.ps1 -ImageUri $generatorImage -Execute
    .\scripts\aws\run_phase5_generator.ps1 -ImageUri $generatorImage -RunId live-20261119-smoke -Count 1 -Execute -Apply
    .\scripts\aws\run_phase5_generator.ps1 -ImageUri $generatorImage -RunId live-20261119-a -Count 45 -Execute -Apply

The first command using -Count 1 creates one real Shopify test order.
Do not repeat an incomplete run under a new RunId until Shopify state is
reconciled. No historical order backdating is claimed.
