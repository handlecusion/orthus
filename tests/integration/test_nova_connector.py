"""NovaMLClient unit tests — VM gpu_devices join + MLflow recent_runs.

Uses httpx.MockTransport so no live network calls are made.
"""

from __future__ import annotations

import httpx
import pytest

from orthus.connectors.nova import NovaMLClient


def _vector(rows: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "vector", "result": rows}}
    )


def _sample(metric: dict, value: str) -> dict:
    return {"metric": metric, "value": [1781236178, value]}


def _vm(handler) -> NovaMLClient:
    return NovaMLClient(
        vm_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://vm")
    )


def test_gpu_devices_joins_on_node_and_gpu() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        m0 = {"node": "gpu-a100", "gpu": "0", "modelName": "A100"}
        m1 = {"node": "gpu-a100", "gpu": "1", "modelName": "A100"}
        if "GPU_UTIL" in q:
            return _vector([_sample(m0, "42"), _sample(m1, "3")])
        if "FB_USED" in q:
            return _vector([_sample(m0, "30000"), _sample(m1, "200")])
        if "FB_FREE" in q:
            return _vector([_sample(m0, "51000"), _sample(m1, "80800")])
        if "GPU_TEMP" in q:
            return _vector([_sample(m0, "61"), _sample(m1, "38")])
        return httpx.Response(404)

    devices = _vm(handler).gpu_devices()
    assert len(devices) == 2
    g0, g1 = devices  # sorted by (node, index)
    assert g0.index == "0" and g0.util == 42.0
    assert g0.mem_used_mib == 30000.0
    assert g0.mem_total_mib == 81000.0  # used + free
    assert g0.temp_c == 61.0
    assert g1.index == "1" and g1.util == 3.0


def test_gpu_devices_missing_temp_series() -> None:
    """A GPU absent from the temp vector keeps temp_c=None, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        m = {"node": "gpu-a100", "gpu": "0", "modelName": "A100"}
        if "GPU_UTIL" in q:
            return _vector([_sample(m, "10")])
        if "FB_USED" in q:
            return _vector([_sample(m, "100")])
        if "FB_FREE" in q:
            return _vector([_sample(m, "900")])
        if "GPU_TEMP" in q:
            return _vector([])  # no temp samples
        return httpx.Response(404)

    devices = _vm(handler).gpu_devices()
    assert len(devices) == 1
    assert devices[0].temp_c is None


def test_gpu_devices_falls_back_to_hostname_label() -> None:
    """When `node` is absent, the Hostname label identifies the node."""

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        m = {"Hostname": "gpu-h200", "gpu": "0"}
        if "GPU_UTIL" in q:
            return _vector([_sample(m, "55")])
        return _vector([_sample(m, "0")])

    devices = _vm(handler).gpu_devices()
    assert devices[0].node == "gpu-h200"


def test_query_instant_raises_on_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "error": "bad query"})

    with pytest.raises(ValueError):
        _vm(handler).query_instant("nonsense")


def test_query_instant_raises_when_unconfigured() -> None:
    with pytest.raises(ValueError):
        NovaMLClient().query_instant("up")


def test_recent_runs_annotates_experiment_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/experiments/search"):
            return httpx.Response(
                200, json={"experiments": [{"experiment_id": "7", "name": "vace"}]}
            )
        if request.url.path.endswith("/runs/search"):
            return httpx.Response(
                200,
                json={"runs": [{"info": {"run_id": "r1", "experiment_id": "7"}, "data": {}}]},
            )
        return httpx.Response(404)

    client = NovaMLClient(
        mlflow_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mlflow")
    )
    runs = client.recent_runs()
    assert runs[0]["_experiment_name"] == "vace"


def test_recent_runs_empty_experiments_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"experiments": []})

    client = NovaMLClient(
        mlflow_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mlflow")
    )
    assert client.recent_runs() == []


def test_storage_filesystems_picks_largest_per_node() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        big = {"node": "bignas-v1", "mountpoint": "/share/CACHEDEV1_DATA", "fstype": "ext4"}
        small = {"node": "bignas-v1", "mountpoint": "/", "fstype": "ext4"}
        if "size_bytes" in q:
            return _vector([_sample(big, "100e12"), _sample(small, "1e12")])
        if "avail_bytes" in q:
            return _vector([_sample(big, "40e12"), _sample(small, "0.5e12")])
        return httpx.Response(404)

    fs = _vm(handler).storage_filesystems()
    assert len(fs) == 1  # largest mount per node
    assert fs[0].mountpoint == "/share/CACHEDEV1_DATA"
    assert fs[0].size_bytes == 100e12
    assert fs[0].used_bytes == 60e12  # 100 - 40


def test_storage_filesystems_empty_when_no_metrics() -> None:
    assert _vm(lambda r: _vector([])).storage_filesystems() == []


def _grafana(handler) -> NovaMLClient:
    return NovaMLClient(
        grafana_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gf")
    )


def test_grafana_health_true_false() -> None:
    assert _grafana(lambda r: httpx.Response(200, json={"database": "ok"})).grafana_health() is True
    assert _grafana(lambda r: httpx.Response(503)).grafana_health() is False

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert _grafana(boom).grafana_health() is False
    # Unconfigured (no grafana client) → False, never raises.
    assert NovaMLClient().grafana_health() is False


def test_resolve_dashboard_uid_prefers_existing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/dashboards/uid/nova-gpu-kpi":
            return httpx.Response(200, json={"dashboard": {"uid": "nova-gpu-kpi"}})
        return httpx.Response(404)

    assert _grafana(handler).resolve_dashboard_uid("nova-gpu-kpi") == "nova-gpu-kpi"


def test_resolve_dashboard_uid_falls_back_to_gpu_search() -> None:
    """Preferred uid gone → pick the first dashboard whose title mentions GPU."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/dashboards/uid/"):
            return httpx.Response(404)  # preferred no longer exists
        if request.url.path == "/api/search":
            return httpx.Response(
                200,
                json=[
                    {"uid": "misc", "title": "Cluster Overview"},
                    {"uid": "new-gpu", "title": "GPU Utilization v2"},
                ],
            )
        return httpx.Response(404)

    assert _grafana(handler).resolve_dashboard_uid("missing-uid") == "new-gpu"


def test_resolve_dashboard_uid_first_when_no_gpu_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[{"uid": "a", "title": "Cluster"}])
        return httpx.Response(404)

    assert _grafana(handler).resolve_dashboard_uid() == "a"


def test_resolve_dashboard_uid_empty_when_none_or_down() -> None:
    # No dashboards at all → "".
    assert _grafana(lambda r: httpx.Response(200, json=[])).resolve_dashboard_uid() == ""

    # Grafana unreachable → "", never raises.
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert _grafana(boom).resolve_dashboard_uid() == ""
    # Unconfigured → "".
    assert NovaMLClient().resolve_dashboard_uid("x") == ""
