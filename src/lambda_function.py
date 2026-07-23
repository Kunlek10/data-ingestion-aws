"""
AWS Lambda handler that fetches World Bank GDP data (NY.GDP.MKTP.CD) for a
fixed set of countries over the last 10 years, cleans it with pandas, and
uploads the result as a CSV object to S3.
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


class DataFetchError(Exception):
    """Raised when the World Bank API call fails or returns no usable data."""


class DataUploadError(Exception):
    """Raised when the S3 upload fails."""


def fetch_gdp_data(countries: list[str], years: int) -> pd.DataFrame:
    """Fetch the last `years` GDP data points for the given countries."""
    try:
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
    df = raw_df.reset_index().rename(columns={"economy": "Country_Code"})

    df = df.melt(
        id_vars="Country_Code",
        var_name="Year",
        value_name="GDP_USD",
    )

    df = df.dropna(subset=["GDP_USD"])
    df["Year"] = df["Year"].astype(int)
    df["GDP_USD"] = df["GDP_USD"].astype(float)
    df = df.sort_values(["Country_Code", "Year"]).reset_index(drop=True)

    if df.empty:
        raise DataFetchError("Cleaned dataframe has zero rows after dropping missing values")

    return df[["Country_Code", "Year", "GDP_USD"]]


def upload_csv_to_s3(df: pd.DataFrame, bucket: str, key: str) -> None:
    """Serialize the dataframe to CSV in-memory and upload it to S3."""
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


def handler(event, context):
    bucket_name = os.environ.get("S3_BUCKET_NAME")
    if not bucket_name:
        logger.error("Environment variable S3_BUCKET_NAME is not set")
        return {"statusCode": 500, "body": "Missing S3_BUCKET_NAME environment variable"}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    object_key = f"gdp-data/gdp_data_{timestamp}.csv"

    try:
        logger.info(
            "Fetching %s for %s (last %s years)", INDICATOR, COUNTRIES, YEARS_OF_HISTORY
        )
        raw_df = fetch_gdp_data(COUNTRIES, YEARS_OF_HISTORY)

        logger.info("Cleaning fetched data (%d raw rows/cols)", raw_df.size)
        clean_df = clean_gdp_data(raw_df)

        logger.info("Uploading %d rows to s3://%s/%s", len(clean_df), bucket_name, object_key)
        upload_csv_to_s3(clean_df, bucket_name, object_key)

        logger.info("Ingestion completed successfully")
        return {
            "statusCode": 200,
            "body": f"Uploaded {len(clean_df)} rows to s3://{bucket_name}/{object_key}",
        }

    except DataFetchError as exc:
        logger.error("Data fetch/clean stage failed: %s", exc)
        return {"statusCode": 502, "body": f"Data fetch error: {exc}"}

    except DataUploadError as exc:
        logger.error("S3 upload stage failed: %s", exc)
        return {"statusCode": 500, "body": f"S3 upload error: {exc}"}

    except Exception as exc:  # last-resort guard so Lambda reports a clean failure
        logger.exception("Unexpected error during ingestion")
        return {"statusCode": 500, "body": f"Unexpected error: {exc}"}
