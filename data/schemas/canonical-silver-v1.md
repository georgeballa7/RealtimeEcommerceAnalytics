# Canonical Silver contract — draft v1

**Scope:** Online Retail II, deterministic synthetic backfill, and Shopify integration. Silver IDs are platform-owned UUIDs. Shopify IDs are external IDs assigned only by Shopify; never manufacture GraphQL GIDs. An identity mapping must be resolved before attributing a Shopify webhook to an existing canonical customer.

## Bronze — immutable source

Preserve the original workbook separately from the derived datasets. Retain the original columns, worksheet name, zero-based worksheet row index, raw invoice date (without guessing timezone), original source IDs, and an import manifest containing file checksum, extraction version, record counts, and storage path. No cleaning or identity remapping in Bronze.

## Silver entity contracts

| Dataset | Canonical primary key | Source/integration identifiers | Other required fields |
|---|---|---|---|
| `customers` | `customer_id` (UUID) | `source_system`, `source_customer_id`, nullable `shopify_customer_gid` (must be verified before Shopify-bound simulation) | `first_seen_at`, origin/provenance |
| `products` | `product_id` (UUID) | `source_system`, `source_stock_code`, nullable verified `shopify_product_gid` | `description`, `item_class`, provenance |
| `product_variants` | `variant_id` (UUID) | `product_id`, nullable verified `shopify_variant_gid` | `sku`, provenance; required mapping before Shopify-bound orders |
| `orders` | `order_id` (UUID) | `source_system`, `source_order_id`, optional verified `shopify_order_gid`, `order_number` | nullable `customer_id`, `order_timestamp`, `currency`, `origin` |
| `order_items` | `order_item_id` (UUID) | `source_record_key`, optional verified Shopify line-item GID | `order_id`, nullable `product_id`, quantity, unit_price, signed_line_amount, quality_flags |

The primary key is one UUID per canonical entity. All 5,942 distinct identifiable UCI customers belong to the simulation and synchronization population; no sampling or selected subset is allowed. Source/Shopify identifiers are alternate lookup keys, not additional primary keys. Persist source-system names alongside source IDs. The full Shopify GID includes its object type and should be stored as returned by the API, with shop scope when connecting more than one Shopify store.

### Historical ID policy

- Normalize the historical Excel customer ID to its integer-like string (for example, `17850`, never `17850.0`) before mapping. Do not reuse the notebook’s `customer_id` field as a canonical UUID without transformation.
- `customer_id = UUIDv5(fixed_namespace, 'customer' + source_system + original_customer_id)` where the UCI customer ID is present; otherwise null on orders and no synthetic customer record.
- `order_id` is derived from the original invoice ID, including cancellation identifiers. Validate invoice-to-customer and invoice-to-timestamp consistency before enforcing order-level attributes.
- `product_id` is derived from the source stock code. Preserve descriptions and code-classification anomalies for review. Each Shopify order item also needs an explicit variant mapping; the generator must not assume that product GID and variant GID are interchangeable.
- `order_item_id` is derived from the **retained source row key** so repeated invoice lines can remain distinct. Its stability depends on consistent source-sheet/row provenance and the documented cross-sheet deduplication policy.
- Original UCI `InvoiceDate` has no timezone information. Store its raw naive timestamp and do not assert UTC until a documented source timezone policy is approved.
- Original customer/product/invoice IDs must remain available in Silver for traceability.

### Transformation, provenance and verification

Bronze-to-Silver must apply the independently verified cross-sheet deduplication rule, preserve returns and adjustments separately from eligible merchandise sales, and keep row-level quality flags. Required checks: row reconciliation; deterministic ID reruns; unique canonical primary keys at their entity grain; order-item foreign keys; source key uniqueness; no fabricated Shopify GIDs; immutable source archive; manifest checksum; and source versus synthetic provenance. All financial values should use exact decimal types in persisted schemas, not binary floating point.

### Full-population Shopify synchronization invariant

- Synchronize **all 5,942 identifiable historical customers**, never a sample. Anonymous source rows remain anonymous and do not become fictional customer entities. The precise historical customer count is an acceptance check; if a later source audit changes it, reconcile the difference before importing.
- Silver contains the canonical UUID and original UCI ID from the first processing run. Shopify GIDs remain null **only while Shopify has not returned a verified identity**; never fabricate them or treat a placeholder as valid.
- Use a stable external reference (the canonical UUID in an appropriately configured Shopify custom-ID metafield, if available) for create/find/retry, then persist a unique mapping `(shop_id, shopify_customer_gid) -> customer_id` and verify no duplicate customer assignment.
- **Do not run Shopify-bound event generation/import until every identifiable historical customer has a verified Shopify customer GID**, and all products/variants referenced by generated Shopify orders have verified GIDs. A failed sync is a blocker, not a reason to silently omit customers.
- Store customer sync progress, retries, API responses and unique external-ID mappings in the operational integration database; export verified mappings with Silver so consumers share one identity contract. Existing historical invoice data remains source-faithful in Bronze.
- A synthetic customer event in Shopify remains `synthetic_backfill` even if delivered by a real Shopify webhook; origin and ingestion channel must be distinct. A real webhook never automatically implies a real sale.

### Generator and backfill contract

The generator consumes versioned Silver-derived profiles covering **every identifiable historical customer** and emits canonical customers, products, product_variants, orders, order_items and lifecycle event records. Generated events must have stable `event_id`, `event_type`, `event_time`, `ingested_at`, `batch_id`, and `origin='synthetic_backfill'`; provenance is independent of transport. Shopify imports return real Shopify GIDs, which are mapped to canonical IDs without replacing them. The backfill start/end are explicit half-open UTC boundaries; an actual live cutover is determined only after integration succeeds.

**Not in this schema-definition commit:** Redshift dimensional tables, Shopify API writes, production event generation, and an unverified timezone conversion. Full Shopify customer/variant sync remains a mandatory acceptance gate for Shopify-bound simulation, not an optional subset or separate identity strategy.
