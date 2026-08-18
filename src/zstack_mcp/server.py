"""
ZStack MCP Server - 主入口

提供以下 MCP Tools:
1. search_api - 搜索 ZStack API
2. describe_api - 获取 API 详细说明
3. execute_api - 执行 ZStack API
4. search_metric - 搜索监控指标
5. get_metric_data - 获取监控数据

安全说明:
- 默认只允许调用只读 API（Query/Get/List 等）
- 设置环境变量 ZSTACK_ALLOW_ALL_API=true 可允许调用所有 API
"""

import argparse
import asyncio
import atexit
import copy
import json
import logging
import os
import re
import sys
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from mcp.server.fastmcp import Context, FastMCP

from .api_search import ApiSearchIndex
from .metric_search import MetricSearchIndex
from .zstack_client import ZStackClient, ZStackApiError


# 初始化 MCP Server
mcp = FastMCP("zstack_mcp_server")

# 只读 API 前缀列表（安全的查询操作）
READONLY_API_PREFIXES = (
    'Query',      # 查询类
    'Get',        # 获取类
    'List',       # 列表类
    'Describe',   # 描述类
    'Check',      # 检查类
    'Count',      # 计数类
    'Search',     # 搜索类
    'Calculate',  # 计算类
    'Preview',    # 预览类
    'Validate',   # 验证类
    'Test',       # 测试类（如 TestConnection）
)


def is_readonly_api(api_name: str) -> bool:
    """检查是否为只读 API"""
    return api_name.startswith(READONLY_API_PREFIXES)


def is_write_api_allowed() -> bool:
    """检查是否允许调用写操作 API"""
    env_value = os.environ.get('ZSTACK_ALLOW_ALL_API', '').lower()
    return env_value in ('true', '1', 'yes', 'on')

# 全局索引和客户端
_api_index: Optional[ApiSearchIndex] = None
_metric_index: Optional[MetricSearchIndex] = None

logger = logging.getLogger(__name__)


@dataclass
class RequestAuth:
    """从 HTTP 请求头中提取的认证信息"""
    account: Optional[str] = None
    password: Optional[str] = None
    session_id: Optional[str] = None
    api_url: Optional[str] = None


def _extract_auth_from_context(ctx: Context) -> RequestAuth:
    """从 FastMCP Context 中提取 HTTP 头认证信息。

    在 streamable-http / SSE 模式下，ctx.request_context.request 是 Starlette Request，
    可以读取 HTTP headers。stdio 模式下没有 HTTP request，安全返回全空。
    """
    auth = RequestAuth()
    try:
        request = ctx.request_context.request
        headers = request.headers
        auth.account = headers.get("x-zstack-account") or None
        auth.password = headers.get("x-zstack-password") or None
        auth.session_id = headers.get("x-zstack-session-id") or None
        auth.api_url = headers.get("x-zstack-api-url") or None
    except Exception:
        # stdio 模式或其他无 HTTP request 的场景，安全忽略
        pass
    return auth


class _SessionManager:
    """管理 ZStack session 生命周期，按账户缓存，避免重复创建"""

    def __init__(self, max_sessions: int = 5):
        self._clients: OrderedDict[str, ZStackClient] = OrderedDict()
        self._max_sessions = max_sessions

    async def get_client(
        self,
        account: Optional[str] = None,
        password: Optional[str] = None,
        session_id: Optional[str] = None,
        api_url: Optional[str] = None,
    ) -> ZStackClient:
        """获取 client，优先用缓存的 session

        凭据优先级：参数（HTTP 头）> 环境变量。
        如果既没有参数也没有环境变量且没有 session_id，则抛出异常。
        """
        # 确定凭据来源
        env_session_id = os.environ.get("ZSTACK_SESSION_ID", "")
        env_account = os.environ.get("ZSTACK_ACCOUNT", "")
        env_password = os.environ.get("ZSTACK_PASSWORD", "")
        env_api_url = os.environ.get("ZSTACK_API_URL", "")

        effective_session_id = session_id or env_session_id
        effective_account = account or env_account
        effective_password = password or env_password
        effective_api_url = api_url or env_api_url or None

        # 如果有 session_id（参数传入或环境变量）且没有传账号密码 → 直接用 session_id
        if effective_session_id and not account and not password:
            cache_key = f"__session_id__{effective_api_url or ''}|{effective_session_id}"
            if cache_key in self._clients:
                self._clients.move_to_end(cache_key)
                return self._clients[cache_key]
            client = ZStackClient(session_id=effective_session_id, api_url=effective_api_url)
            self._clients[cache_key] = client
            return client

        # 必须有凭据
        if not effective_account or not effective_password:
            raise ZStackApiError(
                "缺少认证凭据。请通过 HTTP 头传入 X-ZStack-Account/X-ZStack-Password，"
                "或设置环境变量 ZSTACK_ACCOUNT + ZSTACK_PASSWORD，"
                "或设置 ZSTACK_SESSION_ID / X-ZStack-Session-Id 直接使用已有会话"
            )

        cache_key = f"{effective_api_url or ''}|{effective_account}"
        if cache_key in self._clients:
            self._clients.move_to_end(cache_key)
            return self._clients[cache_key]

        # 缓存未命中 → 创建新 client 并登录
        client = ZStackClient(
            account=effective_account,
            password=effective_password,
            api_url=effective_api_url,
        )
        await client.login()

        # 超过上限 → 淘汰最早的
        while len(self._clients) >= self._max_sessions:
            _, old_client = self._clients.popitem(last=False)
            await self._do_logout(old_client)

        self._clients[cache_key] = client
        return client

    async def logout_all(self) -> None:
        """服务关闭时清理所有 session"""
        for key in list(self._clients):
            client = self._clients.pop(key)
            await self._do_logout(client)

    @staticmethod
    async def _do_logout(client: ZStackClient) -> None:
        """调用 ZStack LogOut API 销毁 session"""
        try:
            await client.logout()
        except Exception:
            pass  # best-effort


_session_mgr = _SessionManager()


def get_data_dir() -> Path:
    """获取数据目录路径（包内 data 目录）"""
    return Path(__file__).parent / "data"


