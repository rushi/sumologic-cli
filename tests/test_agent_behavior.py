import fnmatch
import socket

import httpx
import pytest

from sumologic_cli import __main__ as cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SUMOLOGIC_NONINTERACTIVE", "SUMOLOGIC_READONLY"):
        monkeypatch.delenv(name, raising=False)


LOG_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in ("query", "from", "to", "timezone")},
}


def _parse(argv):
    commands = cli.mcp2cli.extract_mcp_commands(
        [{"name": cli.LOG_SEARCH_TOOL, "description": "Search.", "inputSchema": LOG_SEARCH_SCHEMA}]
    )
    return cli.build_argparse(commands, cli.argparse.ArgumentParser(add_help=False)).parse_args(argv)


def test_dash_led_values_parse_as_values():
    args = _parse(["log-search:run-log-search", "--from", "-15m", "--to", "now", "--query", "-1h error"])
    assert (getattr(args, "from"), args.to, args.query) == ("-15m", "now", "-1h error")


@pytest.mark.parametrize(
    ("env", "tty", "expected"),
    [
        ({}, True, True),
        ({}, False, False),
        ({"SUMOLOGIC_NONINTERACTIVE": "0"}, False, True),
        ({"SUMOLOGIC_NONINTERACTIVE": "1"}, True, False),
    ],
)
def test_is_interactive(monkeypatch, env, tty, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: tty)
    assert cli.is_interactive() is expected


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("dashboards:create-dashboard", False),
        ("dashboards:update-dashboard", False),
        ("insights:update-insight-status", False),
        ("rules:create-threshold-rule", False),
        ("mobot-server:submit-hitl-feedback", False),
        ("mobot-server:mobot-cancel", False),
        ("alerts:alerts-read-by-id", True),
        ("alerts:alerts-search", True),
        ("dashboards:get-dashboard", True),
        ("discovery:list-partitions", True),
        ("insights:get-all-insights", True),
        ("log-search:run-log-search", True),
        ("mobot-server:mobot-invoke", True),
        ("mobot-server:mobot-poll", True),
        ("x-amz-bedrock-agentcore-search", True),
    ],
)
def test_readonly_commands(name, allowed):
    assert any(fnmatch.fnmatch(name, pattern) for pattern in cli.READONLY_COMMANDS) is allowed


def _status_error(status):
    request = httpx.Request("POST", "https://example.test/mcp")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize(
    ("leaf", "code"),
    [
        (_status_error(401), cli.EXIT_AUTH),
        (_status_error(503), cli.EXIT_TRANSIENT),
        (_status_error(429), cli.EXIT_TRANSIENT),
        (_status_error(400), cli.EXIT_TOOL_ERROR),
        (httpx.ConnectTimeout("slow"), cli.EXIT_TRANSIENT),
        (ConnectionResetError(), cli.EXIT_TRANSIENT),
        (ValueError("bad"), cli.EXIT_TOOL_ERROR),
    ],
)
def test_classify_failure(leaf, code):
    assert cli._classify_failure([leaf], str(leaf))[0] == code


@pytest.mark.parametrize(
    ("error", "code"),
    [(cli.AuthRequired(), cli.EXIT_AUTH), (_status_error(502), cli.EXIT_TRANSIENT), (ValueError("x"), 1)],
)
def test_run_mcp_clean_exit_codes(error, code):
    async def fail():
        raise error

    with pytest.raises(SystemExit) as exc:
        cli.mcp2cli._run_mcp_clean(fail, "https://example.test/mcp")
    assert exc.value.code == code


def test_auth_required_message_names_login(capsys):
    async def needs_login():
        raise cli.AuthRequired()

    with pytest.raises(SystemExit) as exc:
        cli.mcp2cli._run_mcp_clean(needs_login, "https://example.test/mcp")
    assert exc.value.code == cli.EXIT_AUTH
    assert "auth:login" in capsys.readouterr().err


def test_invalid_log_search_exits_4_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_dispatch_mcp_call", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit) as exc:
        cli.dispatch_mcp_call("url", False, [], {}, cli.LOG_SEARCH_TOOL, {"query": "x", "from": "-2h", "to": "now"})
    assert exc.value.code == cli.EXIT_INVALID
    assert calls == []


def test_valid_log_search_dispatches_resolved_times(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_dispatch_mcp_call", lambda *args, **kwargs: calls.append(args))
    cli.dispatch_mcp_call("url", False, [], {}, cli.LOG_SEARCH_TOOL, {"query": "x", "from": "-5m", "to": "now"})
    arguments = calls[0][5]
    assert arguments["timezone"] == "UTC"
    assert "m" not in arguments["from"]


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_provider_binds_the_callback_port_only_when_login_starts(monkeypatch, tmp_path):
    import threading
    import urllib.request

    import anyio

    monkeypatch.setattr(cli.mcp2cli, "OAUTH_DIR", tmp_path)
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    opened = []
    monkeypatch.setattr(cli.mcp2cli.webbrowser, "open", opened.append)
    provider = cli.build_oauth_provider(
        "https://example.test/mcp",
        allow_browser=True,
        client_id="https://example.test/client.json",
        redirect_uri=redirect_uri,
        flow="authorization_code",
    )

    # Building the provider must leave the port free for concurrent commands.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))

    async def login():
        await provider.context.redirect_handler("https://auth.example.test/authorize")
        threading.Timer(0.2, urllib.request.urlopen, args=[f"{redirect_uri}?code=abc&state=xyz"]).start()
        return await provider.context.callback_handler()

    result = anyio.run(login)
    assert opened == ["https://auth.example.test/authorize"]
    assert (result.code, result.state) == ("abc", "xyz")
