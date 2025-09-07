#!/usr/bin/env python3
"""
Script to create DynamoDB tables for the Mirror Collective API
Run this script to set up the required DynamoDB tables in your AWS account
"""
import boto3
import os
import sys
from botocore.exceptions import ClientError

def create_users_table(dynamodb, table_name):
    """Create the users table with GSI on email"""
    try:
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {
                    'AttributeName': 'user_id',
                    'KeyType': 'HASH'  # Partition key
                }
            ],
            AttributeDefinitions=[
                {
                    'AttributeName': 'user_id',
                    'AttributeType': 'S'
                },
                {
                    'AttributeName': 'email',
                    'AttributeType': 'S'
                }
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'email-index',
                    'KeySchema': [
                        {
                            'AttributeName': 'email',
                            'KeyType': 'HASH'
                        }
                    ],
                    'Projection': {
                        'ProjectionType': 'ALL'
                    },
                    'BillingMode': 'PAY_PER_REQUEST'
                }
            ],
            BillingMode='PAY_PER_REQUEST',
            Tags=[
                {
                    'Key': 'Environment',
                    'Value': os.getenv('ENVIRONMENT', 'development')
                },
                {
                    'Key': 'Service',
                    'Value': 'mirror-collective-api'
                }
            ]
        )
        
        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False

def create_activity_table(dynamodb, table_name):
    """Create the user activity table"""
    try:
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {
                    'AttributeName': 'user_id',
                    'KeyType': 'HASH'  # Partition key
                },
                {
                    'AttributeName': 'activity_date',
                    'KeyType': 'RANGE'  # Sort key
                }
            ],
            AttributeDefinitions=[
                {
                    'AttributeName': 'user_id',
                    'AttributeType': 'S'
                },
                {
                    'AttributeName': 'activity_date',
                    'AttributeType': 'S'
                }
            ],
            BillingMode='PAY_PER_REQUEST',
            Tags=[
                {
                    'Key': 'Environment',
                    'Value': os.getenv('ENVIRONMENT', 'development')
                },
                {
                    'Key': 'Service',
                    'Value': 'mirror-collective-api'
                }
            ]
        )
        
        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False

def main():
    """Main function to create all required tables"""
    # Configuration
    region = os.getenv('AWS_REGION', 'us-east-1')
    environment = os.getenv('ENVIRONMENT', 'development')
    endpoint_url = os.getenv('DYNAMODB_ENDPOINT_URL')  # For local DynamoDB
    
    # Table names
    users_table = os.getenv('DYNAMODB_USERS_TABLE', f'users-{environment}')
    activity_table = os.getenv('DYNAMODB_ACTIVITY_TABLE', f'user_activity-{environment}')
    
    # Determine if running locally or on AWS
    is_local = endpoint_url is not None
    target = "Local DynamoDB" if is_local else "AWS DynamoDB"
    
    print(f"🚀 Creating DynamoDB tables on: {target}")
    if is_local:
        print(f"📍 Endpoint: {endpoint_url}")
    print(f"📊 Environment: {environment}")
    print(f"🌍 Region: {region}")
    print(f"👥 Users table: {users_table}")
    print(f"📈 Activity table: {activity_table}")
    print()
    
    try:
        # Initialize DynamoDB client (local or AWS)
        if is_local:
            print("🏠 Connecting to local DynamoDB...")
            dynamodb = boto3.resource(
                'dynamodb',
                endpoint_url=endpoint_url,
                region_name=region,
                aws_access_key_id='dummy',
                aws_secret_access_key='dummy'
            )
            # Test local connection
            try:
                list(dynamodb.tables.all())
                print("✅ Connected to local DynamoDB")
            except Exception as e:
                print("❌ Cannot connect to local DynamoDB. Make sure it's running:")
                print("   docker-compose -f docker-compose.local.yml up -d")
                sys.exit(1)
        else:
            print("☁️  Connecting to AWS DynamoDB...")
            dynamodb = boto3.resource('dynamodb', region_name=region)
        
        # Create tables
        users_success = create_users_table(dynamodb, users_table)
        activity_success = create_activity_table(dynamodb, activity_table)
        
        if users_success and activity_success:
            print()
            print("🎉 All tables created successfully!")
            print()
            print("📝 Add these environment variables to your .env file:")
            print(f"DYNAMODB_USERS_TABLE={users_table}")
            print(f"DYNAMODB_ACTIVITY_TABLE={activity_table}")
            print(f"AWS_REGION={region}")
            if is_local:
                print(f"DYNAMODB_ENDPOINT_URL={endpoint_url}")
                print()
                print("🏠 Local development setup:")
                print("- Tables will persist in Docker volume")
                print("- Access DynamoDB Admin UI at: http://localhost:8001")
                print("- No AWS costs for local development")
            else:
                print()
                print("💰 AWS Cost estimate:")
                print("- Pay-per-request billing (no fixed costs)")
                print("- ~$0.25 per million read requests")
                print("- ~$1.25 per million write requests")
                print("- Storage: $0.25 per GB per month")
                print("- Expected cost for small app: <$5/month")
            
        else:
            print("❌ Some tables failed to create")
            sys.exit(1)
            
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()