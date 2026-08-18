#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DATA_DIR = SRC_ROOT / "zstack_mcp" / "data"
API_DOCS_PATH = DATA_DIR / "api_docs.json"
METRIC_PATH = DATA_DIR / "zs_all_metric_metadata.json"

sys.path.insert(0, str(SRC_ROOT))

from zstack_mcp.api_search import ApiSearchIndex  # noqa: E402
from zstack_mcp.metric_search import MetricSearchIndex  # noqa: E402
from zstack_mcp.zstack_client import ZStackApiError, ZStackClient  # noqa: E402


DEFAULT_PERIOD_SECONDS = 300
MAX_RESULTS = 50
MAX_RESOURCES = 30
CONCURRENCY = 6


def normalize_env() -> None:
    api_url = os.environ.get("ZSTACK_API_URL", "")
    if api_url:
        os.environ["ZSTACK_API_URL"] = api_url.strip()

    if not os.environ.get("ZSTACK_ACCOUNT"):
        fallback = os.environ.get("ZSTACK_ZSTACK_ACCOUNT")
        if fallback:
            os.environ["ZSTACK_ACCOUNT"] = fallback


def ensure_data_files() -> None:
    if not API_DOCS_PATH.exists():
        raise FileNotFoundError(f"Missing api docs: {API_DOCS_PATH}")
    if not METRIC_PATH.exists():
        raise FileNotFoundError(f"Missing metric metadata: {METRIC_PATH}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def zstack_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_seconds(dt: datetime) -> int:
    return int(dt.timestamp())


def load_api_index() -> ApiSearchIndex:
    index = ApiSearchIndex()
    index.load_from_file(API_DOCS_PATH)
    return index


def load_metric_index() -> MetricSearchIndex:
    index = MetricSearchIndex()
    index.load_from_file(METRIC_PATH)
    return index


def resolve_api_name(
    api_index: ApiSearchIndex,
    keywords: list[str],
    fallback_name: str,
) -> str:
    results = api_index.search(keywords, limit=5)
    for item in results:
        if item.get("name") == fallback_name:
            return fallback_name
    if results:
        return results[0].get("name", fallback_name)
    if api_index.get_api(fallback_name):
        return fallback_name
    return fallback_name


def describe_api(api_index: ApiSearchIndex, api_name: str) -> dict[str, Any]:
    detail = api_index.get_api_detail(api_name)
    return detail or {}


def extract_inventories(result: Any) -> list[dict[str, Any]]:
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


def is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def collect_metric_values(result: Any) -> list[float]:
    values: list[float] = []

    def handle_point(point: Any) -> None:
        if isinstance(point, dict):
            for key in ("value", "avg", "max", "min"):
                if key in point and is_number(point[key]):
                    values.append(float(point[key]))
                    return
        elif isinstance(point, (list, tuple)) and len(point) >= 2 and is_number(point[1]):
            values.append(float(point[1]))
        elif is_number(point):
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
                    points = series.get("dataPoints") or series.get("points") or series.get("values") or series.get("data")
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


def summarize_values(values: list[float]) -> Optional[dict[str, float]]:
    if not values:
        return None
    total = sum(values)
    return {
        "avg": total / len(values),
        "max": max(values),
        "min": min(values),
        "sum": total,
        "count": len(values),
    }


def explain_error(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    explanations = [
        ("timeout", "请求超时或后端响应慢"),
        ("connection", "连接失败或网络不可达"),
        ("permission", "权限不足"),
        ("denied", "权限被拒绝"),
        ("unauthorized", "认证失败"),
        ("not found", "资源不存在"),
        ("already exists", "资源已存在"),
        ("invalid", "参数不合法"),
        ("quota", "配额不足"),
        ("insufficient", "资源不足"),
        ("busy", "资源忙或被占用"),
        ("unavailable", "服务不可用"),
        ("conflict", "资源状态冲突"),
    ]
    for keyword, desc in explanations:
        if keyword in lowered:
            return desc
    return ""


def extract_metric_error(result: Any) -> Optional[str]:
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


@dataclass
class MetricQueryTarget:
    uuid: str
    name: str
    extra: dict[str, Any]


async def execute_api(
    client: ZStackClient,
    api_index: ApiSearchIndex,
    keywords: list[str],
    fallback_name: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    api_name = resolve_api_name(api_index, keywords, fallback_name)
    _ = describe_api(api_index, api_name)
    api_info = api_index.get_api(api_name)
    if not api_info:
        raise ValueError(f"Unknown API: {api_name}")
    return await client.execute(
        api_name=api_name,
        full_api_name=api_info.full_name,
        parameters=parameters,
        is_async=(api_info.call_type == "async"),
    )


async def query_recent_audit_errors(
    client: ZStackClient,
    api_index: ApiSearchIndex,
    start_time: str,
) -> list[dict[str, Any]]:
    params = {
        "conditions": [
            {"name": "success", "op": "=", "value": "false"},
            {"name": "startTime", "op": ">=", "value": start_time},
        ],
        "limit": MAX_RESULTS,
        "sortBy": "startTime",
        "sortDirection": "desc",
    }
    result = await execute_api(client, api_index, ["Query", "Audit"], "QueryAudit", params)
    return extract_inventories(result)


async def query_recent_long_jobs(
    client: ZStackClient,
    api_index: ApiSearchIndex,
    start_time: str,
) -> list[dict[str, Any]]:
    params = {
        "conditions": [{"name": "createDate", "op": ">=", "value": start_time}],
        "limit": MAX_RESULTS,
        "sortBy": "createDate",
        "sortDirection": "desc",
    }
    result = await execute_api(client, api_index, ["Query", "Long", "Job"], "QueryLongJob", params)
    inventories = extract_inventories(result)
    failed_states = {"failed", "error", "canceled", "cancelled"}
    filtered = [item for item in inventories if str(item.get("state", "")).lower() in failed_states]
    return filtered or inventories


async def query_alarm_records(
    client: ZStackClient,
    api_index: ApiSearchIndex,
    start_time: str,
) -> list[dict[str, Any]]:
    params = {
        "conditions": [{"name": "createTime", "op": ">=", "value": start_time}],
        "limit": MAX_RESULTS,
        "sortBy": "createTime",
        "sortDirection": "desc",
    }
    result = await execute_api(client, api_index, ["Query", "Alarm", "Record"], "QueryAlarmRecord", params)
    return extract_inventories(result)


async def query_vms(
    client: ZStackClient,
    api_index: ApiSearchIndex,
) -> list[dict[str, Any]]:
    params = {
        "conditions": [{"name": "state", "op": "=", "value": "Running"}],
        "fields": ["uuid", "name", "state", "hostUuid", "zoneUuid", "clusterUuid"],
    }
    result = await execute_api(client, api_index, ["Query", "Vm", "Instance"], "QueryVmInstance", params)
    return extract_inventories(result)


async def query_hosts(
    client: ZStackClient,
    api_index: ApiSearchIndex,
) -> list[dict[str, Any]]:
    params = {
        "fields": ["uuid", "name", "state", "status", "managementIp"],
    }
    result = await execute_api(client, api_index, ["Query", "Host"], "QueryHost", params)
    return extract_inventories(result)


async def fetch_metric_group_stats(
    client: ZStackClient,
    namespace: str,
    metric_name: str,
    label_key: str,
    name_map: dict[str, str],
    start_time: int,
    end_time: int,
    period: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        result = await client.query_metric_data(
            namespace=namespace,
            metric_name=metric_name,
            start_time=start_time,
            end_time=end_time,
            period=period,
            labels=None,
        )
    except Exception as exc:
        return [], [{"metric": metric_name, "error": str(exc)}]

    metric_error = extract_metric_error(result)
    if metric_error:
        return [], [{"metric": metric_name, "error": metric_error}]

    data_points: list[Any] = []
    if isinstance(result, dict):
        data_points = result.get("data") or []

    groups: dict[str, list[float]] = defaultdict(list)
    for point in data_points:
        if not isinstance(point, dict):
            continue
        labels = point.get("labels") or {}
        if not isinstance(labels, dict):
            continue
        label_value = labels.get(label_key)
        value = point.get("value")
        if label_value and is_number(value):
            groups[label_value].append(float(value))

    results = []
    for uuid, values in groups.items():
        stats = summarize_values(values)
        results.append(
            {
                "uuid": uuid,
                "name": name_map.get(uuid, uuid),
                "stats": stats,
                "values_count": len(values),
                "extra": {"label": uuid},
            }
        )

    return results, []


def build_metric_targets(items: list[dict[str, Any]], name_key: str = "name") -> list[MetricQueryTarget]:
    targets = []
    for item in items:
        uuid = item.get("uuid")
        name = item.get(name_key) or item.get("name") or uuid
        if uuid:
            targets.append(MetricQueryTarget(uuid=uuid, name=name, extra=item))
    return targets


def find_vm_disk_metrics(metric_index: MetricSearchIndex) -> list[dict[str, Any]]:
    candidates = []
    for metric in metric_index.metrics.values():
        if metric.namespace.lower() != "zstack/vm":
            continue
        labels_lower = [name.lower() for name in metric.label_names]
        if "vmuuid" not in labels_lower:
            continue
        name_lower = metric.name.lower()
        if "disk" in name_lower or "volume" in name_lower or "storage" in name_lower:
            if "used" in name_lower or "utilization" in name_lower or "percent" in name_lower or "capacity" in name_lower:
                candidates.append(
                    {
                        "name": metric.name,
                        "namespace": metric.namespace,
                        "labelNames": metric.label_names,
                        "description": metric.description,
                    }
                )
    return candidates


def filter_disk_alarm_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keywords = ("disk", "volume", "storage", "capacity", "space")
    matches = []
    for record in records:
        metric = str(record.get("metricName", "")).lower()
        if any(keyword in metric for keyword in keywords):
            matches.append(record)
    return matches


async def run() -> dict[str, Any]:
    normalize_env()
    ensure_data_files()

    api_index = load_api_index()
    metric_index = load_metric_index()
    client = ZStackClient()

    now = utc_now()
    start_dt = now - timedelta(days=3)
    start_time_str = zstack_time(start_dt)
    start_iso = iso_time(start_dt)
    end_iso = iso_time(now)
    metric_start = epoch_seconds(start_dt)
    metric_end = epoch_seconds(now)

    report: dict[str, Any] = {
        "meta": {
            "start_time": start_time_str,
            "end_time": zstack_time(now),
            "start_iso": start_iso,
            "end_iso": end_iso,
            "metric_start_sec": metric_start,
            "metric_end_sec": metric_end,
            "period_seconds": DEFAULT_PERIOD_SECONDS,
        }
    }

    try:
        audit_errors = await query_recent_audit_errors(client, api_index, start_time_str)
        long_jobs = await query_recent_long_jobs(client, api_index, start_time_str)
        alarm_records = await query_alarm_records(client, api_index, start_time_str)

        report["recent_audit_errors"] = audit_errors
        report["recent_long_jobs"] = long_jobs
        report["alarm_records"] = alarm_records

        analysis: dict[str, int] = defaultdict(int)
        error_details = []
        for item in audit_errors:
            api_name = item.get("apiName", "")
            error_info = item.get("error") or item.get("responseDump") or ""
            error_text = ""
            error_code = ""
            if isinstance(error_info, dict):
                error_text = error_info.get("description") or json.dumps(error_info, ensure_ascii=False)
                error_code = error_info.get("code") or ""
            else:
                error_text = str(error_info)
            key = f"{api_name}:{error_code or error_text[:80]}"
            analysis[key] += 1
            error_details.append(
                {
                    "apiName": api_name,
                    "errorCode": error_code,
                    "errorText": error_text,
                    "explanation": explain_error(error_text),
                    "startTime": item.get("startTime"),
                    "duration": item.get("duration"),
                    "requestUuid": item.get("requestUuid"),
                }
            )

        report["audit_error_analysis"] = {
            "summary": [{"key": key, "count": count} for key, count in sorted(analysis.items(), key=lambda x: x[1], reverse=True)],
            "details": error_details,
        }

        vm_items = await query_vms(client, api_index)
        host_items = await query_hosts(client, api_index)

        vm_items_sorted = sorted(vm_items, key=lambda x: str(x.get("name", "")))
        host_items_sorted = sorted(host_items, key=lambda x: str(x.get("name", "")))

        vm_truncated = len(vm_items_sorted) > MAX_RESOURCES
        host_truncated = len(host_items_sorted) > MAX_RESOURCES
        vm_items_limited = vm_items_sorted[:MAX_RESOURCES]
        host_items_limited = host_items_sorted[:MAX_RESOURCES]

        vm_targets = build_metric_targets(vm_items_limited)
        host_targets = build_metric_targets(host_items_limited)
        vm_name_map = {item.uuid: item.name for item in vm_targets}
        host_name_map = {item.uuid: item.name for item in host_targets}

        # VM CPU
        vm_cpu_results, vm_cpu_errors = await fetch_metric_group_stats(
            client=client,
            namespace="ZStack/VM",
            metric_name="CPUOccupiedByVm",
            label_key="VMUuid",
            name_map=vm_name_map,
            start_time=metric_start,
            end_time=metric_end,
            period=DEFAULT_PERIOD_SECONDS,
        )
        report["vm_cpu"] = {
            "metric": "CPUOccupiedByVm",
            "namespace": "ZStack/VM",
            "truncated": vm_truncated,
            "results": vm_cpu_results,
            "errors": vm_cpu_errors,
        }

        # VM Network
        vm_net_in, vm_net_in_errors = await fetch_metric_group_stats(
            client=client,
            namespace="ZStack/VM",
            metric_name="TotalNetworkInBytesIn5Min",
            label_key="VMUuid",
            name_map=vm_name_map,
            start_time=metric_start,
            end_time=metric_end,
            period=DEFAULT_PERIOD_SECONDS,
        )
        vm_net_out, vm_net_out_errors = await fetch_metric_group_stats(
            client=client,
            namespace="ZStack/VM",
            metric_name="TotalNetworkOutBytesIn5Min",
            label_key="VMUuid",
            name_map=vm_name_map,
            start_time=metric_start,
            end_time=metric_end,
            period=DEFAULT_PERIOD_SECONDS,
        )
        report["vm_network"] = {
            "metrics": ["TotalNetworkInBytesIn5Min", "TotalNetworkOutBytesIn5Min"],
            "namespace": "ZStack/VM",
            "truncated": vm_truncated,
            "in_results": vm_net_in,
            "out_results": vm_net_out,
            "errors": vm_net_in_errors + vm_net_out_errors,
        }

        # Host Network
        host_network_metric_in = "NetworkAllInBytes"
        host_network_metric_out = "NetworkAllOutBytes"
        host_net_in, host_net_in_errors = await fetch_metric_group_stats(
            client=client,
            namespace="ZStack/Host",
            metric_name=host_network_metric_in,
            label_key="HostUuid",
            name_map=host_name_map,
            start_time=metric_start,
            end_time=metric_end,
            period=DEFAULT_PERIOD_SECONDS,
        )
        host_net_out, host_net_out_errors = await fetch_metric_group_stats(
            client=client,
            namespace="ZStack/Host",
            metric_name=host_network_metric_out,
            label_key="HostUuid",
            name_map=host_name_map,
            start_time=metric_start,
            end_time=metric_end,
            period=DEFAULT_PERIOD_SECONDS,
        )
        report["host_network"] = {
            "metrics": [host_network_metric_in, host_network_metric_out],
            "namespace": "ZStack/Host",
            "truncated": host_truncated,
            "in_results": host_net_in,
            "out_results": host_net_out,
            "errors": host_net_in_errors + host_net_out_errors,
        }

        # VM Disk
        vm_disk_candidates = find_vm_disk_metrics(metric_index)
        disk_section: dict[str, Any] = {
            "candidates": vm_disk_candidates,
            "results": [],
            "errors": [],
            "fallback_alarm_records": [],
        }
        if vm_disk_candidates:
            chosen = vm_disk_candidates[0]
            metric_name = chosen["name"]
            disk_results, disk_errors = await fetch_metric_group_stats(
                client=client,
                namespace="ZStack/VM",
                metric_name=metric_name,
                label_key="VMUuid",
                name_map=vm_name_map,
                start_time=metric_start,
                end_time=metric_end,
                period=DEFAULT_PERIOD_SECONDS,
            )
            disk_section["metric"] = metric_name
            disk_section["results"] = disk_results
            disk_section["errors"] = disk_errors
        else:
            disk_section["fallback_alarm_records"] = filter_disk_alarm_records(alarm_records)
        report["vm_disk"] = disk_section

    finally:
        await client.close()

    return report


def main() -> None:
    try:
        report = asyncio.run(run())
    except ZStackApiError as exc:
        print(
            json.dumps(
                {"success": False, "error": str(exc), "code": exc.code, "details": exc.details},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return

    output = {"success": True, "report": report}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

