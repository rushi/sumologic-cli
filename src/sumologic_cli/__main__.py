"""Entry point for the compiled `sumologic` binary.

Reuses mcp2cli's runtime but hardcodes the SumoLogic connection, so users get a
single downloadable binary instead of installing mcp2cli and baking a config.
"""

import argparse
import difflib
import importlib.metadata
import json
import logging
import os
import re
import sys
import textwrap
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)

    def _unlock(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)

# mcp2cli keys its OAuth token cache by server URL alone, so a stock mcp2cli
# install pointed at the same SumoLogic endpoint would share this binary's
# tokens under a different client_id and get 400 invalid_grant on every refresh.
# Claim a private cache before importing mcp2cli, which reads these at import time.
os.environ.setdefault("MCP2CLI_CACHE_DIR", str(Path.home() / ".cache" / "sumologic-cli"))
os.environ.setdefault("MCP2CLI_CONFIG_DIR", str(Path.home() / ".config" / "sumologic-cli"))

import anyio  # noqa: E402
import httpx  # noqa: E402
import httpx2  # noqa: E402
import mcp2cli  # noqa: E402
from mcp2cli import BakeConfig, _main_impl  # noqa: E402
from mcp.client.auth.exceptions import OAuthFlowError  # noqa: E402
from mcp.client.auth.utils import (  # noqa: E402
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
)
from mcp.shared.auth import OAuthToken  # noqa: E402
from mcp.types import Tool  # noqa: E402

from sumologic_cli.timerange import TimeRangeError, prepare_log_search  # noqa: E402

# SumoLogic's gateway prefixes every tool with its target name and three
# underscores (`log-search___runLogSearch`). Render that separator as a colon so
# subcommands read `log-search:run-log-search` instead of `log-search---run-log-search`.
TARGET_SEPARATOR = "___"
_to_kebab = mcp2cli.to_kebab


def to_kebab(name: str) -> str:
    return ":".join(_to_kebab(part) for part in name.split(TARGET_SEPARATOR))


mcp2cli.to_kebab = to_kebab

# The gateway's tools/list returns only part of the catalog. The Cloud SIEM
# (insights, rules) and mobot tools are reachable through tools/call but only
# discoverable via this search tool, which returns at most 10 matches per query.
SEARCH_TOOL = "x_amz_bedrock_agentcore_search"
SEARCH_QUERIES = (
    "cloud siem insights",
    "cloud siem rules",
    "mobot investigation invoke poll cancel feedback",
)
# The gateway injects this argument itself. Exposing it would make mobot
# subcommands demand a bearer token on the command line.
GATEWAY_INJECTED_ARGS = ("authorization",)

_list_all_tools = mcp2cli._list_all_tools


def _tool_from_search(raw: dict):
    schema = dict(raw.get("inputSchema") or {})
    properties = {
        key: value
        for key, value in (schema.get("properties") or {}).items()
        if key not in GATEWAY_INJECTED_ARGS
    }
    schema["properties"] = properties
    if "required" in schema:
        schema["required"] = [key for key in schema["required"] if key in properties]
    return Tool(
        name=raw["name"],
        description=raw.get("description") or "",
        inputSchema=schema,
    )


async def list_all_tools(session):
    tools = await _list_all_tools(session)
    names = {tool.name for tool in tools}
    if SEARCH_TOOL not in names:
        return tools

    for query in SEARCH_QUERIES:
        result = await session.call_tool(SEARCH_TOOL, {"query": query})
        if mcp2cli._mcp_attr(result, "isError"):
            continue
        payload = mcp2cli._mcp_attr(result, "structuredContent")
        if payload is None:
            payload = json.loads(mcp2cli._extract_content_parts(result.content) or "{}")
        for raw in payload.get("tools", []):
            if raw.get("name") in names:
                continue
            names.add(raw["name"])
            tools.append(_tool_from_search(raw))
    return tools


mcp2cli._list_all_tools = list_all_tools


