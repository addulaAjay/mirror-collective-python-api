# Mirror Collective Python API - Development Commands

.PHONY: help install install-dev lint format test test-cov clean run dev deploy

help:  ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Install production dependencies
	pip install -r requirements.txt

install-dev:  ## Install development dependencies
	pip install -r requirements.txt -r requirements-dev.txt
	pre-commit install

lint:  ## Run linting tools
	flake8 src/
	mypy src/
	bandit -r src/

format:  ## Format code with black and isort
	black src/
	isort src/

test:  ## Run tests
	pytest

test-cov:  ## Run tests with coverage
	pytest --cov=src --cov-report=html --cov-report=term

clean:  ## Clean up generated files
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	rm -rf .coverage htmlcov/ .pytest_cache/ .mypy_cache/

run:  ## Run the application locally
	uvicorn src.app.handler:app --reload --port 8001

dev:  ## Run in development mode with auto-reload
	uvicorn src.app.handler:app --reload --port 8001 --log-level debug

deploy:  ## Deploy to AWS using Serverless Framework
	serverless deploy

check:  ## Run all checks (lint, test, security)
	make lint
	make test
	safety check

setup:  ## Initial project setup
	python -m venv .venv
	source .venv/bin/activate && make install-dev
	echo "Setup complete! Activate venv with: source .venv/bin/activate"