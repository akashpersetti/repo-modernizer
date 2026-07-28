#!/usr/bin/env bash
set -euo pipefail

TABLE_NAME="${DDB_TABLE_CHECKPOINTS:-repomod-checkpoints}"
REGION="${AWS_REGION:-us-east-1}"

aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION"

aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"

aws dynamodb update-time-to-live \
  --table-name "$TABLE_NAME" \
  --time-to-live-specification "Enabled=true,AttributeName=ttl" \
  --region "$REGION"

echo "Table $TABLE_NAME created with TTL on 'ttl' attribute."
