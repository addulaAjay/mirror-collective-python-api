# Mirror Collective Python API (MirrorGPT & Core Services)

A high-performance FastAPI back-end providing the intelligence behind the Mirror Collective app. Built for serverless deployment on AWS Lambda but highly optimized for local development.

## 🌟 Key Features

- **MirrorGPT Orchestrator**: Multi-stage intelligence flow using OpenAI to analyze user data and generate archetypes.
- **Dynamic Quiz Engine**: Managed quiz results and user archetype profiles.
- **Anonymous Linking**: Seamlessly migrate anonymous quiz data to authenticated accounts.
- **Health Monitoring**: Detailed system health checks including DynamoDB connectivity and AI service status.
- **Serverless Ready**: Native Mangum integration for AWS Lambda deployment.

---

## 💻 Local Development Setup

The easiest way to get started is using the provided automated setup script.

### Prerequisites
- Python 3.12+
- Docker (for DynamoDB Local)
- AWS CLI (for local table management)

### Quick Start (One Command)
```bash
./setup-local.sh
```
*This script will: create your virtual environment, install dependencies, start DynamoDB Local in Docker, and initialize all required tables.*

### Manual Setup
If you prefer to perform steps individually:
1. **Environment**: `cp .env.example .env.local` and add your `OPENAI_API_KEY`.
2. **Dependencies**: `pip install -r requirements.txt`
3. **Database**: Start DynamoDB Local and run table creation scripts:
   ```bash
   python scripts/create_dynamodb_tables.py
   python scripts/create_conversation_tables.py
   python scripts/create_mirrorgpt_tables_clean.py
   ```
4. **Run Server**:
   ```bash
   export $(cat .env.local | xargs)
   uvicorn src.app.handler:app --reload --port 8001
   ```

---

## 🚀 Environment Deployment

The API uses the **Serverless Framework** for cloud deployments.

### Initial Configuration
Ensure you have the Serverless Framework installed and AWS credentials configured.

### Deploying to a Specific Environment
Use the `STAGE` variable to target different environments:

```bash
# Deploy to Staging
STAGE=staging serverless deploy

# Deploy to Production
STAGE=production serverless deploy
```

### Environment Variables (.env)
Each environment should have a corresponding configuration. Reference `.env.example` for required keys:
- `DYNAMODB_ENDPOINT_URL`: (Optional) Only used for local dev.
- `OPENAI_API_KEY`: Required for MirrorGPT functions.
- `AWS_REGION`: Target region (e.g., `us-east-1`).
- `ENVIRONMENT`: set to `production`, `staging`, or `development`.

---

## 🛠 Project Structure

- `src/app/api/`: API route definitions and Pydantic models.
- `src/app/services/`: Business logic (DynamoDB, User Linking, MirrorGPT).
- `src/app/core/`: Security, config, and health check logic.
- `scripts/`: Database migrations and utility scripts.
- `tests/`: Integration and unit tests.

## 🔗 Related Documentation
- [Local Development Deep Dive](LOCAL_DEVELOPMENT.md)
- [Postman Testing Guide](POSTMAN_TESTING_GUIDE.md)
