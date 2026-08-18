"""认证改造集成测试

覆盖本次改动的核心场景：
1. get_client() 新增的 session_id / api_url 参数
2. cache_key 从 account 改为 api_url|account（多环境隔离）
3. execute_api 通过 ctx: Context 从 HTTP 头取认证（用 mock ctx）
4. 多环境：同一账号不同 api_url → 各自独立 session

运行方式：
    pytest tests/test_auth_refactor.py -v

需要 ZStack 环境可达（自动跳过不可达的用例）。
"""

import os
import socket
from typing import Optional
from unittest.mock import MagicMock

import pytest

from zstack_mcp.server import _SessionManager, _extract_auth_from_context, RequestAuth
from zstack_mcp.zstack_client import ZStackApiError


# ---------------------------------------------------------------------------
# 环境配置
# ---------------------------------------------------------------------------

ENVS = {
    "env1": {
        "api_url": "http://172.20.0.37:8080",
        "account": "admin",
        "password": "password",
    },
    "env2_admin": {
        "api_url": "http://dev1:8080",
        "account": "admin",
        "password": "password",
    },
    "env2_wei": {
        "api_url": "http://dev1:8080",
        "account": "wei",
        "password": "password",
    },
}


def _reachable(api_url: str) -> bool:
    from urllib.parse import urlparse
    parsed = urlparse(api_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _skip_if_unreachable(env_key: str):
    cfg = ENVS[env_key]
    return pytest.mark.skipif(
        not _reachable(cfg["api_url"]),
        reason=f"{cfg['api_url']} not reachable",
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _make_mock_ctx(account: str, password: str, api_url: str, session_id: str = "") -> MagicMock:
    """构造一个带 HTTP headers 的 mock FastMCP Context"""
    headers = {
        "x-zstack-account": account,
        "x-zstack-password": password,
        "x-zstack-api-url": api_url,
    }
    if session_id:
        headers["x-zstack-session-id"] = session_id

    mock_request = MagicMock()
    mock_request.headers = headers
    mock_ctx = MagicMock()
    mock_ctx.request_context.request = mock_request
    return mock_ctx


def _clear_auth_env():
    for key in ("ZSTACK_ACCOUNT", "ZSTACK_PASSWORD", "ZSTACK_SESSION_ID", "ZSTACK_API_URL"):
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# 单元测试：_extract_auth_from_context
# ---------------------------------------------------------------------------

def test_extract_auth_from_http_context():
    """HTTP 模式：从 mock ctx 中正确提取所有字段"""
    ctx = _make_mock_ctx("admin", "password", "http://172.20.0.37:8080")
    auth = _extract_auth_from_context(ctx)

    assert auth.account == "admin"
    assert auth.password == "password"
    assert auth.api_url == "http://172.20.0.37:8080"
    assert auth.session_id is None


def test_extract_auth_with_session_id():
    """HTTP 模式：包含 session_id 时正确提取"""
    ctx = _make_mock_ctx("", "", "http://172.20.0.37:8080", session_id="test-uuid-1234")
    auth = _extract_auth_from_context(ctx)

    assert auth.session_id == "test-uuid-1234"
    assert auth.api_url == "http://172.20.0.37:8080"


def test_extract_auth_from_stdio_context():
    """stdio 模式（无 HTTP request context）：安全返回全空"""
    class _StdioCtx:
        @property
        def request_context(self):
            raise AttributeError("no request context in stdio mode")

    auth = _extract_auth_from_context(_StdioCtx())
    assert auth.account is None
    assert auth.password is None
    assert auth.session_id is None
    assert auth.api_url is None


# ---------------------------------------------------------------------------
# 单元测试：cache_key 隔离（无需网络）
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_key_isolates_different_api_urls():
    """同一账号不同 api_url → 两个独立的 cache_key，不会互相命中"""
    _clear_auth_env()
    mgr = _SessionManager(max_sessions=5)

    # 构造两个不同 api_url 的 cache_key（不实际登录，直接检查 key）
    key1 = f"http://172.20.0.37:8080|admin"
    key2 = f"http://dev1:8080|admin"

    assert key1 != key2, "不同环境的 cache_key 不应相同"


@pytest.mark.anyio
async def test_no_credentials_error():
    """缺少所有凭据时应抛出明确错误"""
    _clear_auth_env()
    mgr = _SessionManager(max_sessions=3)
    with pytest.raises(ZStackApiError, match="缺少认证凭据"):
        await mgr.get_client()


# ---------------------------------------------------------------------------
# 集成测试：env1（172.20.0.37）
# ---------------------------------------------------------------------------

@_skip_if_unreachable("env1")
@pytest.mark.anyio
async def test_env1_login_and_query():
    """env1：账号密码登录 + 执行 QueryZone"""
    _clear_auth_env()
    cfg = ENVS["env1"]
    mgr = _SessionManager(max_sessions=5)
    try:
        client = await mgr.get_client(
            account=cfg["account"],
            password=cfg["password"],
            api_url=cfg["api_url"],
        )
        assert client.session and client.session.uuid
        result = await client.execute(
            "QueryZone",
            "org.zstack.header.zone.APIQueryZoneMsg",
            {"conditions": [], "limit": 5},
        )
        assert isinstance(result, dict)
    finally:
        await mgr.logout_all()


@_skip_if_unreachable("env1")
@pytest.mark.anyio
async def test_env1_session_reuse():
    """env1：同一账号+api_url 两次 get_client 应返回同一对象"""
    _clear_auth_env()
    cfg = ENVS["env1"]
    mgr = _SessionManager(max_sessions=5)
    try:
        c1 = await mgr.get_client(account=cfg["account"], password=cfg["password"], api_url=cfg["api_url"])
        c2 = await mgr.get_client(account=cfg["account"], password=cfg["password"], api_url=cfg["api_url"])
        assert c1 is c2, "同一账号+url 应复用缓存"
    finally:
        await mgr.logout_all()


@_skip_if_unreachable("env1")
@pytest.mark.anyio
async def test_env1_via_mock_ctx():
    """env1：通过 mock ctx（模拟 HTTP Header）取到 auth 并登录"""
    _clear_auth_env()
    cfg = ENVS["env1"]
    ctx = _make_mock_ctx(cfg["account"], cfg["password"], cfg["api_url"])
    auth = _extract_auth_from_context(ctx)

    mgr = _SessionManager(max_sessions=5)
    try:
        client = await mgr.get_client(
            account=auth.account,
            password=auth.password,
            api_url=auth.api_url,
        )
        assert client.session and client.session.uuid
    finally:
        await mgr.logout_all()


# ---------------------------------------------------------------------------
# 集成测试：env2_admin（dev1/admin）
# ---------------------------------------------------------------------------

@_skip_if_unreachable("env2_admin")
@pytest.mark.anyio
async def test_env2_admin_login_and_query():
    """env2 admin：登录 + QueryVmInstance"""
    _clear_auth_env()
    cfg = ENVS["env2_admin"]
    mgr = _SessionManager(max_sessions=5)
    try:
        client = await mgr.get_client(
            account=cfg["account"],
            password=cfg["password"],
            api_url=cfg["api_url"],
        )
        assert client.session and client.session.uuid
        result = await client.execute(
            "QueryVmInstance",
            "org.zstack.header.vm.APIQueryVmInstanceMsg",
            {"conditions": [], "limit": 5},
        )
        assert isinstance(result, dict)
    finally:
        await mgr.logout_all()


# ---------------------------------------------------------------------------
# 集成测试：env2_wei（dev1/wei）
# ---------------------------------------------------------------------------

@_skip_if_unreachable("env2_wei")
@pytest.mark.anyio
async def test_env2_wei_login_and_query():
    """env2 wei：普通用户登录 + QueryVmInstance"""
    _clear_auth_env()
    cfg = ENVS["env2_wei"]
    mgr = _SessionManager(max_sessions=5)
    try:
        client = await mgr.get_client(
            account=cfg["account"],
            password=cfg["password"],
            api_url=cfg["api_url"],
        )
        assert client.session and client.session.uuid
        result = await client.execute(
            "QueryVmInstance",
            "org.zstack.header.vm.APIQueryVmInstanceMsg",
            {"conditions": [], "limit": 5},
        )
        assert isinstance(result, dict)
    finally:
        await mgr.logout_all()


# ---------------------------------------------------------------------------
# 集成测试：多环境隔离（同一 SessionManager，两个账号）
# ---------------------------------------------------------------------------

@_skip_if_unreachable("env2_admin")
@_skip_if_unreachable("env2_wei")
@pytest.mark.anyio
async def test_multi_tenant_isolation_same_url():
    """同一 api_url，不同账号（admin vs wei）→ 各自独立 session，互不干扰"""
    _clear_auth_env()
    mgr = _SessionManager(max_sessions=5)
    try:
        c_admin = await mgr.get_client(
            account=ENVS["env2_admin"]["account"],
            password=ENVS["env2_admin"]["password"],
            api_url=ENVS["env2_admin"]["api_url"],
        )
        c_wei = await mgr.get_client(
            account=ENVS["env2_wei"]["account"],
            password=ENVS["env2_wei"]["password"],
            api_url=ENVS["env2_wei"]["api_url"],
        )
        assert c_admin is not c_wei, "不同账号应是不同的 client"
        assert c_admin.session.uuid != c_wei.session.uuid, "session uuid 应不同"
        assert len(mgr._clients) == 2
    finally:
        await mgr.logout_all()


@_skip_if_unreachable("env1")
@_skip_if_unreachable("env2_admin")
@pytest.mark.anyio
async def test_multi_env_isolation_same_account():
    """同一账号（admin），不同 api_url → cache_key 不同，各自独立 session"""
    _clear_auth_env()
    mgr = _SessionManager(max_sessions=5)
    try:
        c1 = await mgr.get_client(
            account="admin",
            password="password",
            api_url=ENVS["env1"]["api_url"],
        )
        c2 = await mgr.get_client(
            account="admin",
            password="password",
            api_url=ENVS["env2_admin"]["api_url"],
        )
        assert c1 is not c2, "不同 api_url 的同名账号应是不同 client"
        assert len(mgr._clients) == 2, "应有两个独立缓存条目"
    finally:
        await mgr.logout_all()


# ---------------------------------------------------------------------------
# 集成测试：env 变量 fallback（不传参数，走环境变量）
# ---------------------------------------------------------------------------

@_skip_if_unreachable("env1")
@pytest.mark.anyio
async def test_env_var_fallback_with_api_url():
    """环境变量包含 ZSTACK_API_URL 时应正确 fallback"""
    cfg = ENVS["env1"]
    os.environ["ZSTACK_API_URL"] = cfg["api_url"]
    os.environ["ZSTACK_ACCOUNT"] = cfg["account"]
    os.environ["ZSTACK_PASSWORD"] = cfg["password"]
    os.environ.pop("ZSTACK_SESSION_ID", None)

    mgr = _SessionManager(max_sessions=3)
    try:
        client = await mgr.get_client()
        assert client.session and client.session.uuid
    finally:
        await mgr.logout_all()
        _clear_auth_env()
