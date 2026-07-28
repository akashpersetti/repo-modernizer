#!/usr/bin/env bash
# scripts/bootstrap_tf_backend.sh
set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="repomodernizer-tfstate-${ACCOUNT_ID}"
REGION="${AWS_REGION:-us-east-1}"

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=project,Value=repomodernizer}]'

aws dynamodb create-table \
  --table-name repomod-tf-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" \
  --tags Key=project,Value=repomodernizer

aws dynamodb wait table-exists --table-name repomod-tf-lock --region "$REGION"
echo "Backend bucket: ${BUCKET}"
echo "Lock table: repomod-tf-lock"
