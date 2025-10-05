#!/bin/bash
# Quick setup script for local development with DynamoDB

set -e  # Exit on any error

echo "🚀 Setting up Mirror Collective API for local development..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're in the right directory
if [ ! -f "serverless.yml" ]; then
    print_error "Please run this script from the project root directory"
    exit 1
fi

# Check if virtual environment is activated
if [ -z "$VIRTUAL_ENV" ]; then
    print_warning "Virtual environment not activated. Attempting to activate..."
    if [ -f ".venv/bin/activate" ]; then
        source .venv/bin/activate
        print_success "Virtual environment activated"
    else
        print_error "Virtual environment not found. Please create one first:"
        echo "python -m venv .venv && source .venv/bin/activate"
        exit 1
    fi
fi

# Create local environment file if it doesn't exist
if [ ! -f ".env.local" ]; then
    print_status "Creating .env.local file..."
    cp .env.local.template .env.local
    print_warning "Please edit .env.local and add your OpenAI API key!"
    echo "You can get one from: https://platform.openai.com/api-keys"
    echo ""
fi

# Install dependencies
print_status "Installing dependencies..."
pip install -r requirements.txt -r requirements-dev.txt > /dev/null 2>&1
print_success "Dependencies installed"

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    print_error "Docker is not running. Please start Docker first."
    exit 1
fi

# Stop any existing DynamoDB Local container
print_status "Stopping any existing DynamoDB Local container..."
docker stop dynamodb-local > /dev/null 2>&1 || true
docker rm dynamodb-local > /dev/null 2>&1 || true

# Start DynamoDB Local
print_status "Starting DynamoDB Local..."
docker run -d -p 8000:8000 --name dynamodb-local amazon/dynamodb-local \
    -jar DynamoDBLocal.jar -sharedDb -inMemory > /dev/null

# Wait for DynamoDB to start
print_status "Waiting for DynamoDB Local to start..."
sleep 5

# Check if DynamoDB Local is running
if ! curl -s http://localhost:8000 > /dev/null; then
    print_error "DynamoDB Local failed to start"
    exit 1
fi

print_success "DynamoDB Local is running on http://localhost:8000"

# Load environment variables
print_status "Loading environment variables..."
export $(cat .env.local | grep -v '^#' | xargs)

# Create database tables
print_status "Creating database tables..."

echo "  → Creating basic tables..."
python scripts/create_dynamodb_tables.py

echo "  → Creating conversation tables..."
python scripts/create_conversation_tables.py

echo "  → Creating MirrorGPT tables..."
python scripts/create_mirrorgpt_tables_clean.py

print_success "Database tables created"

# Verify tables were created
print_status "Verifying table creation..."
TABLE_COUNT=$(aws dynamodb list-tables --endpoint-url http://localhost:8000 --region us-east-1 --output json | jq '.TableNames | length')

if [ "$TABLE_COUNT" -gt 0 ]; then
    print_success "Created $TABLE_COUNT tables successfully"
    aws dynamodb list-tables --endpoint-url http://localhost:8000 --region us-east-1 --output table
else
    print_error "No tables were created"
    exit 1
fi

# Check environment setup
print_status "Checking environment configuration..."

if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "your-openai-api-key-here" ]; then
    print_warning "OpenAI API key not set. Please update .env.local with your API key"
    echo "Get one from: https://platform.openai.com/api-keys"
fi

print_success "🎉 Setup complete! Your local development environment is ready."

echo ""
echo "📋 Next steps:"
echo "1. Update .env.local with your OpenAI API key (if not done already)"
echo "2. Start the API server:"
echo "   export \$(cat .env.local | xargs) && uvicorn src.app.handler:app --reload --port 8001"
echo ""
echo "3. Test the API:"
echo "   curl http://localhost:8001/health"
echo ""
echo "4. View tables in DynamoDB Local:"
echo "   aws dynamodb list-tables --endpoint-url http://localhost:8000 --region us-east-1"
echo ""
echo "🛑 To stop DynamoDB Local later:"
echo "   docker stop dynamodb-local && docker rm dynamodb-local"
