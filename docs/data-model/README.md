# Canonical model

Source CSVs: customers, products, orders, order_items and returns. Preserve original source IDs; map Shopify Customer, Product, Variant, Order and Line Item GIDs using explicit cross-source mapping records.

Expected analytical tables include dim_customer, dim_product, fact_order, fact_order_item, fact_refund/return and fact_customer_daily. Keep source_system, source_record_id, generator_operation_id, origin, event_time_utc and ingested_at_utc. All schemas and migrations are pending validation.
