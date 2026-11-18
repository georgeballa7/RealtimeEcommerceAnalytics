# Historical Shopify Identity Synchronization — Implementation Status

The historical customer and product synchronization code is maintained in `feature/historical-data-foundation` until PR #3 is merged. The AWS application runtime is in `eu-central-1` using on-demand ECS/Fargate, private RDS PostgreSQL, Shopify Admin GraphQL, Secrets Manager, S3, and CloudWatch.

## Verified customer reconciliation (18 November 2026)

- Historical Silver v1 contains 5,942 identifiable customer identities.
- The authorized full customer-sync ECS run completed with exit code 0.
- Final application result: 5,942/5,942 Shopify customer identities independently verified and mapped in RDS.
- 5,842 mappings were newly verified during the final import and 100 were already present.
- Do not rerun the completed customer population merely to retest deployment scripts.

## Historical catalog synchronization

- Source Silver v1 catalog contains 4,646 products and variants. Approval is restricted to 3,408 audit-unflagged merchandise stock codes; the remaining flagged/ineligible products are excluded.
- Approval manifest is checksum pinned and its catalog selection is enforced in read-only and write-enabled runs.
- The controlled five-product pilot succeeded, and its repeat run requested zero further Shopify product creations or variant updates.
- On-demand catalog ECS task definition revision 3 uses a digest-pinned image and bounded sequential 100-product reconciliation batches, with per-batch CloudWatch summaries.
- On 18 November 2026, the full 3,408-product sync was launched; **final completion and final verified RDS counts must be checked in ECS/CloudWatch before declaring it complete**.

## Implementation components

- Shopify OAuth client-credentials token acquisition/renewal, canonical-ID lookup and verified mutation read-back.
- Versioned PostgreSQL identity and ledger/lease migrations; exclusive per-identity leases for safe restart.
- Docker build definitions and guarded PowerShell image-publication/ECS-execution scripts in `scripts/aws/`.
- S3 source-lineage, audit, and explicit approved-catalog safeguards.
- Initial webhook HMAC/inbox/outbox, event publisher and SQS-to-S3 consumer code with offline tests. **These do not establish deployed end-to-end webhook ingestion.**

## Engineering checks

From the repository root:

```powershell
python -m pip install -e ".[shopify,postgres,historical,aws,dev]"
ruff check src apps tests
python -m pytest -q tests/unit
```

Deployment instructions and operational evidence belong in the existing Notion E-Commerce Engineering Runbook. GitHub includes source code and repeatable CLI scripts, not a second copy of the run history.

**Safety boundaries:** Shopify writes require explicit operator authorization. PostgreSQL and Shopify do not share an atomic transaction; remote read-back, durable identity mapping, leases and fail-closed conflict checks protect retries. Historical Bronze/Silver source records remain immutable. Keep credentials, local datasets, Terraform state, and saved plans out of Git.
