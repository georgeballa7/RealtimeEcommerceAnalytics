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


def test_deployer_reuses_temporary_docker_ecr_push_workaround():
    """Never regress to Docker Desktop's failing ECR login on Windows."""
    deploy = (ROOT / "scripts/aws/deploy_phase5_once.ps1").read_text()
    assert "& docker login" not in deploy
    assert "& docker push " not in deploy
    assert "& docker --config $tempConfig push $imageTag" in deploy
    assert "Remove-Item -LiteralPath $tempConfig -Recurse -Force" in deploy
    assert "get-login-password" in deploy


def test_deployer_does_not_dirty_worktree_by_running_terraform_fmt():
    deploy = (ROOT / "scripts/aws/deploy_phase5_once.ps1").read_text()
    assert "& terraform fmt -check -diff" in deploy
    assert "& terraform fmt\n" not in deploy
    ignore = (ROOT / ".gitignore").read_text()
    assert "/.phase5-release.tfplan" in ignore

def test_generator_manifest_checksum_survives_windows_checkout():
    import hashlib
    import json

    approved_hash = "7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06"
    path = ROOT / "config/catalog/phase5-gbp-sku-prices-v1.json"
    data = path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == approved_hash
    assert json.loads(data)["count"] == 3408
    windows_checked_out = data.replace(b"\n", b"\r\n")
    assert hashlib.sha256(windows_checked_out.replace(b"\r\n", b"\n")).hexdigest() == approved_hash
    # Meaningful content changes must still be rejected.
    assert hashlib.sha256(data + b" ").hexdigest() != approved_hash


def test_generator_dockerfile_normalizes_manifest_before_verifying_sha():
    docker = (ROOT / "Dockerfile.phase5-generator").read_text()
    source = (ROOT / "scripts/generate_shopify_live_orders.py").read_text()
    assert "b=p.read_bytes().replace(b'\\r\\n',b'\\n')" in docker
    assert "p.write_bytes(b)" in docker
    assert "hashlib.sha256(b).hexdigest()" in docker
    assert 'manifest_bytes.replace(b"\\r\\n", b"\\n")' in source

def test_deployer_produces_lambda_compatible_single_platform_image():
    deploy = (ROOT / "scripts/aws/deploy_phase5_once.ps1").read_text()
    assert "docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load" in deploy
    assert "imageManifestMediaType" in deploy
    assert "application/vnd.docker.distribution.manifest.v2+json" in deploy
    assert "application/vnd.oci.image.manifest.v1+json" in deploy
    assert "Refusing Terraform apply" in deploy
    assert "& docker --config $tempConfig push $imageTag" in deploy

def test_deployer_checks_image_digest_and_manifest_before_terraform():
    deploy = (ROOT / "scripts/aws/deploy_phase5_once.ps1").read_text()
    assert "if ($digest -notmatch '^sha256:[a-f0-9]{64}$')" in deploy
    assert "imageManifestMediaType" in deploy
    assert 'imageDigests[$kind] = "$registry/$name@$digest"' in deploy
    assert "Refusing Terraform apply." in deploy
    workflow = (ROOT / ".github/workflows/ops-powershell-ci.yml").read_text()
    assert "feature/shopify-live-event-generator" in workflow


def test_shopify_draft_order_create_does_not_query_nonexistent_usererror_code():
    source = (ROOT / "scripts/generate_shopify_live_orders.py").read_text()
    create = source.split('CREATE = """', 1)[1].split('"""', 1)[0]
    assert 'draftOrderCreate(input: $input)' in create
    assert 'userErrors { field message }' in create
    assert 'userErrors { code' not in create


def test_phase6_release_builds_refund_lifecycle_and_reuses_projector():
    script = (ROOT / "scripts/aws/deploy_phase6_rds_controller.ps1").read_text()
    assert "Dockerfile.phase5-lambda" in script
    assert "Dockerfile.phase5-generator" in script
    assert "phase5_projector_image = $projectorImage" in script
    assert "phase5_lifecycle_image = $newLifecycleImage" in script
    assert "Dockerfile.phase5-lifecycle" in script
    assert "enable_phase5_legacy_lifecycle_schedule = $true" in script
    assert "phase5_generator_image = $newGeneratorImage" in script
    assert "cron(0 22 * * ? *)" in script
    assert "enable_phase6_glue_rds_lease = $true" in script
    assert "validate_phase6_release_plan.py" not in script
    assert "terraform show $planPath" in script
    assert "No custom resource-diff gate is applied" in script
    assert "if (-not $Apply)" in script
    assert "DEPLOY-GLUE-LEASE" in script


def test_phase6_combined_release_preserves_scheduled_clock_times():
    lifecycle = (ROOT / "infrastructure/terraform/phase5_scheduled_lifecycle.tf").read_text()
    assert 'morning = { cron = "cron(0 8 * * ? *)", count = 100 }' in lifecycle
    assert 'afternoon = { cron = "cron(0 16 * * ? *)", count = 150 }' in lifecycle
    assert 'schedule_expression_timezone = "Europe/Berlin"' in lifecycle


def test_phase6_release_image_helper_does_not_mix_docker_logs_with_uri():
    source = (ROOT / "scripts/aws/deploy_phase6_rds_controller.ps1").read_text()
    assert '--tag $imageTag $root | Out-Host' in source
    assert '& docker --config $tempConfig push $imageTag | Out-Host' in source
    assert '$newImage -isnot [string] -or $newGeneratorImage -isnot [string]' in source
    assert 'terraform plan -no-color' in source

def test_phase6_loader_uses_windows_proven_python_validation():
    """Windows PowerShell 5.1 must not strip embedded Python string quotes."""
    loader = (ROOT / "scripts/aws/deploy_phase6_silver_loader.ps1").read_text()
    coordinator = (ROOT / "scripts/aws/deploy_phase6_evening_coordinator.ps1").read_text()
    safe_ast = "& python -X utf8 -c 'import ast,sys;ast.parse(open(sys.argv[1]).read())' $Source"
    assert safe_ast in loader
    assert safe_ast in coordinator
    assert 'encoding="utf-8"' not in loader
    assert "--profile $AwsProfile" in loader
    assert "$Profile = 'ecommerce-admin'" not in loader


def test_phase6_zip_validation_is_exercised_on_windows_not_only_parsed():
    workflow = (ROOT / ".github/workflows/ops-powershell-ci.yml").read_text()
    assert "runs-on: windows-latest" in workflow
    assert "shell: powershell" in workflow
    assert "deploy_phase6_silver_loader.ps1" in workflow
    assert "deploy_phase6_evening_coordinator.ps1 -ValidateOnly" in workflow
