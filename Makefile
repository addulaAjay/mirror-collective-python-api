# Mirror Collective Python API - Development Commands
# Aligned with CI pipeline in .github/workflows/ci.yml

.PHONY: help install install-dev lint format test test-cov clean run dev deploy pre-commit ci-checks security setup

help:  ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Install production dependencies
	pip install -r requirements.txt

install-dev:  ## Install development dependencies
	pip install -r requirements.txt -r requirements-dev.txt
	pre-commit install --install-hooks

# Code Quality (matches CI pipeline)
lint:  ## Run linting tools (matches CI pipeline)
	@echo "🔍 Running flake8 critical checks..."
	flake8 src/ --count --select=E9,F63,F7,F82 --show-source --statistics
	@echo "🔍 Running flake8 full checks..."
	flake8 src/ --count --exit-zero --max-complexity=10 --max-line-length=88 --statistics

type-check:  ## Run type checking with mypy (matches CI pipeline)
	@echo "🔍 Running mypy type checking..."
	mypy src/

security:  ## Run security scans (matches CI pipeline)
	@echo "🔒 Running bandit security scan..."
	bandit -r src/ -f json -o bandit-report.json
	@echo "🔒 Running safety dependency check..."
	safety check --json --output safety-report.json || true

format:  ## Format code with black and isort
	@echo "🎨 Formatting code with black..."
	black src/ tests/ scripts/
	@echo "🎨 Sorting imports with isort..."
	isort src/ tests/ scripts/

# Testing (matches CI pipeline)
test:  ## Run tests (matches CI pipeline)
	@echo "🧪 Running tests..."
	pytest tests/

test-cov:  ## Run tests with coverage (matches CI pipeline)
	@echo "🧪 Running tests with coverage..."
	pytest --cov=src --cov-report=xml --cov-report=html --cov-report=term tests/

test-integration:  ## Run integration tests
	@echo "🧪 Running integration tests..."
	python scripts/test_mirrorgpt_integration.py

# Pre-commit
pre-commit:  ## Run pre-commit hooks on all files
	@echo "🚀 Running pre-commit hooks..."
	pre-commit run --all-files

pre-commit-update:  ## Update pre-commit hooks
	@echo "🔄 Updating pre-commit hooks..."
	pre-commit autoupdate

# CI Pipeline Simulation
ci-checks:  ## Run all CI pipeline checks locally
	@echo "🚀 Running CI pipeline checks locally..."
	@echo "1️⃣ Code formatting check..."
	black --check src/ tests/ scripts/
	isort --check-only src/ tests/ scripts/
	@echo "2️⃣ Linting..."
	$(MAKE) lint
	@echo "3️⃣ Type checking..."
	$(MAKE) type-check
	@echo "4️⃣ Security scanning..."
	$(MAKE) security
	@echo "5️⃣ Testing..."
	$(MAKE) test-cov
	@echo "✅ All CI checks passed!"

# Development
clean:  ## Clean up generated files
	@echo "🧹 Cleaning up..."
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	rm -rf .coverage htmlcov/ .pytest_cache/ .mypy_cache/ dist/ build/
	rm -f bandit-report.json safety-report.json coverage.xml

run:  ## Run the application locally
	@echo "🚀 Starting local server..."
	uvicorn src.app.handler:app --reload --port 8001

dev:  ## Run in development mode with auto-reload
	@echo "🚀 Starting development server..."
	uvicorn src.app.handler:app --reload --port 8001 --log-level debug

# Database
db-create-tables:  ## Create DynamoDB tables locally
	@echo "🗄️ Creating DynamoDB tables..."
	python scripts/create_mirrorgpt_tables.py create

db-verify-tables:  ## Verify DynamoDB tables
	@echo "🔍 Verifying DynamoDB tables..."
	python scripts/create_mirrorgpt_tables.py verify

# Deployment
deploy-staging:  ## Deploy to staging environment
	@echo "🚀 Deploying to staging..."
	serverless deploy --stage staging

deploy-prod:  ## Deploy to production environment
	@echo "🚀 Deploying to production..."
	serverless deploy --stage production

deploy:  ## Deploy to development environment
	@echo "🚀 Deploying to development..."
	serverless deploy --stage dev

# Project Setup
setup:  ## Initial project setup
	@echo "🏗️ Setting up project..."
	python -m venv .venv
	@echo "📦 Installing dependencies..."
	@echo "Run the following commands:"
	@echo "  source .venv/bin/activate"
	@echo "  make install-dev"
	@echo "  make pre-commit"

setup-complete:  ## Complete setup (after activating venv)
	@echo "📦 Installing dependencies..."
	$(MAKE) install-dev
	@echo "🪝 Setting up pre-commit hooks..."
	pre-commit install --install-hooks
	@echo "✅ Setup complete! Run 'make help' to see available commands."

# Validation
validate-config:  ## Validate configuration files
	@echo "🔍 Validating configuration files..."
	python -c "import yaml; yaml.safe_load(open('serverless.yml'))" && echo "✅ serverless.yml is valid"
	python -c "import tomllib; tomllib.load(open('pyproject.toml', 'rb'))" && echo "✅ pyproject.toml is valid"
	pre-commit validate-config && echo "✅ .pre-commit-config.yaml is valid"

# Performance
performance-test:  ## Run performance analysis
	@echo "📊 Running performance analysis..."
	python performance_analysis.py

# Documentation
docs-serve:  ## Serve documentation locally (if you add docs)
	@echo "📚 Documentation not yet implemented"
	@echo "Consider adding Sphinx or MkDocs for documentation"

# All-in-one commands
check-all:  ## Run all checks (format, lint, type-check, security, test)
	@echo "🔍 Running all checks..."
	$(MAKE) format
	$(MAKE) lint
	$(MAKE) type-check
	$(MAKE) security
	$(MAKE) test-cov
	@echo "✅ All checks completed!"

quick-check:  ## Quick checks (lint and test only)
	@echo "⚡ Running quick checks..."
	$(MAKE) lint
	$(MAKE) test
	@echo "✅ Quick checks completed!"

# Environment info
env-info:  ## Show environment information
	@echo "🔧 Environment Information:"
	@echo "Python: $$(python --version)"
	@echo "pip: $$(pip --version)"
	@echo "pre-commit: $$(pre-commit --version 2>/dev/null || echo 'Not installed')"
	@echo "Current directory: $$(pwd)"
	@echo "Virtual environment: $$(echo $$VIRTUAL_ENV)"
