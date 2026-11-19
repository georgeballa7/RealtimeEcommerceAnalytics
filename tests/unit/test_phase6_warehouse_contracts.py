"""Phase 6 offline contract guards; no AWS account or warehouse is required."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_warehouse_disabled_until_explicit_opt_in():
    terraform = (ROOT / "infrastructure/terraform/phase6_redshift.tf").read_text()
    assert 'variable "enable_redshift_warehouse"' in terraform
    assert "default     = false" in terraform
    assert 'breach_action = "deactivate"' in terraform
    assert 'variable "redshift_monthly_rpu_hour_limit"' in terraform
    assert '"analytics_monthly"' in terraform
    assert 'period        = "monthly"' in terraform
    assert re.search(r"\bbase_capacity\s*=\s*4\b", terraform)
    assert "publicly_accessible   = false" in terraform


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
