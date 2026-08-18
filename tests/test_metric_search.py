import pytest

from zstack_mcp.metric_search import MetricInfo, MetricSearchIndex


def _add_metric(index: MetricSearchIndex, name: str, namespace: str) -> None:
    metric = MetricInfo(
        name=name,
        description="",
        namespace=namespace,
        label_names=[],
        driver="",
    )
    metric.tokens = index._split_camel_case(name)
    index.metrics[name] = metric
    index.namespaces.add(namespace)
    for token in metric.tokens:
        index.inverted_index.setdefault(token, set()).add(name)


def test_search_metric_namespace_fuzzy_match() -> None:
    index = MetricSearchIndex()
    _add_metric(index, "CPUUsedUtilization", "ZStack/Host")
    _add_metric(index, "MemoryUsedInPercent", "ZStack/VM")

    results_vm = index.search(["Memory"], namespace="vm", limit=10)
    assert len(results_vm) == 1
    assert results_vm[0]["name"] == "MemoryUsedInPercent"

    results_host = index.search(["CPU"], namespace="host", limit=10)
    assert len(results_host) == 1
    assert results_host[0]["name"] == "CPUUsedUtilization"

    results_none = index.search(["CPU"], namespace="vm", limit=10)
    assert results_none == []
