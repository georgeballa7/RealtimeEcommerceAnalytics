"""Static release guards for least-privilege partitioning of staged Terraform.

These tests do not substitute for terraform validate / AWS IAM evaluation.
"""
from pathlib import Path

TF = (Path(__file__).resolve().parents[2] /
      'infrastructure/terraform/phase5_live_ingestion.tf').read_text()


def block(name: str) -> str:
    prefix = f'resource "aws_iam_role_policy" "{name}" {{'
    start = TF.index(prefix)
    next_block = TF.find('\nresource "', start + len(prefix))
    return TF[start:next_block if next_block != -1 else None]


def test_public_webhook_can_only_read_secret_and_enqueue():
    ingress = block('phase5_webhook_ingress')
    assert 'sqs:SendMessage' in ingress
    assert 'secretsmanager:GetSecretValue' in ingress
    for prohibited in ['rds:StartDBInstance', 'rds:StopDBInstance',
                       'ecs:RunTask', 'lambda:InvokeFunction', 's3:PutObject']:
        assert prohibited not in ingress


def test_controller_owns_database_lifecycle():
    controller = block('phase5_rds_controller')
    assert 'rds:StartDBInstance' in controller
    assert 'rds:StopDBInstance' in controller
    assert 'ecs:RunTask' in controller
    assert 'sqs:SendMessage' not in controller


def test_landing_cannot_start_or_stop_database():
    landing = block('phase5_event_landing')
    assert 's3:PutObject' in landing
    assert 'lambda:InvokeFunction' in landing
    assert 'rds:StartDBInstance' not in landing
    assert 'rds:StopDBInstance' not in landing


def test_lambda_functions_have_separate_execution_roles():
    for name, role in [('phase5_fastapi', 'phase5_lambda'),
                       ('phase5_landing', 'phase5_landing'),
                       ('phase5_controller', 'phase5_controller')]:
        header = f'resource "aws_lambda_function" "{name}" {{'
        after = TF.split(header, 1)[1].split('\nresource "', 1)[0]
        assert f'role = aws_iam_role.{role}.arn' in after


def test_automatic_rds_stop_disabled_until_review():
    assert 'ALLOW_AUTOMATIC_RDS_STOP = "false"' in TF
