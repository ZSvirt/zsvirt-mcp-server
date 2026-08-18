from datetime import datetime, timezone

from zstack_mcp.zstack_client import ZStackClient


def test_normalize_metric_time_iso_and_epoch() -> None:
    dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    epoch = int(dt.timestamp())
    millis = epoch * 1000

    assert ZStackClient._normalize_metric_time("2026-01-01T00:00:00Z") == epoch
    assert ZStackClient._normalize_metric_time("2026-01-01 00:00:00") == epoch
    assert ZStackClient._normalize_metric_time(epoch) == epoch
    assert ZStackClient._normalize_metric_time(millis) == epoch
    assert ZStackClient._normalize_metric_time(str(millis)) == epoch


def test_normalize_metric_labels_variants() -> None:
    assert ZStackClient._normalize_metric_labels(["VMUuid=abc"]) == ["VMUuid=abc"]
    assert ZStackClient._normalize_metric_labels({"VMUuid": "abc"}) == ["VMUuid=abc"]
    assert ZStackClient._normalize_metric_labels([{"key": "VMUuid", "value": "abc"}]) == ["VMUuid=abc"]
    assert ZStackClient._normalize_metric_labels([{"name": "HostUuid", "val": "def"}]) == ["HostUuid=def"]
    assert ZStackClient._normalize_metric_labels(["  VMUuid=abc  ", ""]) == ["VMUuid=abc"]
