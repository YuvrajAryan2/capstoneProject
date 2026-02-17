terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

########################
# IAM ROLE
########################

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_role" {
  name               = "${var.project_name}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

########################
# CUSTOM POLICY
########################

data "aws_iam_policy_document" "lambda_policy" {

  statement {
    effect  = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ]
    resources = [aws_dynamodb_table.feedback_table.arn]
  }

  statement {
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = ["*"]
  }

  statement {
    effect  = "Allow"
    actions = [
      "comprehend:DetectSentiment",
      "comprehend:DetectKeyPhrases"
    ]
    resources = ["*"]
  }

  statement {
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.export_bucket.arn}/*"]
  }
}

resource "aws_iam_policy" "lambda_policy" {
  name   = "${var.project_name}-lambda-policy"
  policy = data.aws_iam_policy_document.lambda_policy.json
}

resource "aws_iam_role_policy_attachment" "lambda_attach" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

########################
# DYNAMODB
########################

resource "aws_dynamodb_table" "feedback_table" {
  name         = var.feedback_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "feedbackId"

  attribute {
    name = "feedbackId"
    type = "S"
  }
}

########################
# S3
########################

resource "aws_s3_bucket" "export_bucket" {
  bucket = var.export_bucket_name
}

########################
# LAMBDA
########################

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../backend/lambda_function.py"
  output_path = "${path.module}/lambda_build/lambda.zip"
}

resource "aws_lambda_function" "feedback_api" {
  function_name = "${var.project_name}-lambda"
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout = 20

  environment {
    variables = {
      FEEDBACK_TABLE_NAME = aws_dynamodb_table.feedback_table.name
      EXPORT_BUCKET_NAME  = aws_s3_bucket.export_bucket.bucket
      BEDROCK_MODEL_ID    = var.bedrock_model_id
      BEDROCK_REGION      = var.bedrock_region
      AI_PROVIDER         = var.ai_provider
    }
  }
}

########################
# API GATEWAY (PROXY ONLY)
########################

resource "aws_api_gateway_rest_api" "api" {
  name = "${var.project_name}-api"
}

resource "aws_api_gateway_resource" "feedback_resource" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "feedback"
}

resource "aws_api_gateway_resource" "insights_resource" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "insights"
}

########################
# METHODS
########################

resource "aws_api_gateway_method" "feedback_post" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.feedback_resource.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_method" "feedback_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.feedback_resource.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_method" "insights_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.insights_resource.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_method" "insights_options" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.insights_resource.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

########################
# PROXY INTEGRATIONS
########################

resource "aws_api_gateway_integration" "feedback_post_integration" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.feedback_resource.id
  http_method             = aws_api_gateway_method.feedback_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.feedback_api.invoke_arn
}

resource "aws_api_gateway_integration" "feedback_options_integration" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.feedback_resource.id
  http_method             = aws_api_gateway_method.feedback_options.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.feedback_api.invoke_arn
}

resource "aws_api_gateway_integration" "insights_get_integration" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.insights_resource.id
  http_method             = aws_api_gateway_method.insights_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.feedback_api.invoke_arn
}

resource "aws_api_gateway_integration" "insights_options_integration" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.insights_resource.id
  http_method             = aws_api_gateway_method.insights_options.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.feedback_api.invoke_arn
}

########################
# LAMBDA PERMISSION
########################

resource "aws_lambda_permission" "apigw_lambda" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.feedback_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

########################
# DEPLOYMENT
########################

resource "aws_api_gateway_deployment" "deployment" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeploy = timestamp()
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.feedback_post_integration,
    aws_api_gateway_integration.feedback_options_integration,
    aws_api_gateway_integration.insights_get_integration,
    aws_api_gateway_integration.insights_options_integration
  ]
}

resource "aws_api_gateway_stage" "stage" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.deployment.id
  stage_name    = var.api_stage_name
}

########################
# OUTPUT
########################

output "api_base_url" {
  value = aws_api_gateway_stage.stage.invoke_url
}
