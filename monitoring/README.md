# External Monitoring Setup

This directory contains external monitoring configurations that should be deployed separately from the application.

## Architecture

```
┌─────────────────┐    ┌─────────────┐    ┌─────────────┐
│   Application   │───▶│  Prometheus │───▶│   Grafana   │
│  (FastAPI)      │    │  (Metrics)  │    │(Dashboard)  │
└─────────────────┘    └─────────────┘    └─────────────┘
         │                       │                │
         ▼                       ▼                ▼
┌─────────────────┐    ┌─────────────┐    ┌─────────────┐
│   Log Shipping  │    │ Alertmanager│    │  Slack/Email│
│ (Fluentd/Vector)│    │  (Alerts)   │    │(Notifications)│
└─────────────────┘    └─────────────┘    └─────────────┘
```

## Components

### 1. Application Metrics
- **Endpoint**: `/metrics` (basic health metrics)
- **External Collection**: Use AWS CloudWatch, DataDog, or Prometheus
- **Logs**: Structured JSON logs for external aggregation

### 2. Infrastructure Monitoring
- **AWS CloudWatch**: Native AWS monitoring
- **Container Metrics**: Docker/ECS metrics
- **Database Metrics**: RDS/DynamoDB metrics

### 3. Application Performance Monitoring (APM)
- **Recommendation**: DataDog APM, New Relic, or AWS X-Ray
- **Traces**: Request tracing across services
- **Performance**: Response times, throughput, errors

## Setup Instructions

### Option 1: AWS Native (Recommended for AWS deployment)
```bash
# Enable CloudWatch monitoring
aws logs create-log-group --log-group-name /aws/lambda/mirror-collective-api
aws cloudwatch put-metric-alarm --alarm-name "API-HighErrorRate" --alarm-description "High error rate" --metric-name ErrorRate --namespace AWS/Lambda --statistic Average --period 300 --threshold 5.0 --comparison-operator GreaterThanThreshold
```

### Option 2: Self-hosted Prometheus Stack
```bash
# Deploy monitoring stack
docker-compose -f docker-compose.monitoring.yml up -d

# Access dashboards
# Prometheus: http://localhost:9090
# Grafana: http://localhost:3000 (admin/admin)
# Alertmanager: http://localhost:9093
```

### Option 3: SaaS Solutions
- **DataDog**: Add DataDog agent to containers
- **New Relic**: Install New Relic Python agent
- **Honeycomb**: Use OpenTelemetry integration

## Metrics to Monitor

### Golden Signals
1. **Latency**: Response time percentiles (p50, p95, p99)
2. **Traffic**: Requests per second
3. **Errors**: Error rate percentage
4. **Saturation**: CPU, memory, connection usage

### Business Metrics
- User registrations per hour
- Chat messages per hour
- Authentication success rate
- OpenAI API usage and costs

### Infrastructure Metrics
- Lambda cold starts (if using AWS Lambda)
- Database connection pool usage
- External API response times (Cognito, OpenAI)

## Alerting Rules

### Critical Alerts
- API completely down (5xx > 90% for 2 minutes)
- Database connection failures
- Authentication service failures

### Warning Alerts
- High response times (p95 > 2s for 5 minutes)
- Error rate > 5% for 5 minutes
- High memory usage > 80% for 10 minutes

### Info Alerts
- High API usage (rate limiting triggered)
- High OpenAI token consumption
- New deployment notifications

## Log Management

### Structured Logging
The application outputs structured JSON logs in production:
```json
{
  "timestamp": "2024-01-01T12:00:00Z",
  "level": "INFO",
  "message": "User authentication successful",
  "service": "mirror-collective-api",
  "user_id": "user-123",
  "request_id": "req-456",
  "duration_ms": 150
}
```

### Log Aggregation Options
1. **AWS CloudWatch Logs**: Native AWS solution
2. **ELK Stack**: Elasticsearch, Logstash, Kibana
3. **DataDog Logs**: SaaS log management
4. **Splunk**: Enterprise log analysis

## Security Monitoring

### What to Monitor
- Failed authentication attempts
- Rate limiting triggers  
- Unusual API usage patterns
- Error spikes that could indicate attacks

### Tools
- **AWS GuardDuty**: Threat detection
- **CloudTrail**: API access logging
- **WAF Logs**: Web application firewall logs

## Cost Monitoring

### OpenAI Usage
- Token consumption tracking
- Cost per request analysis
- Usage trend monitoring

### AWS Costs
- Lambda execution costs
- Data transfer costs
- Storage costs

## Performance Monitoring

### Key Metrics
- Cold start frequency (Lambda)
- Database query performance
- External API dependencies
- Cache hit rates

### Tools
- **AWS X-Ray**: Distributed tracing
- **DataDog APM**: Application performance monitoring
- **New Relic**: Full-stack observability

## Getting Started

1. **Development**: Use the basic `/metrics` endpoint and local logs
2. **Staging**: Deploy monitoring stack with docker-compose
3. **Production**: Use cloud-native monitoring (CloudWatch) or enterprise SaaS

## Best Practices

1. **Keep monitoring external**: Don't couple monitoring with application code
2. **Use structured logging**: JSON format for better parsing
3. **Monitor user experience**: Not just technical metrics
4. **Set up proper alerting**: Avoid alert fatigue
5. **Regular review**: Update monitoring as application evolves