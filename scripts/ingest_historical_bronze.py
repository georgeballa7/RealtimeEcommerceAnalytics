"""CLI entrypoint for the historical Bronze export."""

import argparse
from pathlib import Path

from ecommerce.pipelines.historical_bronze import ingest_historical_bronze


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Online Retail II worksheets to immutable Bronze Parquet")
    parser.add_argument("--source", type=Path, default=Path("data/raw/online_retail_II.xlsx"))
    parser.add_argument("--destination", type=Path, default=Path("data/bronze/online_retail_ii/v1"))
    args = parser.parse_args()
    manifest = ingest_historical_bronze(args.source, args.destination)
    print(f"Bronze export complete: {manifest['total_rows']:,} rows across "
          f"{manifest['worksheet_count']} worksheets")
    print(f"Source SHA-256: {manifest['source_sha256']}")
    print(f"Export: {args.destination}")


if __name__ == "__main__":
    main()
