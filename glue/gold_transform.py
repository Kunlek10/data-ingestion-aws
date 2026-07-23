"""
AWS Glue Python Shell job. Reads the per-year cleaned GDP CSV produced by the
ingestion Lambda, aggregates GDP_USD into 2-year period averages per country,
and writes the result as a CSV to the gold S3 bucket.
"""

import io
import sys

import boto3
import pandas as pd
from awsglue.utils import getResolvedOptions

args = getResolvedOptions(
    sys.argv,
    ["SOURCE_BUCKET", "SOURCE_KEY", "GOLD_BUCKET"],
)

s3_client = boto3.client("s3")


def load_source_csv(bucket: str, key: str) -> pd.DataFrame:
    """Download and parse the per-year CSV the Lambda just uploaded."""
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    return pd.read_csv(io.BytesIO(obj["Body"].read()))


def transform_to_gold(df: pd.DataFrame) -> pd.DataFrame:
    """Average GDP_USD over non-overlapping 2-year periods per country.

    e.g. years 2016 and 2017 collapse into one "2016-2017" period whose
    Avg_GDP_USD is the mean of those two yearly values.
    """
    df = df.copy()
    # Bucket each year into a 2-year window starting from the earliest year
    # present, e.g. min_year=2016 -> 2016,2017 -> "2016"; 2018,2019 -> "2018".
    min_year = df["Year"].min()
    period_start = min_year + ((df["Year"] - min_year) // 2) * 2
    df["Period"] = period_start.astype(str) + "-" + (period_start + 1).astype(str)

    # Collapse each (country, 2-year period) group down to a single averaged row.
    gold_df = (
        df.groupby(["Country_Code", "Period"], as_index=False)["GDP_USD"]
        .mean()
        .rename(columns={"GDP_USD": "Avg_GDP_USD"})
    )
    return gold_df.sort_values(["Country_Code", "Period"]).reset_index(drop=True)


def upload_csv(df: pd.DataFrame, bucket: str, key: str) -> None:
    """Serialize the dataframe to CSV in-memory and upload it to the gold bucket."""
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    s3_client.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue(), ContentType="text/csv")


def main() -> None:
    # SOURCE_BUCKET/SOURCE_KEY are passed per-run by the Lambda (start_job_run);
    # GOLD_BUCKET is a fixed default argument baked into the Glue job itself.
    source_bucket = args["SOURCE_BUCKET"]
    source_key = args["SOURCE_KEY"]
    gold_bucket = args["GOLD_BUCKET"]

    print(f"Reading s3://{source_bucket}/{source_key}")
    raw_df = load_source_csv(source_bucket, source_key)

    gold_df = transform_to_gold(raw_df)

    # Mirror the source filename under gold/ so each run's output is traceable
    # back to the raw file it was derived from.
    source_filename = source_key.rsplit("/", 1)[-1]
    gold_key = f"gold/gold_{source_filename}"

    print(f"Writing {len(gold_df)} rows to s3://{gold_bucket}/{gold_key}")
    upload_csv(gold_df, gold_bucket, gold_key)


if __name__ == "__main__":
    main()
