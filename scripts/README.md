# Scripts

Repeatable local bootstrap, lint/test commands, source-data validation and controlled backfill utilities. Keep business logic inside the Python application and pipeline packages rather than scripts.

## Historical Bronze export

Install required dependencies: `python -m pip install -e ".[dev,historical]"`.

From the repository root:

```powershell
python scripts/ingest_historical_bronze.py --source data/raw/online_retail_II.xlsx --destination data/bronze/online_retail_ii/v1
python -m pytest tests/unit/test_historical_bronze.py -v
```

The script retains source worksheet names and row positions, writes one Parquet file per worksheet, and generates `manifest.json` with checksums and row totals. It performs no deduplication, filtering, timezone conversion, or identity mapping. The output path must not exist; use a new versioned destination for each new extraction. Do not commit source workbooks, Parquet files, or generated manifests.

For Online Retail II the original counts should reconcile to **525,461 + 541,910 = 1,067,371** source lines. Raw invoice dates remain timezone-naive. Exact decimal monetary treatment belongs in Silver.