class _HiddenToolWarningFilter(logging.Filter):
    """Drop the MCP SDK's warning that a called tool is missing from tools/list.

    Every call to a search-discovered tool trips it, and the tool still runs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "not listed by server" not in record.getMessage()


logging.getLogger("client").addFilter(_HiddenToolWarningFilter())

# Folded into the tool cache key. Bump whenever list_all_tools changes which
# tools it returns, so installed binaries drop catalogs cached by older builds.
CATALOG_VERSION = 3
# The catalog changes only when SumoLogic ships tools, and a cold fetch costs
# tools/list plus one search per SEARCH_QUERIES entry. --refresh forces a fetch.
CATALOG_TTL_SECONDS = 24 * 60 * 60

# Exit codes callers can branch on without parsing stderr.
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_INVALID = 4
EXIT_TRANSIENT = 5

# SUMOLOGIC_READONLY=1 keeps only these, so a write tool the gateway adds later
# stays hidden until it is reviewed and listed here.
READONLY_COMMANDS = (
    "*:get-*",
    "*:list-*",
    "*-read-*",
    "*-search",
    "log-search:run-log-search",
    "mobot-server:mobot-invoke",
    "mobot-server:mobot-poll",
)
LOG_SEARCH_TOOL = "log-search___runLogSearch"


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return None
    return value not in ("0", "false", "no", "off")


def is_readonly() -> bool:
    return _env_flag("SUMOLOGIC_READONLY") is True


def is_interactive() -> bool:
    """Report whether a person is present to complete a browser login.

    Agent harnesses and CI capture stderr instead of attaching a terminal, and
    a browser login there blocks for minutes waiting on a callback nobody sees.
    stderr stays a terminal when a person pipes stdin or stdout.
    """
    forced = _env_flag("SUMOLOGIC_NONINTERACTIVE")
    if forced is not None:
        return not forced
    return sys.stderr.isatty()

PROG = "sumologic"
DESCRIPTION = "SumoLogic log search, dashboards, alerts, Cloud SIEM, and mobot from the command line."
REPO_URL = "https://github.com/rushi/sumologic-cli"
HELP_WIDTH = 100

# Commands are grouped by the gateway target before the colon, in this order.
# Targets missing here land under "Other".
COMMAND_GROUPS = {
    "log-search": "Log search",
    "discovery": "Discovery (find _sourceCategory values, fields, partitions)",
    "dashboards": "Dashboards",
    "alerts": "Alerts library",
    "insights": "Cloud SIEM insights",
    "rules": "Cloud SIEM rules",
    "mobot-server": "Mobot investigations",
}

# One-line summaries for the top-level listing where the server's first sentence
# is a caveat or a typo rather than a description.
COMMAND_SUMMARIES = {
    "x-amz-bedrock-agentcore-search": "Search the gateway's full tool catalog by keyword",
    "log-search:run-log-search": "Run a log search query over a time range and return results",
    "alerts:alerts-read-by-id": "Get an alert or folder from the alerts library",
    "alerts:alerts-search": "Search the alerts library for an alert or folder",
    "insights:get-insights": "List Insights matching a query (capped at 10,000 results)",
}

COMMAND_EXAMPLES = {
    "log-search:run-log-search": """\
examples:
  sumologic log-search:run-log-search \\
    --query '_sourceCategory=prod/api | count by _sourceHost' \\
    --from -15m --to now --limit 20

  sumologic log-search:run-log-search \\
    --query '_sourceCategory=prod/api error' \\
    --from 2026-06-08T00:00:00Z --to 2026-06-08T02:00:00Z

notes:
  --from and --to take ISO 8601 times, 'now', or offsets such as -15m, -2h, '3d ago'.
  The CLI resolves them in --timezone, which defaults to UTC.
  Ranges over 30 minutes need a _sourceCategory, _collector, _index, or _view filter.
  The CLI rejects other ranges with exit code 4 before calling the gateway.
  Run discovery:list-partitions first to find _sourceCategory values.""",
    "mobot-server:mobot-invoke": """\
