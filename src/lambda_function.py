"""
AWS Lambda handler that fetches World Bank GDP data (NY.GDP.MKTP.CD) for a
fixed set of countries over the last 10 years, cleans it with pandas, and
uploads the per-year result to S3. On success it kicks off an AWS Glue job
which reads that object, aggregates it into 2-year period averages, and
writes the transformed ("gold") dataset to a separate S3 bucket.
"""

import io
import logging
import os
from datetime import datetime, timezone

import boto3
import pandas as pd
import wbgapi as wb
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

INDICATOR = "NY.GDP.MKTP.CD"
COUNTRIES = ["USA", "NPL", "CHN", "IND", "BTN"]
YEARS_OF_HISTORY = 10

s3_client = boto3.client("s3")
glue_client = boto3.client("glue")


class DataFetchError(Exception):
    """Raised when the World Bank API call fails or returns no usable data."""


class DataUploadError(Exception):
    """Raised when the S3 upload fails."""


class GlueTriggerError(Exception):
    """Raised when kicking off the Glue transformation job fails."""


def fetch_gdp_data(countries: list[str], years: int) -> pd.DataFrame:
    """Fetch the last `years` GDP data points for the given countries."""
    try:
        # wb.data.DataFrame with mrv=years returns the N most recent values per
        # country as a wide dataframe: rows = country (economy), columns = year.
        raw_df = wb.data.DataFrame(
            INDICATOR,
            economy=countries,
            mrv=years,
            numericTimeKeys=True,
            labels=False,
        )
    except Exception as exc:  # wbgapi raises plain Exceptions on HTTP/API errors
        logger.error("Failed to fetch data from World Bank API: %s", exc)
        raise DataFetchError(str(exc)) from exc

    if raw_df is None or raw_df.empty:
        raise DataFetchError("World Bank API returned no data for the requested query")

    return raw_df


def clean_gdp_data(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Reshape the wide World Bank dataframe into tidy long-format rows."""
    # "economy" is wbgapi's country column; rename it to our target schema.
    df = raw_df.reset_index().rename(columns={"economy": "Country_Code"})

    # Pivot from wide (one column per year) to long (one row per country-year).
    df = df.melt(
        id_vars="Country_Code",
        var_name="Year",
        value_name="GDP_USD",
    )

    # Some country/year combinations have no reported GDP yet — drop those.
    df = df.dropna(subset=["GDP_USD"])
    df["Year"] = df["Year"].astype(int)
    df["GDP_USD"] = df["GDP_USD"].astype(float)
    df = df.sort_values(["Country_Code", "Year"]).reset_index(drop=True)

    if df.empty:
        raise DataFetchError("Cleaned dataframe has zero rows after dropping missing values")

    return df[["Country_Code", "Year", "GDP_USD"]]


def upload_csv_to_s3(df: pd.DataFrame, bucket: str, key: str) -> None:
    """Serialize the dataframe to CSV in-memory and upload it to S3."""
    # StringIO avoids writing a temp file to /tmp — Lambda can do this fully in memory.
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=buffer.getvalue(),
            ContentType="text/csv",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to upload CSV to s3://%s/%s: %s", bucket, key, exc)
        raise DataUploadError(str(exc)) from exc


def trigger_gold_transform(job_name: str, source_bucket: str, source_key: str) -> str:
    """Start the Glue job that transforms the raw CSV into 2-year gold averages."""
    try:
        # start_job_run is asynchronous — it kicks off the Glue job and returns
        # immediately with a run ID; the Lambda does not wait for it to finish.
        # The gold bucket name itself is baked into the Glue job's own
        # default_arguments (set in Terraform), so only the source location is
        # passed here.
        response = glue_client.start_job_run(
            JobName=job_name,
            Arguments={
                "--SOURCE_BUCKET": source_bucket,
                "--SOURCE_KEY": source_key,
            },
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to start Glue job %s: %s", job_name, exc)
        raise GlueTriggerError(str(exc)) from exc

    return response["JobRunId"]


def handler(event, context):
    bucket_name = os.environ.get("S3_BUCKET_NAME")
    glue_job_name = os.environ.get("GLUE_JOB_NAME")

    if not bucket_name:
        logger.error("Environment variable S3_BUCKET_NAME is not set")
        return {"statusCode": 500, "body": "Missing S3_BUCKET_NAME environment variable"}
    if not glue_job_name:
        logger.error("Environment variable GLUE_JOB_NAME is not set")
        return {"statusCode": 500, "body": "Missing GLUE_JOB_NAME environment variable"}

    # Unique key per invocation so repeated runs never overwrite each other.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    object_key = f"gdp-data/gdp_data_{timestamp}.csv"

    try:
        # Step 1: pull the raw GDP series from the World Bank API.
        logger.info(
            "Fetching %s for %s (last %s years)", INDICATOR, COUNTRIES, YEARS_OF_HISTORY
        )
        raw_df = fetch_gdp_data(COUNTRIES, YEARS_OF_HISTORY)

        # Step 2: reshape it into tidy Country_Code / Year / GDP_USD rows.
        logger.info("Cleaning fetched data (%d raw rows/cols)", raw_df.size)
        clean_df = clean_gdp_data(raw_df)

        # Step 3: land the cleaned per-year data in S3 (the "raw/silver" layer).
        logger.info("Uploading %d rows to s3://%s/%s", len(clean_df), bucket_name, object_key)
        upload_csv_to_s3(clean_df, bucket_name, object_key)

        # Step 4: hand off to Glue, which reads what we just uploaded, computes
        # 2-year period averages, and writes the "gold" dataset to its own bucket.
        logger.info("Triggering Glue transform job %s", glue_job_name)
        job_run_id = trigger_gold_transform(glue_job_name, bucket_name, object_key)

        logger.info("Ingestion completed successfully, Glue job run: %s", job_run_id)
        return {
            "statusCode": 200,
            "body": (
                f"Uploaded {len(clean_df)} rows to s3://{bucket_name}/{object_key}; "
                f"started Glue job run {job_run_id}"
            ),
        }

    except DataFetchError as exc:
        logger.error("Data fetch/clean stage failed: %s", exc)
        return {"statusCode": 502, "body": f"Data fetch error: {exc}"}

    except DataUploadError as exc:
        logger.error("S3 upload stage failed: %s", exc)
        return {"statusCode": 500, "body": f"S3 upload error: {exc}"}

    except GlueTriggerError as exc:
        logger.error("Glue trigger stage failed: %s", exc)
        return {"statusCode": 500, "body": f"Glue trigger error: {exc}"}

    except Exception as exc:  # last-resort guard so Lambda reports a clean failure
        logger.exception("Unexpected error during ingestion")
        return {"statusCode": 500, "body": f"Unexpected error: {exc}"}
