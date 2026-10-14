# sumologic-cli

SumoLogic's MCP server as a single downloadable binary. Log search, dashboards, alerts,
Cloud SIEM, and mobot investigations from the command line, with no Python, no `uv`, and
no MCP client.

Built on [mcp2cli](https://github.com/knowsuchagency/mcp2cli), which fetches and caches tool
schemas at runtime, so an agent pays for one subcommand's schema instead of all two dozen on
every turn.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/rushi/sumologic-cli/main/install.sh | bash
```

Or grab a binary from [Releases](https://github.com/rushi/sumologic-cli/releases) directly.
CI publishes builds for macOS (arm64, x86_64), Linux (arm64, x86_64), and Windows (x86_64).

### macOS Gatekeeper

The binaries are unsigned, so macOS quarantines anything downloaded from Releases through a
browser and refuses to run it ("cannot be opened because the developer cannot be verified").
Clear the quarantine attribute once, after checking the release checksum:

```bash
xattr -d com.apple.quarantine ~/.local/bin/sumologic
```

`install.sh` downloads with `curl`, which does not set the attribute, so this step only
applies to browser downloads.

## Usage

```bash
sumologic --help                 # commands grouped by area, global options, env vars
sumologic --search insight       # commands whose name or description matches
sumologic <subcommand> --help    # one tool's options, required ones first, with examples

sumologic discovery:list-partitions

sumologic log-search:run-log-search \
  --query '_sourceCategory=prod/api* | count by _sourceHost' \
  --from "$(date -u -v-5M +%Y-%m-%dT%H:%M:%S)" \
  --to "$(date -u +%Y-%m-%dT%H:%M:%S)" \
  --timezone UTC \
  --limit 5
```

Subcommands are named `<target>:<tool>`, after the gateway's `<target>___<tool>` wire names.

The first invocation opens a browser for SumoLogic authorization. Tokens are cached in
`~/.cache/sumologic-cli/oauth/` and refreshed automatically. SumoLogic access tokens last
five minutes, so the refresh path runs often.

The cache is deliberately separate from a stock `mcp2cli` install's
`~/.cache/mcp2cli/oauth/`. mcp2cli keys that cache by server URL alone, so two clients
pointed at the same SumoLogic endpoint under different `client_id` values would overwrite
each other's refresh token and log `Token refresh failed: 400` on every run. If you see
that message, something else is authorizing against the same endpoint with a different
client; clear the cache directory to recover.

### Tools hidden from the gateway's tool list

SumoLogic's gateway returns only part of its catalog from `tools/list`. The Cloud SIEM
(`insights:*`, `rules:*`) and mobot (`mobot-server:*`) tools are callable but only
discoverable through `x-amz-bedrock-agentcore-search`. The CLI runs a fixed set of searches
when it builds the tool cache and merges the results in, so these tools appear in `--list`
like any other. Cloud SIEM tools return `An internal error occurred` on orgs without Cloud
SIEM enabled.

The mobot tools take an `authorization` argument that the gateway fills in. The CLI hides it.

### Log search gotchas

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
  --hidden-import truststore --copy-metadata sumologic-cli \
  --clean --noconfirm src/sumologic_cli/__main__.py
```

Output lands in `dist/sumologic` (roughly 22 MB), for the current platform only.
PyInstaller does not cross-compile, which is what the CI matrix is for.

## Releasing

Push a `v*` tag. See [RELEASING.md](RELEASING.md) for the full procedure.
