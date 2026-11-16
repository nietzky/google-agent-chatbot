"""Small read-only AWS account diagnostics MCP server. No arbitrary AWS calls.

Enable with AWS_DIAGNOSTICS_ENABLED=true and a standard AWS_PROFILE/AWS_REGION.
Never put AWS credentials in chat. The SDK uses the local credential provider chain.
"""

import asyncio
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cloud-help-aws-diagnostics")


# Report whether local AWS diagnostics are enabled, the configured region, and supported
# operations. This status check does not contact AWS or validate credentials.
@mcp.tool()
async def diagnostics_status() -> dict:
    """Report whether local read-only AWS diagnostics are enabled; does not test credentials."""
    return {
        "enabled": os.getenv("AWS_DIAGNOSTICS_ENABLED", "false").lower() == "true",
        "region": os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "not configured")),
        "operations": ["cloudwatch_alarms", "ec2_status", "rds_status", "lambda_status"],
        "setup": "Configure a local read-only AWS profile and AWS_REGION, then enable AWS_DIAGNOSTICS_ENABLED. Never paste credentials into chat.",
    }


# Execute one allowlisted read-only boto3 diagnostic with standard local credentials and bounded
# SDK timeouts. Return selected resource status fields; the public wrapper validates operation,
# region, and resource ID first.
def inspect(operation, region, resource_id):
    session = boto3.Session()
    config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})

    # Create a service-specific boto3 client using the selected region and shared timeout/retry
    # configuration.
    def client(service):
        return session.client(service, region_name=region, config=config)

    if operation == "ec2_status":
        response = client("ec2").describe_instance_status(
            InstanceIds=[resource_id], IncludeAllInstances=True
        )
        return {"instances": response.get("InstanceStatuses", [])}
    if operation == "cloudwatch_alarms":
        response = client("cloudwatch").describe_alarms(AlarmNames=[resource_id], MaxRecords=10)
        return {
            "alarms": [
                {
                    k: a.get(k)
                    for k in (
                        "AlarmName",
                        "StateValue",
                        "StateReason",
                        "MetricName",
                        "Namespace",
                        "StateUpdatedTimestamp",
                    )
                }
                for a in response.get("MetricAlarms", [])
            ]
        }
    if operation == "rds_status":
        response = client("rds").describe_db_instances(DBInstanceIdentifier=resource_id)
        return {
            "databases": [
                {
                    k: d.get(k)
                    for k in (
                        "DBInstanceIdentifier",
                        "DBInstanceStatus",
                        "Engine",
                        "EngineVersion",
                        "MultiAZ",
                        "AvailabilityZone",
                        "PendingModifiedValues",
                    )
                }
                for d in response.get("DBInstances", [])
            ]
        }
    response = client("lambda").get_function_configuration(FunctionName=resource_id)
    # Explicit allowlist: Lambda environment variables may contain application secrets.
    return {
        k: response.get(k)
        for k in (
            "FunctionName",
            "Runtime",
            "State",
            "StateReason",
            "LastUpdateStatus",
            "LastUpdateStatusReason",
            "MemorySize",
            "Timeout",
        )
    }


# Require diagnostics to be enabled and validate the operation and identifiers before accessing
# AWS. Run blocking boto3 work in a thread and return sanitized SDK errors without credential
# details.
@mcp.tool()
async def inspect_aws_resource(operation: str, region: str, resource_id: str) -> dict:
    """Inspect a named resource using a read-only operation: ec2_status, rds_status,
    lambda_status, or cloudwatch_alarms (exact alarm name). Requires local AWS setup.
    Ask the user for the region and resource identifier. Never changes infrastructure.
    """
    if os.getenv("AWS_DIAGNOSTICS_ENABLED", "false").lower() != "true":
        return {
            "error": "Account diagnostics are disabled. Use documentation guidance or configure a local read-only AWS profile."
        }
    if operation not in {"ec2_status", "rds_status", "lambda_status", "cloudwatch_alarms"}:
        return {"error": "Unsupported read-only operation"}
    if (
        not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region)
        or not resource_id.strip()
        or len(resource_id) > 256
    ):
        return {"error": "Provide a valid AWS region and resource identifier (1-256 characters)"}
    try:
        return await asyncio.to_thread(inspect, operation, region, resource_id)
    except ClientError as exc:
        return {
            "error": "AWS rejected the read-only request",
            "code": exc.response.get("Error", {}).get("Code", "Unknown"),
        }
    except BotoCoreError:
        return {
            "error": "AWS credentials, region or connection are unavailable. Check the local AWS profile."
        }


if __name__ == "__main__":
    mcp.run(transport="stdio")