def get_api_index() -> ApiSearchIndex:
    """获取 API 搜索索引（懒加载）"""
    global _api_index
    if _api_index is None:
        _api_index = ApiSearchIndex()
        data_dir = get_data_dir()
        api_docs_path = data_dir / "api_docs.json"
        if api_docs_path.exists():
            _api_index.load_from_file(api_docs_path)
        else:
            raise FileNotFoundError(f"找不到 API 文档文件: {api_docs_path}")
    return _api_index


def get_metric_index() -> MetricSearchIndex:
    """获取监控指标搜索索引（懒加载）"""
    global _metric_index
    if _metric_index is None:
        _metric_index = MetricSearchIndex()
        data_dir = get_data_dir()
        metric_path = data_dir / "zs_all_metric_metadata.json"
        if metric_path.exists():
            _metric_index.load_from_file(metric_path)
        else:
            raise FileNotFoundError(f"找不到监控指标文件: {metric_path}")
    return _metric_index


def _extract_inventories(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("inventories", "inventory", "records", "results", "longJobs", "jobs"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [value]
    return []


def _get_query_default_limit() -> int:
    """获取 Query API 默认 limit，环境变量 ZSTACK_QUERY_DEFAULT_LIMIT，默认 50，设 0 禁用"""
    raw = os.environ.get("ZSTACK_QUERY_DEFAULT_LIMIT", "50")
    try:
        return max(int(raw), 0)
    except (ValueError, TypeError):
        return 50


def _get_response_size_limit() -> int:
    """获取响应大小上限（字节），环境变量 ZSTACK_RESPONSE_SIZE_LIMIT，默认 65536，设 0 禁用"""
    raw = os.environ.get("ZSTACK_RESPONSE_SIZE_LIMIT", "65536")
    try:
        return max(int(raw), 0)
    except (ValueError, TypeError):
        return 65536


def _apply_query_defaults(parameters: dict) -> Optional[dict]:
    """为 Query API 注入默认 limit（如果用户未显式指定）。

    返回一个提示 dict（_defaultLimit）或 None。
    会直接修改 parameters。
    """
    default_limit = _get_query_default_limit()
    if default_limit <= 0:
        return None

    # 用户已显式指定 limit，不覆盖
    if "limit" in parameters:
        return None

    parameters["limit"] = default_limit
    # 同时注入 replyWithCount 以便返回 totalCount
    if "replyWithCount" not in parameters:
        parameters["replyWithCount"] = True

    return {
        "applied": default_limit,
        "hint": f"Default limit={default_limit} applied. Use 'limit' and 'start' to paginate, or 'fields' to reduce response size.",
    }


def _truncate_response_if_needed(
    response_payload: dict[str, Any],
    result: Any,
    size_limit: int,
) -> None:
    """如果序列化后的响应超过 size_limit，渐进裁剪 inventories 列表。

    直接修改 response_payload 和 result（如果是 dict）。
    """
    if size_limit <= 0:
        return

    serialized = json.dumps(response_payload, ensure_ascii=False)
    if len(serialized.encode("utf-8")) <= size_limit:
        return

    # 找到 inventories 列表的 key
    if not isinstance(result, dict):
        return

    inv_key: Optional[str] = None
    inventories: Optional[list] = None
    for key in ("inventories", "inventory", "records", "results", "longJobs", "jobs"):
        value = result.get(key)
        if isinstance(value, list) and len(value) > 1:
            inv_key = key
            inventories = value
            break

    if inv_key is None or inventories is None:
        return

    original_count = len(inventories)

    # 预估 _truncation 元数据的大小，在二分查找时预留空间
    truncation_template = {
        "reason": "response_size_limit",
        "originalCount": original_count,
        "returnedCount": original_count,
        "sizeLimit": size_limit,
        "hint": f"Response exceeded {size_limit} bytes. Showing {original_count}/{original_count} items. Use 'limit', 'start' to paginate, or 'fields' to select specific columns.",
    }
    truncation_overhead = len(json.dumps({"_truncation": truncation_template}, ensure_ascii=False).encode("utf-8")) + 2  # +2 for comma and spacing

    effective_limit = size_limit - truncation_overhead

    # 二分查找：找到最大的 count 使得序列化大小 <= effective_limit
    lo, hi = 0, original_count
    while lo < hi:
        mid = (lo + hi + 1) // 2
        result[inv_key] = inventories[:mid]
        response_payload["result"] = result
        response_payload["resultCount"] = mid
        trial = json.dumps(response_payload, ensure_ascii=False)
        if len(trial.encode("utf-8")) <= effective_limit:
            lo = mid
        else:
            hi = mid - 1

    kept = lo
    result[inv_key] = inventories[:kept]
    response_payload["result"] = result
    response_payload["resultCount"] = kept
    response_payload["_truncation"] = {
        "reason": "response_size_limit",
        "originalCount": original_count,
        "returnedCount": kept,
        "sizeLimit": size_limit,
        "hint": f"Response exceeded {size_limit} bytes. Showing {kept}/{original_count} items. Use 'limit', 'start' to paginate, or 'fields' to select specific columns.",
    }


def _normalize_keywords(keywords: Any) -> tuple[list[str], bool]:
    if keywords is None:
        return [], False
    if isinstance(keywords, str):
        parts = [item for item in re.split(r"[,\s]+", keywords.strip()) if item]
        return parts, True
    if isinstance(keywords, (list, tuple, set)):
        results: list[str] = []
        changed = False
        for item in keywords:
            if item is None:
                changed = True
                continue
            if isinstance(item, str):
                parts = [part for part in re.split(r"[,\s]+", item.strip()) if part]
                results.extend(parts)
                if len(parts) != 1 or parts[0] != item:
                    changed = True
                continue
            text = str(item).strip()
            if text:
                results.append(text)
                changed = True
        return results, changed
    text = str(keywords).strip()
    if not text:
        return [], True
    return [text], True


def _regex_to_like(value: str) -> str:
    if not value:
        return value
    converted = value
    if converted.startswith("^"):
        converted = converted[1:]
    if converted.endswith("$"):
        converted = converted[:-1]
    converted = converted.replace(".*", "%")
    return converted


def _normalize_condition_item(condition: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    updated = dict(condition)
    warnings: list[str] = []
    changed = False

    op = updated.get("op")
    if isinstance(op, str):
        op_clean = op.strip()
        if op_clean != op:
            updated["op"] = op_clean
            changed = True

    op_value = updated.get("op")
    if isinstance(op_value, str):
        op_value = op_value.strip().lower()
    value = updated.get("value")
    if op_value in ("in", "not in") and isinstance(value, (list, tuple, set)):
        updated["value"] = ",".join(str(item) for item in value)
        changed = True
        warnings.append("op 'in' 的 value 已从数组转换为逗号字符串")

    return updated, changed, warnings


def _normalize_query_parameters(parameters: Any) -> tuple[dict[str, Any], list[str], bool]:
    if parameters is None:
        return {}, [], False
    if not isinstance(parameters, dict):
        return {}, ["parameters 必须是对象(dict)"], True
    normalized = dict(parameters)
    warnings: list[str] = []
    changed = False

    fields = normalized.get("fields")
    if isinstance(fields, str):
        fields_list = [item.strip() for item in fields.split(",") if item.strip()]
        normalized["fields"] = fields_list
        warnings.append("fields 已从逗号字符串转换为数组")
        changed = True
    elif isinstance(fields, (list, tuple, set)):
        fields_list: list[str] = []
        for item in fields:
            if item is None:
                changed = True
                continue
            if isinstance(item, str):
                parts = [part.strip() for part in item.split(",") if part.strip()]
                fields_list.extend(parts)
                if len(parts) != 1 or parts[0] != item:
                    changed = True
                continue
            text = str(item).strip()
            if text:
                fields_list.append(text)
                changed = True
        if fields_list != list(fields):
            normalized["fields"] = fields_list
    elif fields is None:
        pass
    else:
        warnings.append("fields 应为字符串数组，例如 [\"uuid\",\"name\"]")

    conditions = normalized.get("conditions")
    if isinstance(conditions, dict):
        normalized["conditions"] = [conditions]
        warnings.append("conditions 已从单个对象转换为数组")
        changed = True
    elif isinstance(conditions, (list, tuple)):
        new_conditions: list[Any] = []
        for item in conditions:
            if isinstance(item, dict):
                updated, item_changed, item_warnings = _normalize_condition_item(item)
                new_conditions.append(updated)
                if item_changed:
                    changed = True
                warnings.extend(item_warnings)
            else:
                new_conditions.append(item)
        if new_conditions != list(conditions):
            normalized["conditions"] = new_conditions
            changed = True

    return normalized, warnings, changed


def _collect_condition_ops(parameters: dict[str, Any]) -> set[str]:
    ops: set[str] = set()
    conditions = parameters.get("conditions")
    if isinstance(conditions, dict):
        conditions = [conditions]
    if isinstance(conditions, list):
        for item in conditions:
            if isinstance(item, dict):
                op = item.get("op")
                if isinstance(op, str):
                    ops.add(op.strip().lower())
    return ops


def _replace_condition_ops(
    parameters: dict[str, Any],
    from_ops: set[str],
    to_op: str,
    value_converter: Optional[Callable[[str], str]] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(parameters, dict):
        return None
    conditions = parameters.get("conditions")
    if isinstance(conditions, dict):
        conditions = [conditions]
    if not isinstance(conditions, list):
        return None

    suggested = None
    for idx, item in enumerate(conditions):
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        if not isinstance(op, str):
            continue
        op_lower = op.strip().lower()
        if op_lower not in from_ops:
            continue
        if suggested is None:
            suggested = copy.deepcopy(parameters)
        target = suggested["conditions"][idx]
        target["op"] = to_op
        if value_converter:
            value = target.get("value")
            if isinstance(value, str):
                converted = value_converter(value)
                if converted != value:
                    target["value"] = converted
    return suggested


def _build_api_error_hint(
    api_name: str,
    api_info: Any,
    parameters: dict[str, Any],
    error_text: str,
) -> tuple[Optional[str], list[str], Optional[dict[str, Any]]]:
    hints: list[str] = []
    suggested_parameters: Optional[dict[str, Any]] = None

    lowered = error_text.lower() if error_text else ""
    if api_name.startswith("Query"):
        if "fields" in lowered:
            hints.append("fields 必须是数组，例如 [\"uuid\",\"name\"]")
        if "unknown queryop type[?=" in lowered:
            hints.append("当前环境不支持 ?=，请改用 like（或 ~=）")
            suggested_parameters = _replace_condition_ops(parameters, {"?="}, "like")
        elif "unknown queryop type[like]" in lowered:
            hints.append("当前环境不支持 like，请改用 ?=")
            suggested_parameters = _replace_condition_ops(parameters, {"like"}, "?=")
        elif "unknown queryop type[~=" in lowered or "unknown queryop type[regex" in lowered:
            hints.append("当前环境不支持 ~=，请改用 like（或 ?=）")
            suggested_parameters = _replace_condition_ops(parameters, {"~=", "regex"}, "like", _regex_to_like)
        if api_info and getattr(api_info, "primitive_fields", None):
            if "field" in lowered or "unknown" in lowered or "not found" in lowered:
                fields = [field for field in api_info.primitive_fields if field]
                if fields:
                    sample = ", ".join(fields[:10])
                    hints.append(f"可用 fields 示例: {sample}")

    hint_text = "；".join(hints) if hints else None
    return hint_text, hints, suggested_parameters


def _summarize_metric_values(values: list[float]) -> Optional[dict[str, float]]:
    if not values:
        return None
    total = 0.0
    count = 0
    mean = 0.0
    m2 = 0.0
    min_value = None
    max_value = None
    for value in values:
        count += 1
        total += value
        if min_value is None or value < min_value:
            min_value = value
        if max_value is None or value > max_value:
            max_value = value
        delta = value - mean
        mean += delta / count
        m2 += delta * (value - mean)
    variance = m2 / count if count else 0.0
    return {
        "avg": mean,
        "max": max_value if max_value is not None else 0.0,
        "min": min_value if min_value is not None else 0.0,
        "sum": total,
        "count": count,
        "variance": variance,
        "stddev": variance ** 0.5,
    }


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def _collect_metric_values(result: Any) -> list[float]:
    values: list[float] = []

    def handle_point(point: Any) -> None:
        if isinstance(point, dict):
            for key in ("value", "avg", "max", "min"):
                if key in point and _is_number(point[key]):
                    values.append(float(point[key]))
                    return
        elif isinstance(point, (list, tuple)) and len(point) >= 2 and _is_number(point[1]):
            values.append(float(point[1]))
        elif _is_number(point):
            values.append(float(point))

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return values

    if isinstance(result, list):
        for item in result:
            handle_point(item)
        return values

    if isinstance(result, dict):
        data_list = result.get("data")
        if isinstance(data_list, list):
            for series in data_list:
                if isinstance(series, dict):
                    points = (
                        series.get("dataPoints")
                        or series.get("points")
                        or series.get("values")
                        or series.get("data")
                    )
                    if isinstance(points, list):
                        for point in points:
                            handle_point(point)
                else:
                    handle_point(series)
        for key in ("dataPoints", "points", "values"):
            points = result.get(key)
            if isinstance(points, list):
                for point in points:
                    handle_point(point)

    return values


def _extract_metric_error(result: Any) -> Optional[str]:
    if isinstance(result, dict) and result.get("success") is False:
        error = result.get("error")
        if isinstance(error, dict):
            description = error.get("description") or ""
            details = error.get("details") or ""
            if description and details:
                return f"{description} ({details})"
            if description:
                return description
            if details:
                return details
            return json.dumps(error, ensure_ascii=False)
        if error:
            return str(error)
        return "metric query failed"
    return None


def _metric_error_hint(error_text: str) -> Optional[str]:
    if not error_text:
        return None
    if "Expected STRING but was BEGIN_OBJECT" in error_text:
        return "labels 建议传字符串列表，例如 [\"VMUuid=xxx\"] 或 {\"VMUuid\":\"xxx\"}"
    if "NumberFormatException" in error_text:
        return "startTime/endTime 建议传秒级时间戳或 ISO 时间"
    if "Prometheus" in error_text and "HTTP" in error_text:
        return "Prometheus 指标查询失败，请检查 Prometheus 可达性"
    return None


def _session_error_hint(error_text: str) -> Optional[str]:
    if not error_text:
        return None
    lowered = error_text.lower()
    if "session" in lowered and ("invalid" in lowered or "expired" in lowered):
        return (
            "session 已过期或无效：若使用 ZSTACK_SESSION_ID 请更新；"
            "若使用账号密码请确认未设置 ZSTACK_SESSION_ID"
        )
    return None


def _compare_threshold(value: float, op: str, target: float) -> bool:
    if op == ">":
        return value > target
    if op == ">=":
        return value >= target
    if op == "<":
        return value < target
    if op == "<=":
        return value <= target
    if op == "==":
        return value == target
    if op == "!=":
        return value != target
    return value >= target


def _group_metric_values(
    data_points: list[Any],
    label_key: str,
) -> tuple[dict[str, list[float]], set[str]]:
    available_label_keys: set[str] = set()
    grouped_values: dict[str, list[float]] = defaultdict(list)
    for point in data_points:
        if not isinstance(point, dict):
            continue
        labels = point.get("labels") or {}
        if isinstance(labels, dict):
            available_label_keys.update(labels.keys())
            label_value = labels.get(label_key)
        else:
            label_value = None
        value = point.get("value")
        if label_value is None or value is None:
            continue
        try:
            grouped_values[str(label_value)].append(float(value))
        except Exception:
            continue
    return grouped_values, available_label_keys


def _count_metric_points(result: Any) -> dict[str, int]:
    counts = {"points": 0, "series": 0}
    if result is None:
        return counts
    if isinstance(result, list):
        counts["points"] = len(result)
        counts["series"] = 1 if counts["points"] else 0
        return counts
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            if data and all(
                isinstance(item, dict) and "value" in item and "time" in item
                for item in data
            ):
                counts["points"] = len(data)
                counts["series"] = 1
                return counts
            for item in data:
                if isinstance(item, dict):
                    for key in ("dataPoints", "points", "values", "data"):
                        points = item.get(key)
                        if isinstance(points, list):
                            counts["points"] += len(points)
                            counts["series"] += 1
                            break
                    else:
                        counts["points"] += 1
                        counts["series"] += 1
                elif isinstance(item, list):
                    counts["points"] += len(item)
                    counts["series"] += 1
                else:
                    counts["points"] += 1
            if counts["series"] == 0 and counts["points"] > 0:
                counts["series"] = 1
            return counts
        for key in ("dataPoints", "points", "values"):
            points = result.get(key)
            if isinstance(points, list):
                counts["points"] = len(points)
                counts["series"] = 1 if counts["points"] else 0
                return counts
    return counts


# ============== MCP Tools ==============


@mcp.tool()
async def search_api(
    keywords: list[str],
    category: Optional[str] = None,
    limit: int = 15,
) -> str:
    """
    根据关键词搜索 ZStack API
    
    Args:
        keywords: 搜索关键词列表，如 ["Query", "Vm"] 或 ["Create", "Volume"]
                  支持驼峰拆分匹配，如搜索 "vm" 可以匹配 "QueryVmInstance"
        category: 可选，按分类过滤，如 "vm", "volume", "network"
        limit: 最多返回数量，默认 15
        
    Returns:
        匹配的 API 列表，包含名称、描述、分类、调用类型
    """
    try:
        normalized_keywords, keywords_changed = _normalize_keywords(keywords)
        keywords = normalized_keywords
        if not keywords:
            return json.dumps({
                "success": False,
                "error": "keywords 不能为空",
                "hint": "示例: keywords=[\"Query\", \"Vm\"]",
            }, ensure_ascii=False, indent=2)
        index = get_api_index()
        results = index.search(keywords, category=category, limit=limit)
        
        if not results:
            payload = {
                "success": True,
                "message": f"未找到匹配关键词 {keywords} 的 API",
                "apis": [],
                "hint": f"可用分类: {', '.join(index.list_categories()[:10])}..."
            }
            if keywords_changed:
                payload["normalizedKeywords"] = keywords
            return json.dumps(payload, ensure_ascii=False, indent=2)
        
        payload = {
            "success": True,
            "count": len(results),
            "apis": results,
        }
        if keywords_changed:
            payload["normalizedKeywords"] = keywords
        return json.dumps(payload, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def describe_api(api_name: str) -> str:
    """
    获取指定 ZStack API 的详细参数说明
    
    Args:
        api_name: API 名称，如 "QueryVmInstance"
        
    Returns:
        API 的精简信息。对于 Query API，仅返回核心参数和 queryableFields。
    """
    try:
        index = get_api_index()
        detail = index.get_api_detail(api_name)
        
        if not detail:
            # 尝试模糊搜索给出建议
            suggestions = index.search([api_name], limit=5)
            return json.dumps({
                "success": False,
                "error": f"未找到 API: {api_name}",
                "suggestions": [s['name'] for s in suggestions] if suggestions else [],
            }, ensure_ascii=False, indent=2)
        
        return json.dumps({
            "success": True,
            "api": detail,
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def execute_api(
    api_name: str,
    parameters: dict,
    ctx: Context = None,
) -> str:
    """
    执行 ZStack API

    注意: 默认只允许调用只读 API（Query/Get/List 等）。
    如需调用写操作 API，请设置环境变量 ZSTACK_ALLOW_ALL_API=true

    Args:
        api_name: API 名称，如 "QueryVmInstance"
        parameters: API 参数字典
                   对于 Query API，conditions 格式为:
                   [{"name": "字段名", "op": "操作符", "value": "值"}, ...]
                   分页: limit（默认 50）、start（偏移量）
                   字段选择: fields（减少返回数据量）

    Returns:
        API 执行结果 (JSON 格式)

    Example:
        execute_api(
            api_name="QueryVmInstance",
            parameters={
                "conditions": [
                    {"name": "uuid", "op": "like", "value": "ae6e57a0%"}
                ]
            }
        )
    """
    api_info = None
    try:
        # 权限检查：默认只允许只读 API
        if not is_readonly_api(api_name) and not is_write_api_allowed():
            return json.dumps({
                "success": False,
                "error": f"安全限制: API '{api_name}' 是写操作，默认被禁止",
                "hint": "如需执行写操作，请设置环境变量 ZSTACK_ALLOW_ALL_API=true",
                "allowed_prefixes": list(READONLY_API_PREFIXES),
            }, ensure_ascii=False, indent=2)
        
        # 获取 API 信息
        index = get_api_index()
        api_info = index.get_api(api_name)
        
        if not api_info:
            return json.dumps({
                "success": False,
                "error": f"未找到 API: {api_name}",
            }, ensure_ascii=False, indent=2)

        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            return json.dumps({
                "success": False,
                "error": "parameters 必须是对象(dict)",
                "hint": "示例: {\"conditions\": [{\"name\": \"uuid\", \"op\": \"=\", \"value\": \"xxx\"}]}",
            }, ensure_ascii=False, indent=2)

        normalization_warnings: list[str] = []
        normalized_changed = False
        default_limit_hint: Optional[dict] = None
        if api_name.startswith("Query"):
            parameters, normalization_warnings, normalized_changed = _normalize_query_parameters(parameters)
            default_limit_hint = _apply_query_defaults(parameters)

        # 获取客户端并执行
        auth = _extract_auth_from_context(ctx) if ctx else RequestAuth()
        client = await _session_mgr.get_client(
            account=auth.account,
            password=auth.password,
            session_id=auth.session_id,
            api_url=auth.api_url,
        )
        is_async = api_info.call_type == 'async'

        result = await client.execute(
            api_name=api_name,
            full_api_name=api_info.full_name,
            parameters=parameters,
            is_async=is_async,
        )

        response_payload: dict[str, Any] = {
            "success": True,
            "result": result,
        }
        if api_name.startswith("Query"):
            inventories = _extract_inventories(result)
            response_payload["resultCount"] = len(inventories)
            if isinstance(result, dict) and "total" in result:
                response_payload["totalCount"] = result["total"]
        if normalization_warnings:
            response_payload["warnings"] = normalization_warnings
        if normalized_changed:
            response_payload["normalizedParameters"] = parameters
        if default_limit_hint:
            response_payload["_defaultLimit"] = default_limit_hint

        # 响应大小兜底裁剪
        if api_name.startswith("Query"):
            _truncate_response_if_needed(response_payload, result, _get_response_size_limit())

        return json.dumps(response_payload, ensure_ascii=False, indent=2)
        
    except ZStackApiError as e:
        hint_text, hints, suggested_parameters = _build_api_error_hint(
            api_name,
            api_info,
            parameters if isinstance(parameters, dict) else {},
            str(e),
        )
        session_hint = _session_error_hint(str(e))
        if session_hint:
            hints.append(session_hint)
            hint_text = "；".join(hints)
        payload = {
            "success": False,
            "error": str(e),
            "code": e.code,
            "details": e.details,
        }
        if hint_text:
            payload["hint"] = hint_text
        if hints:
            payload["hints"] = hints
        if suggested_parameters:
            payload["suggestedParameters"] = suggested_parameters
        return json.dumps(payload, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_metric(
    keywords: list[str],
    namespace: Optional[str] = None,
    limit: int = 20,
    match_mode: str = "or",
    prefer_namespaces: Optional[list[str]] = None,
) -> str:
    """
    搜索可用的 ZStack 监控指标
    
    Args:
        keywords: 搜索关键词，如 ["CPU", "Usage"] 或 ["Memory"]
                  支持驼峰拆分匹配
        namespace: 可选，按命名空间过滤（支持模糊匹配），如 "ZStack/VM", "vm", "host"
        limit: 最多返回数量，默认 20
        match_mode: 关键词匹配模式，"and" 或 "or"，默认 "or"
        prefer_namespaces: 优先排序的命名空间列表（默认 ["ZStack/VM","ZStack/Host"]）
        
    Returns:
        匹配的监控指标列表，包含名称、描述、命名空间、可用标签
    """
    try:
        normalized_keywords, keywords_changed = _normalize_keywords(keywords)
        keywords = normalized_keywords
        if not keywords:
            return json.dumps({
                "success": False,
                "error": "keywords 不能为空",
                "hint": "示例: keywords=[\"CPU\", \"Usage\"]",
            }, ensure_ascii=False, indent=2)
        if namespace is not None:
            namespace = str(namespace).strip() or None
        match_mode = (match_mode or "or").lower().strip()
        if match_mode not in ("and", "or"):
            match_mode = "or"
        if prefer_namespaces is None:
            prefer_namespaces = ["ZStack/VM", "ZStack/Host"]
        else:
            normalized_pref, _ = _normalize_keywords(prefer_namespaces)
            prefer_namespaces = normalized_pref or []
        index = get_metric_index()
        results = index.search(
            keywords,
            namespace=namespace,
            limit=limit,
            match_mode=match_mode,
            prefer_namespaces=prefer_namespaces,
        )
        
        if not results:
            response_payload: dict[str, Any] = {
                "success": True,
                "message": f"未找到匹配关键词 {keywords} 的监控指标",
                "metrics": [],
                "hint": "namespace 支持模糊匹配，如 vm/host/backup；可用命名空间: "
                        f"{', '.join(index.list_namespaces())}",
            }
            fallback_results: list[dict[str, Any]] = []
            if namespace:
                fallback_results = index.search(
                    keywords,
                    namespace=None,
                    limit=limit,
                    match_mode=match_mode,
                    prefer_namespaces=prefer_namespaces,
                )
                if fallback_results:
                    namespaces = sorted({
                        item.get("namespace") for item in fallback_results if item.get("namespace")
                    })
                    response_payload["suggestedNamespaces"] = namespaces[:8]
                    response_payload["suggestedMetrics"] = fallback_results
                    response_payload["hint"] = (
                        f"当前 namespace '{namespace}' 无结果，已提供跨命名空间建议"
                    )
            if not fallback_results:
                loose_results: list[dict[str, Any]] = []
                for kw in keywords:
                    loose_results.extend(
                        index.search(
                            [kw],
                            namespace=namespace,
                            limit=limit,
                            match_mode="or",
                            prefer_namespaces=prefer_namespaces,
                        )
                    )
                if loose_results:
                    deduped: list[dict[str, Any]] = []
                    seen: set[str] = set()
                    for item in loose_results:
                        name = item.get("name")
                        if not name or name in seen:
                            continue
                        seen.add(name)
                        deduped.append(item)
                    response_payload["suggestedMetrics"] = deduped[:limit]
            if keywords_changed:
                response_payload["normalizedKeywords"] = keywords
            return json.dumps(response_payload, ensure_ascii=False, indent=2)
        
        response_payload = {
            "success": True,
            "count": len(results),
            "metrics": results,
        }
        if keywords_changed:
            response_payload["normalizedKeywords"] = keywords
        return json.dumps(response_payload, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_metric_data(
    namespace: str,
    metric_name: str,
    start_time: Optional[Any] = None,
    end_time: Optional[Any] = None,
    period: Optional[int] = 60,
    labels: Optional[Any] = None,
    summary_only: bool = False,
    ctx: Context = None,
) -> str:
    """
    获取 ZStack 监控数据

    Args:
        namespace: 命名空间，如 "ZStack/VM", "ZStack/Host"
        metric_name: 指标名称，如 "CPUUsedUtilization"
        start_time: 开始时间（ISO 或秒级时间戳）
        end_time: 结束时间（ISO 或秒级时间戳）
        period: 采样周期(秒)，默认 60
        labels: 标签过滤，如 ["VMUuid=xxx"] 或 {"VMUuid":"xxx"}
        summary_only: 仅返回统计信息（点数/最大/最小/平均/方差/标准差）

    注意:
        返回数据量与时间跨度和 period 成正比。可用估算公式:
        点数 ≈ ceil((end_time - start_time) / period) * series_count
        series_count 为不同 label 组合数量；若不传 labels，可能返回多组系列
        （例如指标包含 CPUNum/VMUuid 等 label 时每个组合都会产出一组序列）。
        为避免输出过大：缩短时间范围、增大 period 或增加 labels 过滤。

    Returns:
        监控数据点列表
    """
    try:
        auth = _extract_auth_from_context(ctx) if ctx else RequestAuth()
        client = await _session_mgr.get_client(
            account=auth.account,
            password=auth.password,
            session_id=auth.session_id,
            api_url=auth.api_url,
        )
        result = await client.query_metric_data(
            namespace=namespace,
            metric_name=metric_name,
            start_time=start_time,
            end_time=end_time,
            period=period,
            labels=labels,
        )
        
        metric_error = _extract_metric_error(result)
        if metric_error:
            return json.dumps({
                "success": False,
                "error": metric_error,
                "hint": _metric_error_hint(metric_error),
            }, ensure_ascii=False, indent=2)

        counts = _count_metric_points(result)
        if summary_only:
            values = _collect_metric_values(result)
            summary = _summarize_metric_values(values)
            response_payload: dict[str, Any] = {
                "success": True,
                "summary": summary,
                "dataPointCount": len(values),
                "seriesCount": counts["series"],
            }
            if not values:
                response_payload["message"] = "未返回监控数据点"
            return json.dumps(response_payload, ensure_ascii=False, indent=2)
        response_payload: dict[str, Any] = {
            "success": True,
            "result": result,
        }
        if counts["points"]:
            response_payload["dataPointCount"] = counts["points"]
            response_payload["seriesCount"] = counts["series"]
        return json.dumps(response_payload, ensure_ascii=False, indent=2)
        
    except ZStackApiError as e:
        payload = {
            "success": False,
            "error": str(e),
            "code": e.code,
            "details": e.details,
        }
        session_hint = _session_error_hint(str(e))
        if session_hint:
            payload["hint"] = session_hint
        return json.dumps(payload, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_metric_summary(
    namespace: str,
    metric_name: str,
    label_key: str,
    metric_names: Optional[list[str]] = None,
    start_time: Optional[Any] = None,
    end_time: Optional[Any] = None,
    period: Optional[int] = 60,
    aggregate: str = "max",
    combine: str = "sum",
    threshold_op: Optional[str] = None,
    threshold_value: Optional[float] = None,
    top_n: int = 10,
    resolve_resource: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """
    获取监控指标的聚合 TopN（按 label_key 分组）

    Args:
        namespace: 命名空间，如 "ZStack/VM", "ZStack/Host"
        metric_name: 指标名称，如 "CPUOccupiedByVm"
        label_key: 标签键，如 "VMUuid", "HostUuid"
        metric_names: 可选，多指标合并（如 in/out）
        start_time: 开始时间（ISO 或秒级时间戳）
        end_time: 结束时间（ISO 或秒级时间戳）
        period: 采样周期(秒)，默认 60
        aggregate: 单指标聚合方式，可选 "max"|"avg"|"sum"|"min"
        combine: 多指标合并方式，可选 "sum"|"avg"|"max"|"min"
        threshold_op: 阈值比较符，如 >,>=,<,<=,==,!=
        threshold_value: 阈值数值
        top_n: 返回条数，默认 10
        resolve_resource: 可选 "vm" 或 "host"，用于解析名称

    Returns:
        聚合后的 TopN 列表
    """
    try:
        auth = _extract_auth_from_context(ctx) if ctx else RequestAuth()
        client = await _session_mgr.get_client(
            account=auth.account,
            password=auth.password,
            session_id=auth.session_id,
            api_url=auth.api_url,
        )
        metrics = []
        if metric_names:
            metrics.extend([name for name in metric_names if name])
        if metric_name and metric_name not in metrics:
            metrics.insert(0, metric_name)
        metrics = [name for name in metrics if name]
        if not metrics:
            return json.dumps({
                "success": False,
                "error": "metric_name 或 metric_names 不能为空",
            }, ensure_ascii=False, indent=2)

        aggregate = aggregate.lower().strip()
        if aggregate not in ("max", "avg", "sum", "min"):
            aggregate = "max"
        combine = combine.lower().strip()
        if combine not in ("sum", "avg", "max", "min"):
            combine = "sum"

        metric_groups: dict[str, dict[str, list[float]]] = {}
        available_label_keys: set[str] = set()
        for name in metrics:
            result = await client.query_metric_data(
                namespace=namespace,
                metric_name=name,
                start_time=start_time,
                end_time=end_time,
                period=period,
                labels=None,
            )
            metric_error = _extract_metric_error(result)
            if metric_error:
                return json.dumps({
                    "success": False,
                    "error": metric_error,
                    "hint": _metric_error_hint(metric_error),
                }, ensure_ascii=False, indent=2)

            data_points = []
            if isinstance(result, dict):
                data_points = result.get("data") or []
            grouped_values, label_keys = _group_metric_values(data_points, label_key)
            available_label_keys.update(label_keys)
            metric_groups[name] = grouped_values

        label_values: set[str] = set()
        for grouped in metric_groups.values():
            label_values.update(grouped.keys())

        if not label_values:
            hint = None
            if available_label_keys:
                hint = f"可用 label_key: {', '.join(sorted(available_label_keys))}"
            return json.dumps({
                "success": True,
                "result": [],
                "hint": hint,
            }, ensure_ascii=False, indent=2)

        rows = []
        for label_value in label_values:
            metric_stats: dict[str, dict[str, float]] = {}
            metric_agg_values: dict[str, Optional[float]] = {}
            combine_values: list[float] = []

            for name in metrics:
                values = metric_groups.get(name, {}).get(label_value)
                if not values:
                    metric_agg_values[name] = None
                    continue
                stats = _summarize_metric_values(values)
                if not stats:
                    metric_agg_values[name] = None
                    continue
                metric_stats[name] = stats
                agg_value = stats.get(aggregate)
                metric_agg_values[name] = agg_value
                if agg_value is not None:
                    combine_values.append(agg_value)

            if not combine_values:
                continue

            if combine == "sum":
                combined_value = sum(combine_values)
            elif combine == "avg":
                combined_value = sum(combine_values) / len(combine_values)
            elif combine == "min":
                combined_value = min(combine_values)
            else:
                combined_value = max(combine_values)

            if threshold_op and threshold_value is not None:
                if not _compare_threshold(combined_value, threshold_op, float(threshold_value)):
                    continue

            row = {
                "labelValue": label_value,
                "name": label_value,
                "aggregateValue": combined_value,
            }
            if len(metrics) == 1 and metrics[0] in metric_stats:
                row["stats"] = metric_stats[metrics[0]]
                row["aggregateValue"] = metric_agg_values.get(metrics[0])
            else:
                row["metrics"] = {
                    name: {
                        "stats": metric_stats.get(name),
                        "aggregateValue": metric_agg_values.get(name),
                    }
                    for name in metrics
                }
            rows.append(row)

        rows.sort(key=lambda x: x.get("aggregateValue") or 0, reverse=True)
        limit = max(top_n, 1)
        rows = rows[:limit]

        if resolve_resource in ("vm", "host"):
            api_name = "QueryVmInstance" if resolve_resource == "vm" else "QueryHost"
            index = get_api_index()
            api_info = index.get_api(api_name)
            if api_info:
                uuids = [row["labelValue"] for row in rows]
                if uuids:
                    params = {
                        "conditions": [{"name": "uuid", "op": "in", "value": ",".join(uuids)}],
                        "fields": ["uuid", "name"],
                    }
                    resolved = await client.execute(
                        api_name=api_name,
                        full_api_name=api_info.full_name,
                        parameters=params,
                        is_async=(api_info.call_type == "async"),
                    )
                    inventories = _extract_inventories(resolved)
                    name_map = {
                        item.get("uuid"): item.get("name")
                        for item in inventories
                        if item.get("uuid")
                    }
                    for row in rows:
                        row["name"] = name_map.get(row["labelValue"], row["name"])

        return json.dumps({
            "success": True,
            "metrics": metrics,
            "aggregate": aggregate,
            "combine": combine,
            "threshold": {
                "op": threshold_op,
                "value": threshold_value,
            } if threshold_op and threshold_value is not None else None,
            "count": len(rows),
            "result": rows,
        }, ensure_ascii=False, indent=2)

    except ZStackApiError as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "code": e.code,
            "details": e.details,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


def _normalize_transport(value: Optional[str]) -> str:
    if not value:
        return "stdio"
    return value.strip().lower()


def _get_first_env(*keys: str) -> Optional[str]:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return None


def _apply_fastmcp_network_settings(
    host: Optional[str],
    port: Optional[int],
    streamable_path: Optional[str],
) -> None:
    if host:
        mcp.settings.host = host
        # When binding to a non-loopback address (e.g. 0.0.0.0 or a LAN IP),
        # disable DNS rebinding protection so that MCP clients (Dify, Claude Desktop,
        # etc.) connecting from any origin are not rejected with 403/421.
        if (
            mcp.settings.transport_security
            and mcp.settings.transport_security.enable_dns_rebinding_protection
            and host not in ("127.0.0.1", "localhost", "::1")
        ):
            mcp.settings.transport_security.enable_dns_rebinding_protection = False
    if port is not None:
        mcp.settings.port = port
    if streamable_path:
        if not streamable_path.startswith("/"):
            streamable_path = "/" + streamable_path
        mcp.settings.streamable_http_path = streamable_path


def _mask_sensitive_value(value: str, head: int = 2, tail: int = 2) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * (len(value) - head - tail)}{value[-tail:]}"


def _format_env_value(key: str, value: Optional[str]) -> str:
    if not value:
        return "(未设置)"
    if key in {"ZSTACK_PASSWORD", "ZSTACK_SESSION_ID"}:
        return _mask_sensitive_value(value, head=3, tail=3)
    return value


def _normalize_path_value(value: Optional[str], fallback: str) -> str:
    if not value:
        return fallback
    return value if value.startswith("/") else f"/{value}"


def _build_endpoint(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def _build_json_import_example(transport: str, endpoint: Optional[str]) -> dict[str, Any]:
    if transport in {"sse", "streamable-http"} and endpoint:
        return {
            "mcpServers": {
                "zstack": {
                    "transport": transport,
                    "url": endpoint,
                }
            }
        }
    return {
        "mcpServers": {
            "zstack": {
                "command": "python",
                "args": ["-m", "zstack_mcp.server"],
                "env": {
                    "ZSTACK_API_URL": "http://your-zstack-server:8080",
                    "ZSTACK_ACCOUNT": "admin",
                    "ZSTACK_PASSWORD": "your-password",
                    "ZSTACK_ALLOW_ALL_API": "false",
                },
            }
        }
    }


def _print_startup_summary(
    transport: str,
    sse_path: Optional[str],
    streamable_path: Optional[str],
) -> None:
    host = getattr(mcp.settings, "host", None) or "127.0.0.1"
    port = getattr(mcp.settings, "port", None) or 8000
    sse_path = _normalize_path_value(sse_path or getattr(mcp.settings, "mount_path", None), "/sse")
    streamable_path = _normalize_path_value(
        streamable_path or getattr(mcp.settings, "streamable_http_path", None), "/mcp"
    )

    endpoint: Optional[str] = None
    if transport == "sse":
        endpoint = _build_endpoint(host, port, sse_path)
    elif transport == "streamable-http":
        endpoint = _build_endpoint(host, port, streamable_path)

    print("\n=== ZStack MCP Server 启动信息 ===")
    print(f"传输模式: {transport}")
    if endpoint:
        print(f"Endpoint: {endpoint}")
    else:
        print("Endpoint: (stdio 模式，无网络地址)")

    env_keys = (
        "ZSTACK_API_URL",
        "ZSTACK_ACCOUNT",
        "ZSTACK_PASSWORD",
        "ZSTACK_SESSION_ID",
        "ZSTACK_ALLOW_ALL_API",
        "ZSTACK_QUERY_DEFAULT_LIMIT",
        "ZSTACK_RESPONSE_SIZE_LIMIT",
    )
    print("\nZStack 环境变量:")
    for key in env_keys:
        value = _format_env_value(key, os.environ.get(key))
        print(f"- {key}: {value}")

    client = ZStackClient()
    print("\nZStack API 端点（当前配置推导）:")
    print(f"- api_url: {client.api_url}")
    print(f"- api_endpoint: {client.api_endpoint}")
    print(f"- auth_mode: {client.auth_mode}")

    if transport in ("sse", "streamable-http"):
        print("\n多租户 HTTP 头认证（优先级高于环境变量）:")
        print("- X-ZStack-Account: 账户名")
        print("- X-ZStack-Password: 密码")
        print("- X-ZStack-Session-Id: 已有 Session UUID")
        print("- X-ZStack-API-URL: ZStack 管理节点地址（可代理多套环境）")

    example = _build_json_import_example(transport, endpoint)
    print("\nJSON 导入示例:")
    print(json.dumps(example, ensure_ascii=False, indent=2))


def main():
    """主入口函数"""
    parser = argparse.ArgumentParser(description="ZStack MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=_normalize_transport(_get_first_env("MCP_TRANSPORT", "FASTMCP_TRANSPORT")),
        help="MCP 传输模式（默认: stdio）",
    )
    parser.add_argument(
        "--host",
        default=_get_first_env("MCP_HOST", "FASTMCP_HOST"),
        help="SSE 模式绑定地址（可选）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_get_first_env("MCP_PORT", "FASTMCP_PORT"))
        if _get_first_env("MCP_PORT", "FASTMCP_PORT")
        else None,
        help="SSE 模式端口（可选）",
    )
    parser.add_argument(
        "--path",
        default=_get_first_env("MCP_PATH", "FASTMCP_MOUNT_PATH"),
        help="SSE 模式挂载路径（可选）",
    )
    parser.add_argument(
        "--streamable-path",
        default=_get_first_env("MCP_STREAMABLE_PATH", "FASTMCP_STREAMABLE_HTTP_PATH"),
        help="Streamable HTTP 模式路径（可选）",
    )
    args = parser.parse_args()

    transport = _normalize_transport(args.transport)
    streamable_path = args.streamable_path
    if transport == "streamable-http" and not streamable_path:
        streamable_path = args.path

    _apply_fastmcp_network_settings(args.host, args.port, streamable_path)
    _print_startup_summary(transport, args.path if transport == "sse" else None, streamable_path)

    def _shutdown_cleanup():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_session_mgr.logout_all())
            else:
                loop.run_until_complete(_session_mgr.logout_all())
        except RuntimeError:
            asyncio.run(_session_mgr.logout_all())

    atexit.register(_shutdown_cleanup)

    mcp.run(transport=transport, mount_path=args.path if transport == "sse" else None)


if __name__ == "__main__":
    main()

