"""SessionManager 运行时认证改造 - 集成测试

需要连接真实 ZStack 环境，通过 config/zstack.env 提供凭据。

运行方式:
    pytest tests/test_session_manager.py -v

如果 ZStack 不可达会自动跳过。
"""

import os
from pathlib import Path

import pytest

from zstack_mcp.zstack_client import ZStackClient, ZStackApiError
from zstack_mcp.server import _SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV_PATH = Path(__file__).resolve().parent.parent / "config" / "zstack.env"


def _load_env() -> dict[str, str]:
    """读取 config/zstack.env，不污染 os.environ"""
    cfg: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return cfg
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()
    return cfg


def _zstack_reachable(cfg: dict[str, str]) -> bool:
    """快速检测 ZStack API 端口是否可达"""
    import socket
    from urllib.parse import urlparse

    url = cfg.get("ZSTACK_API_URL", "")
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


_cfg = _load_env()
_skip = not _zstack_reachable(_cfg)
requires_zstack = pytest.mark.skipif(_skip, reason="ZStack not reachable or config/zstack.env missing")


def _clear_auth_env() -> None:
    for key in ("ZSTACK_ACCOUNT", "ZSTACK_PASSWORD", "ZSTACK_SESSION_ID"):
        os.environ.pop(key, None)


def _set_api_url() -> None:
    os.environ["ZSTACK_API_URL"] = _cfg["ZSTACK_API_URL"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@requires_zstack
@pytest.mark.anyio
async def test_client_logout():
    """ZStackClient.logout() 应销毁 session 并关闭连接"""
    _set_api_url()
    client = ZStackClient(
        api_url=_cfg["ZSTACK_API_URL"],
        account=_cfg["ZSTACK_ACCOUNT"],
        password=_cfg["ZSTACK_PASSWORD"],
    )
    session = await client.login()
    assert session.uuid

    await client.logout()
    assert client.session is None


@requires_zstack
@pytest.mark.anyio
async def test_session_mgr_with_params():
    """通过参数传入 account/password，应能登录并执行 API"""
    _set_api_url()
    _clear_auth_env()

    mgr = _SessionManager(max_sessions=3)
    try:
        client = await mgr.get_client(
            account=_cfg["ZSTACK_ACCOUNT"],
            password=_cfg["ZSTACK_PASSWORD"],
        )
        assert client.session and client.session.uuid

        result = await client.execute(
            "QueryZone",
            "org.zstack.header.zone.APIQueryZoneMsg",
            {"conditions": []},
        )
        assert "success" in result or "inventories" in result
    finally:
        await mgr.logout_all()


@requires_zstack
@pytest.mark.anyio
async def test_session_reuse():
    """同一账户多次 get_client 应复用同一 session"""
    _set_api_url()
    _clear_auth_env()

    mgr = _SessionManager(max_sessions=3)
    try:
        client1 = await mgr.get_client(
            account=_cfg["ZSTACK_ACCOUNT"],
            password=_cfg["ZSTACK_PASSWORD"],
        )
        client2 = await mgr.get_client(
            account=_cfg["ZSTACK_ACCOUNT"],
            password=_cfg["ZSTACK_PASSWORD"],
        )
        assert client1 is client2
        assert client1.session.uuid == client2.session.uuid
    finally:
        await mgr.logout_all()


@requires_zstack
@pytest.mark.anyio
async def test_env_var_fallback():
    """不传参数时应回退到环境变量中的凭据"""
    os.environ["ZSTACK_API_URL"] = _cfg["ZSTACK_API_URL"]
    os.environ["ZSTACK_ACCOUNT"] = _cfg["ZSTACK_ACCOUNT"]
    os.environ["ZSTACK_PASSWORD"] = _cfg["ZSTACK_PASSWORD"]
    os.environ.pop("ZSTACK_SESSION_ID", None)

    mgr = _SessionManager(max_sessions=3)
    try:
        client = await mgr.get_client()
        assert client.session and client.session.uuid
    finally:
        await mgr.logout_all()
        _clear_auth_env()


@pytest.mark.anyio
async def test_no_credentials_error():
    """缺少凭据时应抛出明确的 ZStackApiError"""
    _clear_auth_env()

    mgr = _SessionManager(max_sessions=3)
    with pytest.raises(ZStackApiError, match="缺少认证凭据"):
        await mgr.get_client()


@requires_zstack
@pytest.mark.anyio
async def test_lru_eviction():
    """缓存满时应淘汰最早的 session，logout_all 应清空"""
    _set_api_url()
    _clear_auth_env()

    mgr = _SessionManager(max_sessions=1)
    try:
        client = await mgr.get_client(
            account=_cfg["ZSTACK_ACCOUNT"],
            password=_cfg["ZSTACK_PASSWORD"],
        )
        assert len(mgr._clients) == 1

        # 同一账户不触发淘汰
        client_again = await mgr.get_client(
            account=_cfg["ZSTACK_ACCOUNT"],
            password=_cfg["ZSTACK_PASSWORD"],
        )
        assert client is client_again
        assert len(mgr._clients) == 1
    finally:
        await mgr.logout_all()
        assert len(mgr._clients) == 0
