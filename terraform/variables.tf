variable "aws_region" {
  description = "AWS region to deploy the pipeline into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix used to name and tag all resources."
  type        = string
  default     = "gdp-ingestion"
}

variable "environment" {
  description = "Deployment environment name (e.g. dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "lambda_runtime" {
  description = "Python runtime version for the Lambda function and layer."
  type        = string
  default     = "python3.11"
}

variable "lambda_timeout" {
  description = "Lambda function timeout in seconds."
  type        = number
  default     = 60
}

variable "lambda_memory_size" {
  description = "Lambda function memory allocation in MB."
  type        = number
  default     = 256
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention period for the Lambda function."
  type        = number
  default     = 14
}

variable "lambda_source_dir" {
  description = "Path to the directory containing lambda_function.py."
  type        = string
  default     = "../src"
}

variable "lambda_layer_dir" {
  description = "Path to the directory containing the packaged layer (must hold a top-level python/ folder)."
  type        = string
  default     = "../layer"
}

variable "tags" {
  description = "Common tags applied to all resources."
  type        = map(string)
  default = {
    Project   = "gdp-ingestion-pipeline"
    ManagedBy = "terraform"
  }
}
