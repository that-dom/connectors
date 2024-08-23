from connectors.version import connectors_version
from connectors.es.cli_client import CLIClient


def test_overrides_user_agent_header():
    config = {
        "username": "elastic",
        "password": "changeme",
        "host": "http://nowhere.com:9200",
    }
    cli_client = CLIClient(config)

    assert (
        cli_client.client._headers["user-agent"]
        == f"elastic-connectors-{connectors_version()}/cli"
    )
