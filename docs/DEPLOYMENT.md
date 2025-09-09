# Deployment Guide

This guide covers deploying the Mirror Collective API to AWS with DynamoDB tables.

## 🚀 **Deployment Options**

### **Option 1: Automated via GitHub Actions (Recommended)**

The CI/CD pipeline automatically creates DynamoDB tables and deploys the application.

**Setup:**
1. Configure GitHub Secrets (see below)
2. Push to `develop` branch → deploys to staging
3. Push to `main` branch → deploys to production

**Required GitHub Secrets:**

Go to your repository → Settings → Secrets and variables → Actions, then add:

```bash
# AWS Credentials (Required)
AWS_ACCESS_KEY_ID=your-access-key-with-dynamodb-lambda-permissions
AWS_SECRET_ACCESS_KEY=your-corresponding-secret-key
AWS_REGION=us-east-1

# Staging Environment Secrets
STAGING_COGNITO_USER_POOL_ID=your-staging-cognito-pool-id
STAGING_COGNITO_CLIENT_ID=your-staging-cognito-client-id
STAGING_COGNITO_CLIENT_SECRET=your-staging-cognito-secret  # optional
STAGING_OPENAI_API_KEY=your-staging-openai-api-key

# Production Environment Secrets
PROD_COGNITO_USER_POOL_ID=your-production-cognito-pool-id
PROD_COGNITO_CLIENT_ID=your-production-cognito-client-id
PROD_COGNITO_CLIENT_SECRET=your-production-cognito-secret  # optional
PROD_OPENAI_API_KEY=your-production-openai-api-key
```

**⚠️ Important:** The AWS credentials must have permissions for DynamoDB, Lambda, API Gateway, CloudFormation, and IAM operations (see permissions section below).

### **Option 2: Manual Deployment**

For initial setup or troubleshooting.

#### **Step 1: Create DynamoDB Tables**

```bash
# Set environment variables
export AWS_REGION=us-east-1
export ENVIRONMENT=production
export DYNAMODB_USERS_TABLE=users-production
export DYNAMODB_ACTIVITY_TABLE=user_activity-production

# Create tables
python scripts/create_dynamodb_tables.py
```

#### **Step 2: Deploy Application**

```bash
# Set environment variables in .env file
COGNITO_USER_POOL_ID=your-pool-id
COGNITO_CLIENT_ID=your-client-id
OPENAI_API_KEY=your-openai-key
DYNAMODB_USERS_TABLE=users-production
DYNAMODB_ACTIVITY_TABLE=user_activity-production

# Deploy with Serverless
serverless deploy --stage production
```

## 📋 **Environment-Specific Table Names**

| Environment | Users Table | Activity Table |
|-------------|-------------|----------------|
| Local | `users-local` | `user_activity-local` |
| Development | `users-development` | `user_activity-development` |
| Staging | `users-staging` | `user_activity-staging` |
| Production | `users-production` | `user_activity-production` |

## 🔧 **Required AWS Permissions**

Your AWS user/role needs these permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:CreateTable",
                "dynamodb:DescribeTable",
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:Scan"
            ],
            "Resource": [
                "arn:aws:dynamodb:*:*:table/users-*",
                "arn:aws:dynamodb:*:*:table/user_activity-*",
                "arn:aws:dynamodb:*:*:table/users-*/index/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "lambda:*",
                "apigateway:*",
                "cloudformation:*",
                "iam:*",
                "logs:*",
                "s3:*"
            ],
            "Resource": "*"
        }
    ]
}
```

## 🧪 **Testing Environments**

### **Staging Environment**
- **Purpose**: Test features before production deployment
- **Trigger**: Push to `develop` branch
- **Table Names**: `users-staging`, `user_activity-staging`
- **Access**: Use staging API Gateway URL from deployment output

### **Production Environment**
- **Purpose**: Live application serving real users
- **Trigger**: Push to `main` branch
- **Table Names**: `users-production`, `user_activity-production`
- **Access**: Use production API Gateway URL from deployment output

## 🔍 **Verification Steps**

After deployment, verify everything works:

### **1. Check DynamoDB Tables**
```bash
aws dynamodb list-tables --region us-east-1
aws dynamodb describe-table --table-name users-production --region us-east-1
```

### **2. Test API Endpoints**
```bash
# Health check
curl https://your-api-gateway-url/health

# Detailed health (shows DynamoDB connectivity)
curl https://your-api-gateway-url/health/detailed
```

### **3. Test User Profile Creation**
```bash
# Make a chat request (requires authentication)
curl -X POST https://your-api-gateway-url/api/chat/mirror \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-jwt-token" \
  -d '{"message": "Hello!"}'
```

## 🚨 **Troubleshooting**

### **Table Creation Fails**
- Check AWS credentials and permissions
- Verify region settings
- Ensure table names don't conflict

### **Application Can't Connect to DynamoDB**
- Verify `DYNAMODB_USERS_TABLE` environment variable
- Check Lambda execution role has DynamoDB permissions
- Ensure tables exist in the correct region

### **GitHub Actions Fails**
- Check all required secrets are set
- Verify AWS credentials have sufficient permissions
- Check workflow logs for specific error messages

## 💰 **Cost Estimates**

### **DynamoDB (Pay-per-request)**
- Read requests: ~$0.25 per million
- Write requests: ~$1.25 per million
- Storage: $0.25 per GB/month
- **Expected small app cost**: <$5/month

### **Lambda + API Gateway**
- Lambda: $0.20 per 1M requests + compute time
- API Gateway: $3.50 per million requests
- **Expected small app cost**: <$10/month

### **Total Expected Cost**: <$15/month for moderate usage

## 🔄 **Database Migrations**

For schema changes, update the table creation script and redeploy:

```bash
# The script is idempotent - safe to run multiple times
python scripts/create_dynamodb_tables.py
```

⚠️ **Note**: DynamoDB schema changes are limited. Plan carefully for production.
