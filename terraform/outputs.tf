output "s3_bucket_name" {
  description = "Name of the S3 bucket storing ingested GDP data."
  value       = aws_s3_bucket.gdp_data.bucket
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket storing ingested GDP data."
  value       = aws_s3_bucket.gdp_data.arn
}

output "lambda_function_name" {
  description = "Name of the deployed Lambda function."
  value       = aws_lambda_function.gdp_ingest.function_name
}

output "lambda_function_arn" {
  description = "ARN of the deployed Lambda function."
  value       = aws_lambda_function.gdp_ingest.arn
}

output "lambda_role_arn" {
  description = "ARN of the IAM role assumed by the Lambda function."
  value       = aws_iam_role.lambda_exec.arn
}

output "lambda_layer_arn" {
  description = "ARN of the Lambda layer containing pandas and wbgapi."
  value       = aws_lambda_layer_version.dependencies.arn
}
