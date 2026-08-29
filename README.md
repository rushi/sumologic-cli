# sumologic-cli

SumoLogic's MCP server as a single downloadable binary. Log search, dashboards, alerts,
and Cloud SIEM from the command line, with no Python, no `uv`, and no MCP client.

Built on [mcp2cli](https://github.com/knowsuchagency/mcp2cli): tool schemas are fetched
and cached at runtime, so an agent pays for one subcommand's schema instead of all twenty
on every turn.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/rushi/sumologic-cli/main/install.sh | bash
```

Or grab a binary from [Releases](https://github.com/rushi/sumologic-cli/releases) directly.
Builds are published for macOS (arm64, x86_64), Linux (arm64, x86_64), and Windows (x86_64).

## Usage

```bash
sumologic --list                 # tool names plus one-line descriptions
sumologic <subcommand> --help    # full schema for that one tool

sumologic discovery---list-partitions

sumologic log-search---run-log-search \
  --query '_sourceCategory=prod/api* | count by _sourceHost' \
  --from "$(date -u -v-5M +%Y-%m-%dT%H:%M:%S)" \
  --to "$(date -u +%Y-%m-%dT%H:%M:%S)" \
  --timezone UTC \
  --limit 5
```

The first invocation opens a browser for SumoLogic authorization. Tokens are cached in
`~/.cache/mcp2cli/oauth/` and refreshed automatically.

### Log search gotchas

- `--timezone` is required even though the schema marks it optional.
- `--from` / `--to` reject relative offsets like `-5m`. Pass absolute ISO 8601 timestamps.
- Ranges over 30 minutes are rejected unless the query filters on `_sourceCategory`,
  `_collector`, `_index`, or `_view`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SUMOLOGIC_DEPLOYMENT` | `us2` | Deployment shorthand: `us1`, `us2`, `eu`, `au`, `ca`, `de`, `in`, `jp`, `fed` |
| `SUMOLOGIC_MCP_URL` | derived | Full MCP endpoint URL, overriding `SUMOLOGIC_DEPLOYMENT` |
| `SUMOLOGIC_OAUTH_CLIENT_ID` | the hosted CIMD URL | Alternative OAuth client, for orgs with CIMD disabled |
| `SUMOLOGIC_OAUTH_REDIRECT_URI` | `http://localhost:8888/callback` | OAuth callback |

## How authorization works

SumoLogic's authorization server (`https://service.<deployment>.sumologic.com`) exposes no
`registration_endpoint`, so OAuth dynamic client registration fails with `403 Forbidden`.
It does advertise `client_id_metadata_document_supported: true`, so the client ID is an
HTTPS URL pointing at a client metadata document that SumoLogic fetches server-side during
`/oauth2/authorize`.

That document is [`docs/client.json`](docs/client.json), served via GitHub Pages at
`https://rushi.github.io/sumologic-cli/client.json`. It holds no secrets: a client name, a
localhost redirect URI, and grant types. It must stay publicly reachable, or every
installed binary loses the ability to log in.

Your SumoLogic administrator must have **Enable CIMD Clients** checked under
Administration > Policies. Orgs that do not allow CIMD can register a client under
Administration > Security > OAuth Clients and point `SUMOLOGIC_OAUTH_CLIENT_ID` at it.

## Build from source

```bash
uv sync --group dev
uv run pyinstaller --onefile --name sumologic \
  --collect-all mcp2cli \
  --exclude-module mcp.cli --exclude-module typer \
  --hidden-import truststore \
  --clean --noconfirm src/sumologic_cli/__main__.py
```

Output lands in `dist/sumologic` (roughly 22 MB). Pushing a `v*` tag runs the same build
across the release matrix and publishes the artifacts.