examples:
  sumologic mobot-server:mobot-invoke --question 'Why did 5xx errors spike in the last hour?'

  The response carries a conversation_id and message_id. Poll for the answer with:
  sumologic mobot-server:mobot-poll --conversation-id <id> --message-id <id> --wait-seconds 60""",
}

GLOBAL_OPTIONS = (
    ("--list", "List commands with one-line descriptions"),
    ("--search PATTERN", "List commands whose name or description contains PATTERN"),
    ("--json", "Emit the full MCP result envelope as JSON, including isError"),
    ("--pretty", "Pretty-print JSON output"),
    ("--raw", "Print the raw response body"),
    ("--head N", "Limit output to the first N records (arrays) or N lines (text)"),
    ("--refresh", "Re-fetch the tool catalog instead of using the 24-hour cache"),
    ("--version", "Print the sumologic and mcp2cli versions"),
    ("-h, --help", "Show this help, or a command's options with <command> --help"),
)

ENVIRONMENT = (
    ("SUMOLOGIC_DEPLOYMENT", "Deployment: us1, us2 (default), eu, au, ca, de, in, jp, fed"),
    ("SUMOLOGIC_MCP_URL", "Full MCP endpoint URL, overriding SUMOLOGIC_DEPLOYMENT"),
    ("SUMOLOGIC_OAUTH_CLIENT_ID", "OAuth client for orgs with CIMD clients disabled"),
    ("SUMOLOGIC_OAUTH_REDIRECT_URI", "OAuth callback (default http://localhost:8888/callback)"),
    ("SUMOLOGIC_READONLY", "Set to 1 to keep only commands that read data"),
    ("SUMOLOGIC_NONINTERACTIVE", "1 never opens a browser for login, 0 always may (default: auto)"),
)

EXIT_CODES = (
    ("0", "Success"),
    (str(EXIT_TOOL_ERROR), "The tool reported an error"),
    (str(EXIT_USAGE), "Bad command line: unknown command or option"),
    (str(EXIT_AUTH), f"Authorization required or rejected. Run '{PROG} auth:login'"),
    (str(EXIT_INVALID), "Arguments rejected locally before calling the gateway"),
    (str(EXIT_TRANSIENT), "Network failure, timeout, or 5xx/429 from the gateway. Retry"),
)


def _summary(cmd) -> str:
    if cmd.name in COMMAND_SUMMARIES:
        return COMMAND_SUMMARIES[cmd.name]
    first_sentence = cmd.description.split(". ")[0].strip().rstrip(".")
    return textwrap.shorten(first_sentence, width=70, placeholder="...")


def _rows(pairs, indent: int) -> list[str]:
    width = max(len(left) for left, _ in pairs) + 3
    return [f"{' ' * indent}{left.ljust(width)}{right}" for left, right in pairs]


def format_top_help(commands) -> str:
    groups: dict[str, list] = {title: [] for title in COMMAND_GROUPS.values()}
    groups["Other"] = []
    for cmd in commands:
        target = cmd.name.split(":")[0] if ":" in cmd.name else None
        groups[COMMAND_GROUPS.get(target, "Other")].append(cmd)

    name_width = max(len(cmd.name) for cmd in commands) + 3
    lines = [
        f"usage: {PROG} [options] <command> [command options]",
        "",
        DESCRIPTION,
        "",
        "commands:",
        "  Authentication",
        *(
            f"    {name.ljust(name_width)}{summary}"
            for name, (_, summary) in AUTH_COMMANDS.items()
        ),
        "",
    ]
    for title, cmds in groups.items():
        if not cmds:
            continue
        lines.append(f"  {title}")
        lines.extend(
            f"    {cmd.name.ljust(name_width)}{_summary(cmd)}"
            for cmd in sorted(cmds, key=lambda cmd: cmd.name)
        )
        lines.append("")

    lines.append("options (place before <command>):")
    lines.extend(_rows(GLOBAL_OPTIONS, indent=2))
    lines.append("")
    lines.append("environment:")
    lines.extend(_rows(ENVIRONMENT, indent=2))
    lines.append("")
    lines.append("exit codes:")
    lines.extend(_rows(EXIT_CODES, indent=2))
    lines.append("")
    lines.append(f"Run '{PROG} <command> --help' for a command's options. Docs: {REPO_URL}")
    return "\n".join(lines) + "\n"


def _report_unknown_command(parser: argparse.ArgumentParser, names: list[str], message: str) -> None:
    match = re.search(r"invalid choice: '([^']*)'", message)
    if match is None:
        parser.print_usage(sys.stderr)
        parser.exit(2, f"{PROG}: error: {message}\n")

    typed = match.group(1)
    # Pre-0.2 names used "---" where commands now use ":".
    candidates = difflib.get_close_matches(typed.replace("---", ":"), names, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(candidates)}?" if candidates else ""
    if is_readonly():
        hint += " SUMOLOGIC_READONLY is set, so commands that change data are hidden."
    parser.exit(2, f"{PROG}: unknown command '{typed}'.{hint}\nRun '{PROG} --help' to see all commands.\n")


def _customize_command_parser(sub: argparse.ArgumentParser, cmd) -> None:
    required_names = {p.original_name for p in cmd.params if p.required}
    required_dests = {mcp2cli._param_dest(p) for p in cmd.params if p.original_name in required_names}

    # mcp2cli never marks tool inputs argparse-required, since --stdin can supply
    # them instead. Move them into their own section so they stand out.
    if required_dests:
        required_group = sub.add_argument_group("required options")
        sub._action_groups.remove(required_group)
        sub._action_groups.insert(sub._action_groups.index(sub._optionals), required_group)
        for action in list(sub._optionals._group_actions):
            if action.dest in required_dests:
                sub._optionals._group_actions.remove(action)
                required_group._group_actions.append(action)
        required_flags = " ".join(
            f"{action.option_strings[0]} {action.metavar or action.dest.upper()}"
            for action in required_group._group_actions
        )
        sub.usage = f"{PROG} [options] {cmd.name} {required_flags} [command options]"

    sub.formatter_class = argparse.RawDescriptionHelpFormatter
    sub.description = textwrap.fill(cmd.description, width=HELP_WIDTH - 20)
    epilog = [COMMAND_EXAMPLES[cmd.name]] if cmd.name in COMMAND_EXAMPLES else []
    epilog.append(
        f"Output options such as --json, --pretty, and --head go before the command:\n"
        f"  {PROG} --pretty {cmd.name} ..."
    )
    sub.epilog = "\n\n".join(epilog)


_build_argparse = mcp2cli.build_argparse

# argparse takes a dash-led token as an option value only when it looks like a
# negative number. Widen that to any digit after the dash, so `--from -15m`
# parses. No option name starts with a digit.
DASH_VALUE = re.compile(r"^-\d")


def build_argparse(commands, pre_parser, **kwargs) -> argparse.ArgumentParser:
    parser = _build_argparse(commands, pre_parser, **kwargs)
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    subparsers.metavar = "<command>"
    names = sorted([*subparsers.choices, *AUTH_COMMANDS])

    parser.format_help = lambda: format_top_help(commands)
    parser.error = lambda message: _report_unknown_command(parser, names, message)
    parser._negative_number_matcher = DASH_VALUE
    for sub in subparsers.choices.values():
        sub._negative_number_matcher = DASH_VALUE
        _customize_command_parser(sub, sub.get_default("_cmd"))
    return parser


mcp2cli.build_argparse = build_argparse


class AuthRequired(BaseException):
    """Raised instead of opening a browser when no one can complete the login.

    A BaseException so mcp2cli's `except Exception` transport fallbacks pass it
    through instead of retrying the login over SSE.
    """


AUTH_REQUIRED_MESSAGE = (
    f"Error: SumoLogic authorization required, and this session cannot open a browser. "
    f"Ask the user to run '{PROG} auth:login' in a terminal, then retry."
)

_build_oauth_provider = mcp2cli.build_oauth_provider


async def refuse_redirect(auth_url: str) -> None:
    raise AuthRequired()


def build_oauth_provider(server_url: str, *, allow_browser: bool | None = None, **kwargs):
    """Build the provider without binding the OAuth callback port.

    mcp2cli binds it while building the provider, on every run, so a second
    concurrent command fails with "Address already in use" even when its token
    is valid. Build with manual_callback, which binds nothing, and build
    mcp2cli's browser provider only once a login actually starts.
    """
    provider = _build_oauth_provider(server_url, **{**kwargs, "manual_callback": True})
    if allow_browser is None:
        allow_browser = is_interactive()
    if not allow_browser:
        provider.context.redirect_handler = refuse_redirect
        return provider

    browser_contexts = []

    async def redirect_handler(auth_url: str) -> None:
        browser = _build_oauth_provider(server_url, **{**kwargs, "manual_callback": False})
        browser_contexts.append(browser.context)
        await browser.context.redirect_handler(auth_url)

    async def callback_handler():
        return await browser_contexts.pop().callback_handler()

    provider.context.redirect_handler = redirect_handler
    provider.context.callback_handler = callback_handler
    return provider


mcp2cli.build_oauth_provider = build_oauth_provider

_HTTP_ERRORS = (httpx.HTTPStatusError, httpx2.HTTPStatusError)
_TRANSIENT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx2.TimeoutException,
    httpx2.NetworkError,
    httpx2.RemoteProtocolError,
    TimeoutError,
    ConnectionError,
)
_AUTH_STATUS = re.compile(r"\b40[13]\b")


def _classify_failure(leaves: list[BaseException], message: str) -> tuple[int, str]:
    statuses = {leaf.response.status_code for leaf in leaves if isinstance(leaf, _HTTP_ERRORS)}
    if statuses & {401, 403} or _AUTH_STATUS.search(message):
        return EXIT_AUTH, f". The gateway rejected the credentials. Run '{PROG} auth:login'."
    is_transient = (
        any(status >= 500 or status == 429 for status in statuses)
        or any(isinstance(leaf, _TRANSIENT_ERRORS) or mcp2cli._is_disconnect(leaf) for leaf in leaves)
    )
    if is_transient:
        return EXIT_TRANSIENT, ". The failure looks transient. Retry."
    return EXIT_TOOL_ERROR, ""


def _with_exit_codes(fn, source: str):
    """Wrap an MCP coroutine so its failures exit with a code that names the kind of failure.

    mcp2cli's _run_mcp_clean exits 1 for every failure but passes a lone
    SystemExit through, so classify here and raise that.
    """

    async def run():
        try:
            return await fn()
        except BaseException as exc:
            leaves = mcp2cli._exc_leaves(exc)
            if any(isinstance(leaf, AuthRequired) for leaf in leaves):
                print(AUTH_REQUIRED_MESSAGE, file=sys.stderr)
                raise SystemExit(EXIT_AUTH) from None
            is_exit = any(isinstance(leaf, (SystemExit, KeyboardInterrupt)) for leaf in leaves)
            if is_exit or os.environ.get("MCP2CLI_DEBUG"):
                raise
            message = mcp2cli._exc_message(exc)
            code, hint = _classify_failure(leaves, message)
            print(f"Error: cannot use MCP server at {source}: {message}{hint}", file=sys.stderr)
            raise SystemExit(code) from None

    return run


_run_mcp_clean = mcp2cli._run_mcp_clean
mcp2cli._run_mcp_clean = lambda fn, source: _run_mcp_clean(_with_exit_codes(fn, source), source)

_dispatch_mcp_call = mcp2cli._dispatch_mcp_call


def dispatch_mcp_call(source, is_stdio, auth_headers, env_vars, tool_name, arguments, *args, **kwargs):
    if tool_name == LOG_SEARCH_TOOL and arguments is not None:
        try:
            arguments = prepare_log_search(arguments)
        except TimeRangeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(EXIT_INVALID)
    return _dispatch_mcp_call(source, is_stdio, auth_headers, env_vars, tool_name, arguments, *args, **kwargs)


mcp2cli._dispatch_mcp_call = dispatch_mcp_call


def print_version() -> None:
    try:
        version = importlib.metadata.version("sumologic-cli")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    print(f"{PROG} {version} (mcp2cli {mcp2cli.__version__})")

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
        sys.exit(EXIT_USAGE)
    return url


# Refresh this long before the access token's recorded expiry, so a token that
# is valid when checked does not lapse mid-request.
EXPIRY_MARGIN_SECONDS = 30


class AuthError(Exception):
    pass


def _token_storage(mcp_url: str) -> "mcp2cli.FileTokenStorage":
    return mcp2cli.FileTokenStorage(mcp_url)


def _read_tokens(storage) -> "OAuthToken | None":
    return anyio.run(storage.get_tokens)


def _format_expiry(storage) -> str:
    expires_at = storage.get_expires_at()
    if expires_at is None:
        return "unknown expiry"
    stamp = datetime.fromtimestamp(expires_at).astimezone().strftime("%H:%M:%S %Z")
    return f"expires {stamp}"


async def _get_json(client: httpx2.AsyncClient, urls: list[str]) -> dict:
    for url in urls:
        response = await client.get(url)
        if response.status_code == 200:
            return response.json()
    raise AuthError(f"OAuth metadata discovery failed, tried: {', '.join(urls)}")


async def _discover_token_endpoint(client: httpx2.AsyncClient, mcp_url: str) -> str:
    """Find the token endpoint through RFC 9728 and RFC 8414 discovery.

    The MCP SDK falls back to `<mcp host>/token` in a process that has not run
    discovery yet, and SumoLogic's gateway rejects that with a 400, so a cached
    refresh token never gets used. Discover the endpoint the way a full login does.
    """
    resource = await _get_json(
        client, build_protected_resource_metadata_discovery_urls(None, mcp_url)
    )
    auth_server_url = resource["authorization_servers"][0]
    metadata = await _get_json(
        client, build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, mcp_url)
    )
    return metadata["token_endpoint"]


async def _refresh(mcp_url: str) -> None:
    storage = _token_storage(mcp_url)
    tokens = await storage.get_tokens()
    client_info = await storage.get_client_info()
    if tokens is None or not tokens.refresh_token or client_info is None:
        raise AuthError(f"No cached refresh token. Run '{PROG} auth:login'.")

    async with httpx2.AsyncClient(timeout=30) as client:
        token_endpoint = await _discover_token_endpoint(client, mcp_url)
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": client_info.client_id,
            },
        )

    if response.status_code != 200:
        try:
            error = response.json().get("error", response.text)
        except ValueError:
            error = response.text
        raise AuthError(
            f"Token refresh failed: {response.status_code} {error}. Run '{PROG} auth:login'."
        )

    refreshed = OAuthToken.model_validate_json(response.content)
    # RFC 6749 section 6 lets the server omit refresh_token when the old one stays valid.
    if not refreshed.refresh_token:
        refreshed = refreshed.model_copy(update={"refresh_token": tokens.refresh_token})
    await storage.set_tokens(refreshed)


@contextmanager
def refresh_lock():
    """Serialize token refreshes across processes.

    Agents run several commands at once. Two processes refreshing with the same
    refresh token race, and if SumoLogic rotates it the loser gets invalid_grant
    and falls through to a browser login.
    """
    mcp2cli.OAUTH_DIR.mkdir(parents=True, exist_ok=True)
    with open(mcp2cli.OAUTH_DIR / "refresh.lock", "a+") as handle:
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)


def _is_expired(storage) -> bool:
    expires_at = storage.get_expires_at()
    return expires_at is not None and expires_at - EXPIRY_MARGIN_SECONDS <= time.time()


def refresh_if_expired(mcp_url: str) -> None:
    """Refresh an expired access token before mcp2cli sees it.

    Without this, every run more than five minutes after the last one hits the
    SDK's broken refresh path and opens a browser for a full login. A failure
    here is silent, since mcp2cli's own login flow still runs as the fallback.
    """
    storage = _token_storage(mcp_url)
    if not _is_expired(storage):
        return
    with refresh_lock():
        # Another process may have refreshed while this one waited for the lock.
        if not _is_expired(storage):
            return
        try:
            anyio.run(_refresh, mcp_url)
        except (AuthError, httpx2.HTTPError):
            pass


def auth_refresh(mcp_url: str) -> None:
    try:
        with refresh_lock():
            anyio.run(_refresh, mcp_url)
    except (AuthError, httpx2.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_AUTH)
    print(f"Access token refreshed, {_format_expiry(_token_storage(mcp_url))}.", file=sys.stderr)


async def _login(mcp_url: str) -> None:
    # An explicit login always opens the browser, even from a non-interactive session.
    provider = build_oauth_provider(
        mcp_url,
        allow_browser=True,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        flow="authorization_code",
    )
    # Any authenticated MCP request works. The gateway answers it with a 401,
    # which drives the provider through discovery and the browser flow.
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": PROG, "version": "login"},
        },
    }
    async with httpx2.AsyncClient(auth=provider, timeout=330) as client:
        response = await client.post(
            mcp_url,
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )
    if response.status_code != 200:
        raise AuthError(f"Login failed: the gateway answered {response.status_code}.")


def auth_login(mcp_url: str) -> None:
    storage = _token_storage(mcp_url)
    storage.clear_tokens()
    try:
        anyio.run(_login, mcp_url)
    except (AuthError, httpx2.HTTPError, OAuthFlowError, RuntimeError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_AUTH)
    if _read_tokens(storage) is None:
        print("Error: login finished without storing a token.", file=sys.stderr)
        sys.exit(EXIT_AUTH)
    print(f"Logged in to {mcp_url}, access token {_format_expiry(storage)}.", file=sys.stderr)


AUTH_COMMANDS = {
    "auth:login": (auth_login, "Log in through the browser, replacing any cached tokens"),
    "auth:refresh": (auth_refresh, "Exchange the cached refresh token for a new access token"),
}


def main() -> None:
    args = sys.argv[1:] or ["--help"]
    if args[0] == "--version":
        print_version()
        return

    if args[0] in AUTH_COMMANDS:
        handler, summary = AUTH_COMMANDS[args[0]]
        if len(args) > 1:
            if args[1] in ("-h", "--help"):
                print(f"usage: {PROG} {args[0]}\n\n{summary}.")
                return
            print(f"{PROG}: {args[0]} takes no arguments.", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        handler(resolve_mcp_url())
        return

    mcp_url = resolve_mcp_url()
    refresh_if_expired(mcp_url)
    cache_key = mcp2cli.cache_key_for({"source": mcp_url, "catalog": CATALOG_VERSION})
    argv = [
        "--mcp",
        mcp_url,
        "--cache-key",
        cache_key,
        "--oauth",
        "--oauth-client-id",
        CLIENT_ID,
        "--oauth-redirect-uri",
        REDIRECT_URI,
        "--oauth-flow",
        "authorization_code",
        "--cache-ttl",
        str(CATALOG_TTL_SECONDS),
        *args,
    ]
    include = list(READONLY_COMMANDS) if is_readonly() else []
    _main_impl(
        argv,
        bake_config=BakeConfig(prog=PROG, description=DESCRIPTION, include=include),
    )


if __name__ == "__main__":
    main()
