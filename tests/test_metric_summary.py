import json
from unittest.mock import AsyncMock

import pytest

import zstack_mcp.server as server


class DummyClient:
    def __init__(self, responses: dict[str, dict]):
        self.responses = responses

    async def query_metric_data(self, namespace, metric_name, start_time, end_time, period, labels):
        return self.responses[metric_name]

    async def execute(self, *args, **kwargs):
        return {}


def _patch_session_mgr(monkeypatch, dummy: DummyClient):
    """让 _session_mgr.get_client 返回 dummy client"""
    mock_get = AsyncMock(return_value=dummy)
    monkeypatch.setattr(server._session_mgr, "get_client", mock_get)


@pytest.mark.anyio
async def test_get_metric_summary_threshold(monkeypatch) -> None:
    responses = {
        "CPUOccupiedByVm": {
            "data": [
                {"labels": {"VMUuid": "a"}, "value": 50},
                {"labels": {"VMUuid": "a"}, "value": 90},
                {"labels": {"VMUuid": "b"}, "value": 10},
            ]
        }
    }
    dummy = DummyClient(responses)
    _patch_session_mgr(monkeypatch, dummy)

    result = await server.get_metric_summary(
        namespace="ZStack/VM",
        metric_name="CPUOccupiedByVm",
        label_key="VMUuid",
        aggregate="max",
        threshold_op=">=",
        threshold_value=80,
        top_n=10,
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 1
    row = data["result"][0]
    assert row["labelValue"] == "a"
    assert row["aggregateValue"] == 90
    assert row["stats"]["max"] == 90


@pytest.mark.anyio
async def test_get_metric_summary_combine(monkeypatch) -> None:
    responses = {
        "TotalNetworkInBytesIn5Min": {
            "data": [
                {"labels": {"VMUuid": "a"}, "value": 1},
                {"labels": {"VMUuid": "a"}, "value": 2},
                {"labels": {"VMUuid": "b"}, "value": 10},
            ]
        },
        "TotalNetworkOutBytesIn5Min": {
            "data": [
                {"labels": {"VMUuid": "a"}, "value": 4},
                {"labels": {"VMUuid": "b"}, "value": 1},
            ]
        },
    }
    dummy = DummyClient(responses)
    _patch_session_mgr(monkeypatch, dummy)

    result = await server.get_metric_summary(
        namespace="ZStack/VM",
        metric_name="TotalNetworkInBytesIn5Min",
        metric_names=["TotalNetworkOutBytesIn5Min"],
        label_key="VMUuid",
        aggregate="sum",
        combine="sum",
        top_n=2,
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["metrics"] == ["TotalNetworkInBytesIn5Min", "TotalNetworkOutBytesIn5Min"]
    assert data["count"] == 2
    # label b should be first (10 + 1)
    first = data["result"][0]
    assert first["labelValue"] == "b"
    assert first["aggregateValue"] == 11
