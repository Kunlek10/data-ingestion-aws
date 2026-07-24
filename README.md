# GDP Ingestion Pipeline

Serverless AWS pipeline that pulls GDP data (`NY.GDP.MKTP.CD`) from the World
Bank API for five countries, lands it in S3, aggregates it into two-year
period averages with AWS Glue, and serves the result from DynamoDB for
dashboarding.

## Architecture

```
World Bank API
      │
      ▼
┌─────────────┐    raw CSV     ┌─────────────┐
│   Lambda    │ ─────────────▶ │  S3 (raw)   │
│ fetch+clean │                └─────────────┘
└──────┬──────┘
       │ glue:StartJobRun (async)
       ▼
┌─────────────┐   gold CSV     ┌─────────────┐
│  AWS Glue   │ ─────────────▶ │  S3 (gold)  │
│ 2yr average │                └─────────────┘
└──────┬──────┘
       │ upsert
       ▼
┌─────────────┐    scan        ┌─────────────┐
│  DynamoDB   │ ─────────────▶ │  Dashboard  │
│    gold     │                │ (Artifact)  │
└─────────────┘                └─────────────┘
```

| Stage | Service | What it does |
|---|---|---|
| Ingest | Lambda (Python 3.11) | Fetches last 10 years of GDP for USA, NPL, CHN, IND, BTN via `wbgapi`, cleans with `pandas`, uploads CSV to S3, then triggers the Glue job |
| Raw store | S3 | Per-year CSV, one object per Lambda invocation (`gdp-data/gdp_data_<timestamp>.csv`) |
| Transform | AWS Glue (Python Shell) | Reads the raw CSV, averages `GDP_USD` into non-overlapping 2-year periods per country |
| Gold store | S3 + DynamoDB | Glue writes the same aggregated rows to both: a CSV in the gold S3 bucket (archive) and the DynamoDB table (query/serving) |
| Presentation | Claude Artifact dashboard | Static snapshot dashboard built from a DynamoDB scan — indexed growth chart, stat tiles, data table |

**Why Glue instead of doing the transform in Lambda:** keeps ingestion and
transformation as separate, independently-triggerable stages.

**Why DynamoDB instead of Aurora:** Aurora was the original choice, but this
AWS account is on a free-tier plan that blocks direct API/Terraform creation
of Aurora clusters (`FreeTierRestrictionError`). DynamoDB fits the data shape
well anyway — the gold dataset is small (25 rows) and pre-aggregated, so it
needs point lookups, not joins — and it bills on-demand with no continuous
baseline cost, unlike Aurora's forced 0.5 ACU minimum (~$45+/month even idle).

## Repo layout

```
.
├── src/
│   └── lambda_function.py     # Ingestion Lambda handler
├── glue/
│   └── gold_transform.py      # Glue Python Shell transform job
├── terraform/
│   ├── main.tf                # All AWS resources
│   ├── variables.tf           # Configurable inputs (see below)
│   └── outputs.tf             # Resource names/ARNs after apply
└── layer/                     # pip-installed Lambda layer deps (gitignored, rebuilt locally)
```

## Prerequisites

- AWS account with credentials configured locally (`aws configure` or SSO)
- IAM permissions to create: S3 buckets, IAM roles/policies, Lambda
  functions/layers, Glue jobs, DynamoDB tables, CloudWatch log groups
- Terraform >= 1.5.0
- Python 3.11 + `pip`

## Deploy

```bash
# 1. Package the Lambda layer (pandas + wbgapi, for the Lambda runtime)
mkdir -p layer/python
pip install \
  --platform manylinux2014_x86_64 \
  --target=layer/python \
  --python-version 3.11 \
  --only-binary=:all: \
  --upgrade pandas wbgapi

# 2. Deploy
cd terraform
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

Terraform auto-zips `src/` and uploads `glue/gold_transform.py` to S3 — no
manual packaging beyond the layer above.

## Run it

```bash
cd terraform

# Trigger ingestion (this also kicks off the Glue job asynchronously)
aws lambda invoke --function-name "$(terraform output -raw lambda_function_name)" \
  --region us-east-1 /tmp/result.json && cat /tmp/result.json

# Check the Glue job finished
aws glue get-job-runs --job-name "$(terraform output -raw glue_job_name)" \
  --region us-east-1 --query 'JobRuns[0].JobRunState' --output text

# Inspect the gold data
aws dynamodb scan --table-name "$(terraform output -raw dynamodb_table_name)" --region us-east-1
```

## Configuration

All inputs live in `terraform/variables.tf` with sensible defaults — override
via `-var` or a `.tfvars` file. Notable ones:

| Variable | Default | Purpose |
|---|---|---|
| `aws_region` | `us-east-1` | Deployment region |
| `project_name` / `environment` | `gdp-ingestion` / `dev` | Resource naming prefix |
| `lambda_timeout` / `lambda_memory_size` | `60` / `256` | Lambda sizing |
| `log_retention_days` | `14` | CloudWatch log retention |

Lambda environment variables (set automatically by Terraform, not manual):
`S3_BUCKET_NAME`, `GLUE_JOB_NAME`, `LOG_LEVEL`. Glue job arguments:
`--GOLD_BUCKET`, `--DYNAMODB_TABLE` (fixed), `--SOURCE_BUCKET` /
`--SOURCE_KEY` (per-run, passed by the Lambda).

## DynamoDB schema

Single-table, single combined partition key — no range key:

| Attribute | Type | Example |
|---|---|---|
| `country_year` (PK) | String | `"USA_2016-2017"` |
| `country_code` | String | `"USA"` |
| `period` | String | `"2016-2017"` |
| `avg_gdp_usd` | Number | `19208507500000` |

## Known limitations

This was built incrementally as an exploration of the service pattern, not
against a production checklist. Worth knowing before relying on it:

- **No orchestration/failure feedback.** The Lambda calls `glue:StartJobRun`
  and returns immediately — it does not know or report whether the Glue job
  actually succeeded. A Glue failure currently surfaces only in the Glue
  console/CloudWatch Logs, not as an alert.
- **No scheduling.** Runs only on manual `lambda invoke`. Add an EventBridge
  scheduled rule if this needs to run unattended.
- **DynamoDB models one access pattern.** The single `country_year` key
  supports point lookups and full scans (fine at 25 rows) but not a native
  "all periods for one country" query — that would need a GSI or a scan.
- **No monitoring** — no CloudWatch alarms, no DLQ on the Lambda, no
  data-quality checks on what the World Bank API returns beyond dropping
  nulls.
- **Dashboard is a static snapshot**, not a live view — refresh by re-scanning
  DynamoDB and republishing.

## Cleanup

```bash
cd terraform
terraform destroy
```
