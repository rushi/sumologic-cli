"""Entry point for the compiled `sumologic` binary.

Reuses mcp2cli's runtime but hardcodes the SumoLogic connection, so users get a
single downloadable binary instead of installing mcp2cli and baking a config.
"""

import os
import sys

from mcp2cli import BakeConfig, _main_impl

# CIMD client metadata document. SumoLogic's authorization server fetches this URL
# server-side during /oauth2/authorize, so it must stay publicly reachable.
CLIENT_ID = os.environ.get(
    "SUMOLOGIC_OAUTH_CLIENT_ID",
    "https://rushi.github.io/sumologic-cli/client.json",
)
REDIRECT_URI = os.environ.get("SUMOLOGIC_OAUTH_REDIRECT_URI", "http://localhost:8888/callback")

DEPLOYMENTS = {
    "us1": "https://mcp.sumologic.com/mcp",
    "us2": "https://mcp.us2.sumologic.com/mcp",
    "eu": "https://mcp.eu.sumologic.com/mcp",
    "au": "https://mcp.au.sumologic.com/mcp",
    "ca": "https://mcp.ca.sumologic.com/mcp",
    "de": "https://mcp.de.sumologic.com/mcp",
    "in": "https://mcp.in.sumologic.com/mcp",
    "jp": "https://mcp.jp.sumologic.com/mcp",
    "fed": "https://mcp.fed.sumologic.com/mcp",
}
DEFAULT_DEPLOYMENT = "us2"


def resolve_mcp_url() -> str:
    override = os.environ.get("SUMOLOGIC_MCP_URL")
    if override:
        return override

    deployment = os.environ.get("SUMOLOGIC_DEPLOYMENT", DEFAULT_DEPLOYMENT).lower()
    url = DEPLOYMENTS.get(deployment)
    if url is None:
        known = ", ".join(sorted(DEPLOYMENTS))
        print(
            f"Error: unknown SUMOLOGIC_DEPLOYMENT {deployment!r}. Known: {known}",
            file=sys.stderr,
        )
        sys.exit(1)
    return url


def main() -> None:
    argv = [
        "--mcp",
        resolve_mcp_url(),
        "--oauth",
        "--oauth-client-id",
        CLIENT_ID,
        "--oauth-redirect-uri",
        REDIRECT_URI,
        "--oauth-flow",
        "authorization_code",
        *sys.argv[1:],
    ]
    _main_impl(
        argv,
        bake_config=BakeConfig(prog="sumologic", description="SumoLogic log search, dashboards, alerts, and Cloud SIEM from the command line"),
    )


if __name__ == "__main__":
    main()
