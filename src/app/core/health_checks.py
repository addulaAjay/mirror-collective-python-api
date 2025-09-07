"""
Health check utilities for monitoring service dependencies
"""
import logging
import time
import asyncio
from typing import Dict, Any, List
from enum import Enum
import boto3
from botocore.exceptions import ClientError, BotoCoreError
from openai import OpenAI

logger = logging.getLogger('app.health_checks')


class HealthStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


class HealthCheck:
    """Base health check class"""
    
    def __init__(self, name: str, timeout: float = 5.0):
        self.name = name
        self.timeout = timeout
    
    async def check(self) -> Dict[str, Any]:
        """Perform health check"""
        start_time = time.time()
        try:
            result = await asyncio.wait_for(
                self._check_implementation(),
                timeout=self.timeout
            )
            duration = time.time() - start_time
            return {
                "name": self.name,
                "status": HealthStatus.HEALTHY.value,
                "duration_ms": round(duration * 1000, 2),
                "details": result
            }
        except asyncio.TimeoutError:
            duration = time.time() - start_time
            logger.warning(f"Health check timeout for {self.name}")
            return {
                "name": self.name,
                "status": HealthStatus.UNHEALTHY.value,
                "duration_ms": round(duration * 1000, 2),
                "error": "Timeout"
            }
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Health check failed for {self.name}: {str(e)}")
            return {
                "name": self.name,
                "status": HealthStatus.UNHEALTHY.value,
                "duration_ms": round(duration * 1000, 2),
                "error": str(e)
            }
    
    async def _check_implementation(self) -> Dict[str, Any]:
        """Override this method in subclasses"""
        raise NotImplementedError


class CognitoHealthCheck(HealthCheck):
    """Health check for AWS Cognito service"""
    
    def __init__(self):
        super().__init__("cognito", timeout=10.0)
    
    async def _check_implementation(self) -> Dict[str, Any]:
        try:
            import os
            from src.app.services.cognito_service import CognitoService
            
            # Basic configuration check
            user_pool_id = os.getenv('COGNITO_USER_POOL_ID')
            client_id = os.getenv('COGNITO_CLIENT_ID')
            
            if not user_pool_id or not client_id:
                return {
                    "configured": False,
                    "error": "Missing Cognito configuration"
                }
            
            # Try to create client (this validates AWS credentials)
            client = boto3.client('cognito-idp', region_name=os.getenv('AWS_REGION', 'us-east-1'))
            
            # Simple API call to test connectivity
            response = await asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: client.describe_user_pool(UserPoolId=user_pool_id)
            )
            
            return {
                "configured": True,
                "user_pool_id": user_pool_id[:8] + "...",  # Masked for security
                "region": os.getenv('AWS_REGION', 'us-east-1'),
                "pool_name": response.get('UserPool', {}).get('Name', 'Unknown')
            }
        except (ClientError, BotoCoreError) as e:
            logger.warning(f"Cognito health check failed: {str(e)}")
            return {
                "configured": True,
                "error": f"AWS Error: {str(e)}"
            }


class OpenAIHealthCheck(HealthCheck):
    """Health check for OpenAI service"""
    
    def __init__(self):
        super().__init__("openai", timeout=15.0)
    
    async def _check_implementation(self) -> Dict[str, Any]:
        try:
            import os
            
            api_key = os.getenv('OPENAI_API_KEY')
            if not api_key:
                return {
                    "configured": False,
                    "error": "Missing OpenAI API key"
                }
            
            client = OpenAI(api_key=api_key)
            
            # Simple API call to test connectivity
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.models.list()
            )
            
            models = [model.id for model in response.data if 'gpt' in model.id][:3]
            
            return {
                "configured": True,
                "api_key_present": True,
                "available_models": models
            }
        except Exception as e:
            logger.warning(f"OpenAI health check failed: {str(e)}")
            return {
                "configured": True,
                "error": str(e)
            }


class DatabaseHealthCheck(HealthCheck):
    """Placeholder for database health check"""
    
    def __init__(self):
        super().__init__("database", timeout=5.0)
    
    async def _check_implementation(self) -> Dict[str, Any]:
        # For now, just return healthy since no database is configured
        return {
            "configured": False,
            "note": "No database configured"
        }


class HealthCheckService:
    """Service to orchestrate all health checks"""
    
    def __init__(self):
        self.checks: List[HealthCheck] = [
            CognitoHealthCheck(),
            OpenAIHealthCheck(),
            DatabaseHealthCheck()
        ]
    
    async def run_all_checks(self) -> Dict[str, Any]:
        """Run all health checks and aggregate results"""
        start_time = time.time()
        
        # Run all checks concurrently
        results = await asyncio.gather(
            *[check.check() for check in self.checks],
            return_exceptions=True
        )
        
        # Process results
        check_results: List[Dict[str, Any]] = []
        overall_status = HealthStatus.HEALTHY
        
        for result in results:
            if isinstance(result, Exception):
                check_results.append({
                    "name": "unknown",
                    "status": HealthStatus.UNHEALTHY.value,
                    "error": str(result)
                })
                overall_status = HealthStatus.UNHEALTHY
            elif isinstance(result, dict):
                # Result is a Dict[str, Any] from the check() method
                check_results.append(result)
                if result.get("status") == HealthStatus.UNHEALTHY.value:
                    overall_status = HealthStatus.UNHEALTHY
                elif result.get("status") == HealthStatus.DEGRADED.value and overall_status == HealthStatus.HEALTHY:
                    overall_status = HealthStatus.DEGRADED
        
        total_duration = time.time() - start_time
        
        return {
            "status": overall_status.value,
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            "duration_ms": round(total_duration * 1000, 2),
            "checks": check_results,
            "summary": {
                "total_checks": len(check_results),
                "healthy_checks": len([c for c in check_results if c["status"] == HealthStatus.HEALTHY.value]),
                "unhealthy_checks": len([c for c in check_results if c["status"] == HealthStatus.UNHEALTHY.value])
            }
        }