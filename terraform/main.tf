terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ---------------------------------------------------------------------------
# Random suffix to guarantee a globally-unique S3 bucket name
# ---------------------------------------------------------------------------
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# ---------------------------------------------------------------------------
# S3 bucket — destination for ingested GDP data
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "gdp_data" {
  bucket = "${var.project_name}-${var.environment}-${random_id.bucket_suffix.hex}"
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "gdp_data" {
  bucket = aws_s3_bucket.gdp_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "gdp_data" {
  bucket = aws_s3_bucket.gdp_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "gdp_data" {
  bucket                  = aws_s3_bucket.gdp_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# S3 bucket — gold layer, holds the Glue-transformed 2-year averages
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "gold_data" {
  bucket = "${var.project_name}-${var.environment}-gold-${random_id.bucket_suffix.hex}"
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "gold_data" {
  bucket = aws_s3_bucket.gold_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "gold_data" {
  bucket = aws_s3_bucket.gold_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "gold_data" {
  bucket                  = aws_s3_bucket.gold_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# DynamoDB table — queryable store for the gold dataset (2-year GDP averages).
# On-demand billing: no continuous baseline cost, unlike Aurora.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "gold" {
  name         = "${var.project_name}-${var.environment}-gdp-gold"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "country_code"
  range_key    = "period"

  attribute {
    name = "country_code"
    type = "S"
  }

  attribute {
    name = "period"
    type = "S"
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# IAM role + least-privilege policy for the Lambda function
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project_name}-${var.environment}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "lambda_s3_write" {
  statement {
    sid       = "AllowPutObjectToGdpBucket"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.gdp_data.arn}/*"]
  }

  statement {
    sid = "AllowLambdaLogging"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-${var.environment}-*"]
  }

  statement {
    sid       = "AllowStartGoldTransformJob"
    actions   = ["glue:StartJobRun"]
    resources = [aws_glue_job.gold_transform.arn]
  }
}

resource "aws_iam_policy" "lambda_s3_write" {
  name   = "${var.project_name}-${var.environment}-lambda-policy"
  policy = data.aws_iam_policy_document.lambda_s3_write.json
}

resource "aws_iam_role_policy_attachment" "lambda_s3_write" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_s3_write.arn
}

# ---------------------------------------------------------------------------
# Glue job — transforms the raw per-year CSV into 2-year gold averages
# ---------------------------------------------------------------------------
resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.gdp_data.id
  key    = "scripts/gold_transform.py"
  source = "${path.module}/${var.glue_script_path}"
  etag   = filemd5("${path.module}/${var.glue_script_path}")
}

data "aws_iam_policy_document" "glue_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "glue_exec" {
  name               = "${var.project_name}-${var.environment}-glue-role"
  assume_role_policy = data.aws_iam_policy_document.glue_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "glue_s3_access" {
  statement {
    sid       = "AllowReadRawAndScript"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.gdp_data.arn}/*"]
  }

  statement {
    sid       = "AllowWriteGoldBucket"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.gold_data.arn}/*"]
  }

  statement {
    sid = "AllowGlueLogging"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.aws_region}:*:log-group:/aws-glue/*"]
  }
}

resource "aws_iam_policy" "glue_s3_access" {
  name   = "${var.project_name}-${var.environment}-glue-policy"
  policy = data.aws_iam_policy_document.glue_s3_access.json
}

resource "aws_iam_role_policy_attachment" "glue_s3_access" {
  role       = aws_iam_role.glue_exec.name
  policy_arn = aws_iam_policy.glue_s3_access.arn
}

data "aws_iam_policy_document" "glue_dynamodb_access" {
  statement {
    sid       = "AllowWriteGoldTable"
    actions   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.gold.arn]
  }
}

resource "aws_iam_policy" "glue_dynamodb_access" {
  name   = "${var.project_name}-${var.environment}-glue-dynamodb-policy"
  policy = data.aws_iam_policy_document.glue_dynamodb_access.json
}

resource "aws_iam_role_policy_attachment" "glue_dynamodb_access" {
  role       = aws_iam_role.glue_exec.name
  policy_arn = aws_iam_policy.glue_dynamodb_access.arn
}

resource "aws_glue_job" "gold_transform" {
  name         = "${var.project_name}-${var.environment}-gold-transform"
  role_arn     = aws_iam_role.glue_exec.arn
  glue_version = "3.0"
  max_capacity = 1
  timeout      = 10

  command {
    name            = "pythonshell"
    python_version  = "3.9"
    script_location = "s3://${aws_s3_bucket.gdp_data.bucket}/${aws_s3_object.glue_script.key}"
  }

  default_arguments = {
    "--GOLD_BUCKET"         = aws_s3_bucket.gold_data.bucket
    "--DYNAMODB_TABLE"      = aws_dynamodb_table.gold.name
    "--library-set"         = "analytics"
    "--job-bookmark-option" = "job-bookmark-disable"
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Lambda layer — packaged pandas + wbgapi dependencies
# (build the layer/python directory first, see deployment steps)
# ---------------------------------------------------------------------------
data "archive_file" "lambda_layer" {
  type        = "zip"
  source_dir  = var.lambda_layer_dir
  output_path = "${path.module}/layer_payload.zip"
}

resource "aws_lambda_layer_version" "dependencies" {
  layer_name          = "${var.project_name}-${var.environment}-deps"
  filename            = data.archive_file.lambda_layer.output_path
  source_code_hash    = data.archive_file.lambda_layer.output_base64sha256
  compatible_runtimes = [var.lambda_runtime]
  description         = "pandas + wbgapi dependencies for the GDP ingestion Lambda"
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------
data "archive_file" "lambda_source" {
  type        = "zip"
  source_dir  = var.lambda_source_dir
  output_path = "${path.module}/lambda_source.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-${var.environment}-ingest"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "gdp_ingest" {
  function_name = "${var.project_name}-${var.environment}-ingest"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "lambda_function.handler"
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_size

  filename         = data.archive_file.lambda_source.output_path
  source_code_hash = data.archive_file.lambda_source.output_base64sha256

  layers = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      S3_BUCKET_NAME = aws_s3_bucket.gdp_data.bucket
      GLUE_JOB_NAME  = aws_glue_job.gold_transform.name
      LOG_LEVEL      = "INFO"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_s3_write,
  ]

  tags = var.tags
}
