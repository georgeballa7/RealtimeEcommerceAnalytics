"""Phase 6 offline contract guards; no AWS account or warehouse is required."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_warehouse_disabled_until_explicit_opt_in():
    terraform = (ROOT / "infrastructure/terraform/phase6_redshift.tf").read_text()
    assert 'variable "enable_redshift_warehouse"' in terraform
    assert "default     = false" in terraform
    assert 'variable "redshift_monthly_rpu_hour_limit"' in terraform
    daily_limit = terraform.split(
        'resource "aws_redshiftserverless_usage_limit" "analytics_daily" {', 1
    )[1].split("\n}", 1)[0]
    monthly_limit = terraform.split(
        'resource "aws_redshiftserverless_usage_limit" "analytics_monthly" {', 1
    )[1].split("\n}", 1)[0]
    assert 'breach_action = "log"' in daily_limit
    assert 'breach_action = "deactivate"' in monthly_limit
    assert 'depends_on = [aws_redshiftserverless_usage_limit.analytics_daily]' in monthly_limit
    assert '"analytics_monthly"' in terraform
    assert 'period        = "monthly"' in terraform
    assert re.search(r"\bbase_capacity\s*=\s*4\b", terraform)
    assert re.search(r"\bpublicly_accessible\s*=\s*false\b", terraform)


def test_rds_export_has_no_mutation_or_long_lived_credentials():
    script = (ROOT / "scripts/redshift/export_rds_live.py").read_text()
    assert "REPEATABLE READ READ ONLY" in script
    assert 'IfNoneMatch="*"' in script
    assert "S3_LANDING_BUCKET" in script
    assert "start_db_instance" not in script
    assert "write_draft_orders" not in script


def test_historical_loader_never_reconstructs_source_ids_or_fabricates_orders():
    loader = (ROOT / "scripts/redshift/load_warehouse.py").read_text()
    assert "SYNTHETIC_HEAD" in loader
    assert "verified_source_file" in loader
    assert "prior_manifest_key" in loader
    assert "BatchExecuteStatement" in loader or "batch_execute_statement" in loader


def test_redshift_silver_staging_requires_reviewed_schema_rename():
    schema_sql = (ROOT / "analytics/redshift/silver_staging_schema.sql").read_text()
    loader = (ROOT / "scripts/redshift/load_warehouse.py").read_text()
    rename = (ROOT / "scripts/redshift/rename_raw_to_silver_staging.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS silver_staging.synthetic_orders" in schema_sql
    assert "silver_staging_schema_exists" in loader
    assert "pg_namespace WHERE nspname = 'silver_staging'" in loader
    assert "raise RuntimeError" in loader
    assert 'ALTER SCHEMA \\"raw\\" RENAME TO silver_staging' in rename
    assert "--apply" in rename
    assert "Raw RDS snapshots must pass the Glue" in loader
    sources = (ROOT / "analytics/dbt/models/staging/_sources.yml").read_text()
    assert "schema: silver_staging" in sources
    assert "quoting:\\n      schema: true" in sources


def test_analytics_retains_synthetic_label_and_temporal_churn_window():
    mart = (ROOT / "analytics/dbt/models/marts/mart_customer_churn_training.sql").read_text()
    fact = (ROOT / "analytics/dbt/models/marts/fct_orders.sql").read_text()
    assert "dateadd(day,30" in mart
    assert "order_timestamp_utc < p.as_of_date" in mart
    assert "stg_synthetic_orders" in fact and "stg_shopify_orders" in fact


def test_no_model_training_without_temporal_holdout():
    ml = (ROOT / "ml/churn/train_churn.py").read_text()
    assert 'frame["as_of_date"] < last_month' in ml
    assert 'frame["as_of_date"] == last_month' in ml
    assert "DummyClassifier" in ml and "LogisticRegression" in ml
    assert "mlflow" in ml
