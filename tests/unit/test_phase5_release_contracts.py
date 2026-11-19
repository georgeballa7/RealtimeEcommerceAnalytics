"""Release-level contract tests against the staged package."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def test_generator_container_uses_slot_runner():
    docker = (ROOT / "Dockerfile.phase5-generator").read_text()
    assert 'ENTRYPOINT ["python", "scripts/phase5_slot_runner.py"]' in docker
    assert "phase5_slot_runner.py ./scripts/phase5_slot_runner.py" in docker

def test_refunds_are_not_reported_as_cash_payments():
    sql = (ROOT / "infrastructure/sql/008_shopify_live_orders.sql").read_text()
    code = (ROOT / "src/ecommerce/application/use_cases/live_order_projection.py").read_text()
    assert "refund_line_subtotal" in sql and "refund_line_subtotal" in code
    assert "refund_total" not in sql and "refund_total" not in code
    assert "LEFT JOIN shopify_live_order" in sql
    assert "order_present" in sql

def test_projector_checks_tls_root_ca():
    code = (ROOT / "src/ecommerce/interfaces/workers/phase5_project_s3.py").read_text()
    assert 'Path(os.environ["DB_SSLROOTCERT"]).is_file()' in code
