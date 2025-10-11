#!/usr/bin/env python3
"""
Create the echo_vault DynamoDB table for storing message metadata.
Partition key: user_id (S)
Sort key: vault_id (S) – typically <millis>-<short-uuid>
GSI: media_type-index (PK=user_id, SK=media_type#vault_id) to query by type
"""

import os
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()


def main():
    region = os.getenv("AWS_REGION", "us-east-1")
    endpoint = os.getenv("DYNAMODB_ENDPOINT_URL")
    table_name = os.getenv("DYNAMODB_ECHO_VAULT_TABLE", "echo_vault")

    client_kwargs = {"region_name": region}
    if endpoint:
        client_kwargs.update(
            {
                "endpoint_url": endpoint,
                "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID", "dummy"),
                "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY", "dummy"),
            }
        )

    dynamodb = boto3.client("dynamodb", **client_kwargs)

    try:
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "vault_id", "AttributeType": "S"},
                {"AttributeName": "media_type", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "vault_id", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "media_type-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "media_type", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=[
                {"Key": "Service", "Value": "EchoVault"},
                {"Key": "Environment", "Value": os.getenv("ENVIRONMENT", "development")},
            ],
        )
        print(f"Creating table {table_name}...")
        waiter = boto3.client("dynamodb", **client_kwargs).get_waiter("table_exists")
        waiter.wait(TableName=table_name)
        print(f"✅ {table_name} created and active")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"⚠️  {table_name} already exists")
        else:
            raise


if __name__ == "__main__":
    main()
