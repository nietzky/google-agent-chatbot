from unittest.mock import Mock

from chatbot import aws_tools_server as server


async def test_disabled_diagnostics_never_contact_aws(monkeypatch):
    monkeypatch.delenv("AWS_DIAGNOSTICS_ENABLED", raising=False)
    sdk = Mock()
    monkeypatch.setattr(server.boto3, "Session", sdk)
    assert not (await server.diagnostics_status())["enabled"]
    result = await server.inspect_aws_resource("ec2_status", "us-east-1", "i-123")
    assert "disabled" in result["error"]
    sdk.assert_not_called()


async def test_lambda_diagnostics_exclude_secrets_and_mutations(monkeypatch):
    monkeypatch.setenv("AWS_DIAGNOSTICS_ENABLED", "true")
    sdk = Mock()
    sdk.return_value.client.return_value.get_function_configuration.return_value = {
        "FunctionName": "test",
        "State": "Active",
        "Environment": {"Variables": {"PASSWORD": "secret"}},
    }
    monkeypatch.setattr(server.boto3, "Session", sdk)
    result = await server.inspect_aws_resource("lambda_status", "us-east-1", "test")
    assert result["State"] == "Active"
    assert "Environment" not in result and "secret" not in str(result)
    sdk.reset_mock()
    assert "error" in await server.inspect_aws_resource("delete_function", "us-east-1", "test")
    sdk.assert_not_called()
