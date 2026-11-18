# Historical data pipeline entrypoints

Install dependencies from the repository root:

```powershell
python -m pip install -e ".[dev,historical,aws]"
```

## Bronze — source workbook to S3

```powershell
python scripts/ingest_historical_bronze.py
```

Reads the original local Excel workbook, converts sheets to temporary Parquet files, uploads to the existing Terraform S3 landing bucket with remote SHA-256 checks and commits `historical/online_retail_ii/bronze/v1/manifest.json` last.

## Silver — customer-linked historical data

```powershell
python scripts/transform_historical_silver.py
```

Reads verified Bronze objects from S3; reconciles 1,067,371 source lines; retains all 5,942 customers; stores cross-sheet copies and missing-customer lines separately; publishes the S3 Silver manifest last. Existing versions cannot be overwritten.

## Silver business audit — read-only

```powershell
python -m pytest tests/unit -v
python scripts/audit_historical_silver.py
```

The audit verifies published Silver manifests, Parquet checksums, identity and business rules. It writes a local JSON report under `reports/historical_silver_audit_v1.json` and makes **no S3 changes**. WARN findings require investigation before analytics is declared business-ready.

Source files and generated data/reports must not be committed.
