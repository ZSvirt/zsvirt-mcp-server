import contextlib
import os
import socket
import subprocess
import sys
import time

import anyio

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(env_overrides: dict[str, str]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [sys.executable, "-m", "zstack_mcp.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_port(proc: subprocess.Popen[str], host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            raise RuntimeError(
                f"Server exited early (code {proc.returncode}).\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"Server did not start listening on {host}:{port} within {timeout:.1f}s")


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc.stdout or proc.stderr:
        with contextlib.suppress(Exception):
            proc.communicate(timeout=1)


async def _assert_tools_over_sse(base_url: str) -> None:
    async with sse_client(f"{base_url}/sse") as (read_stream, write_stream):
        session = ClientSession(read_stream, write_stream)
        await session.initialize()
        result = await session.list_tools()
        names = {tool.name for tool in result.tools}
        assert "search_api" in names


async def _assert_tools_over_streamable_http(base_url: str) -> None:
    async with streamable_http_client(f"{base_url}/mcp") as (read_stream, write_stream, _get_session_id):
        session = ClientSession(read_stream, write_stream)
        await session.initialize()
        result = await session.list_tools()
        names = {tool.name for tool in result.tools}
        assert "search_api" in names


def test_mcp_sse_transport() -> None:
    host = "127.0.0.1"
    port = _pick_free_port()
    proc = _start_server(
        {
            "MCP_TRANSPORT": "sse",
            "MCP_HOST": host,
            "MCP_PORT": str(port),
        }
    )
    try:
        _wait_for_port(proc, host, port)
        anyio.run(_assert_tools_over_sse, f"http://{host}:{port}")
    finally:
        _terminate_process(proc)


def test_mcp_streamable_http_transport() -> None:
    host = "127.0.0.1"
    port = _pick_free_port()
    proc = _start_server(
        {
            "MCP_TRANSPORT": "streamable-http",
            "MCP_HOST": host,
            "MCP_PORT": str(port),
            "MCP_STREAMABLE_PATH": "/mcp",
        }
    )
    try:
        _wait_for_port(proc, host, port)
        anyio.run(_assert_tools_over_streamable_http, f"http://{host}:{port}")
    finally:
        _terminate_process(proc)
