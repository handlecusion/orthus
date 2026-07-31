"""Nova ML platform connector — read-only pulls over the Tailscale tailnet.

The company dashboard's infra page surfaces live GPU/experiment data from the
Nova k3s cluster (separate repo `nova-ml-platform`). Two upstreams, both
reachable only on the tailnet and both without app auth (the tailnet + EC2 SG
are their only gate):

  * VictoriaMetrics PromQL API (NodePort 30428) — GPU/DCGM time series.
  * MLflow REST + UI (NodePort 30500) — experiment runs.

This module exposes a FIXED read-only surface: callers pick from the methods
below, never pass raw PromQL/MLflow filter strings. orthus's own web may be
reachable beyond the tailnet, so we must not turn the backend into a generic
proxy into an auth-less cluster.

Mirrors the `NotionConnector` shape (injectable `httpx.Client` for tests via
`httpx.MockTransport`). VM/MLflow calls are interactive (a dashboard sync click
or page load), so failures fail fast with a Korean operator-facing `ValueError`
— the route layer maps that to HTTP 422, matching the legacy SSH sync UX.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

# GPU utilization is read with a 5-minute average. The A100 node runs driver 535,
# where DCGM's instantaneous GR_ENGINE_ACTIVE is unavailable and GPU_UTIL is
# coarse/spiky; the Grafana KPI dashboard smooths the same way.
_GPU_UTIL_QUERY = "avg_over_time(DCGM_FI_DEV_GPU_UTIL[5m])"
_FB_USED_QUERY = "DCGM_FI_DEV_FB_USED"
_FB_FREE_QUERY = "DCGM_FI_DEV_FB_FREE"
_GPU_TEMP_QUERY = "DCGM_FI_DEV_GPU_TEMP"

# node_exporter filesystem metrics. Exclude pseudo filesystems so the data
# volume (ext4/xfs/zfs/btrfs) is what surfaces, not tmpfs/overlay noise.
_FS_EXCLUDE = "tmpfs|overlay|squashfs|devtmpfs|ramfs|iso9660|fuse.*"
_FS_SIZE_QUERY = f'node_filesystem_size_bytes{{fstype!~"{_FS_EXCLUDE}"}}'
_FS_AVAIL_QUERY = f'node_filesystem_avail_bytes{{fstype!~"{_FS_EXCLUDE}"}}'

# Busy threshold (percent). Matches the legacy SSH path (utilization >= 10).
BUSY_UTIL_THRESHOLD = 10


class GpuDevice(BaseModel):
    node: str  # k8s node name (DCGM `node`/`Hostname` label)
    index: str  # per-node GPU index (DCGM `gpu` label)
    model: str = ""  # DCGM `modelName` label
    util: float = 0.0  # avg_over_time(GPU_UTIL[5m])
    mem_used_mib: float = 0.0
    mem_total_mib: float = 0.0
    temp_c: float | None = None


class StorageFS(BaseModel):
    node: str  # node_exporter `node` label
    mountpoint: str
    fstype: str = ""
    size_bytes: float = 0.0
    used_bytes: float = 0.0


class NovaMLClient:
    """Read-only client for the Nova VictoriaMetrics + MLflow endpoints.

    vm_client / mlflow_client let tests inject an httpx.Client backed by
    MockTransport; in production they are built from vm_url / mlflow_url.
    """

    def __init__(
        self,
        *,
        vm_url: str = "",
        mlflow_url: str = "",
        grafana_url: str = "",
        timeout: float = 8.0,
        vm_client: httpx.Client | None = None,
        mlflow_client: httpx.Client | None = None,
        grafana_client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout
        if vm_client is not None:
            self._vm = vm_client
        elif vm_url:
            self._vm = httpx.Client(base_url=vm_url.rstrip("/"), timeout=timeout)
        else:
            self._vm = None
        if mlflow_client is not None:
            self._mlflow = mlflow_client
        elif mlflow_url:
            self._mlflow = httpx.Client(base_url=mlflow_url.rstrip("/"), timeout=timeout)
        else:
            self._mlflow = None
        if grafana_client is not None:
            self._grafana = grafana_client
        elif grafana_url:
            # Short timeout: Grafana health/discovery is best-effort on page load.
            self._grafana = httpx.Client(
                base_url=grafana_url.rstrip("/"), timeout=min(timeout, 4.0)
            )
        else:
            self._grafana = None

    # ---- VictoriaMetrics ----------------------------------------------------
    def query_instant(self, promql: str) -> list[dict]:
        """Run one instant PromQL query, return the result vector (list of
        {metric, value}). Raises ValueError on connect/timeout/non-success."""
        if self._vm is None:
            raise ValueError("VictoriaMetrics URL이 설정되지 않았습니다 (ORTHUS_NOVA_ML_VM_URL).")
        try:
            resp = self._vm.get("/api/v1/query", params={"query": promql})
        except httpx.HTTPError as e:
            raise ValueError(f"VictoriaMetrics 접속 실패: {e}") from e
        if resp.status_code != 200:
            raise ValueError(
                f"VictoriaMetrics 질의 실패 (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        body = resp.json()
        if body.get("status") != "success":
            raise ValueError(f"VictoriaMetrics 질의 오류: {str(body)[:200]}")
        return body.get("data", {}).get("result", [])

    def gpu_devices(self) -> list[GpuDevice]:
        """Pull per-GPU metrics and join them on (node, gpu) into GpuDevice rows."""
        util = self.query_instant(_GPU_UTIL_QUERY)
        fb_used = self.query_instant(_FB_USED_QUERY)
        fb_free = self.query_instant(_FB_FREE_QUERY)
        temp = self.query_instant(_GPU_TEMP_QUERY)

        devices: dict[tuple[str, str], GpuDevice] = {}
        for sample in util:
            metric = sample.get("metric", {})
            node = _node_key(metric)
            index = str(metric.get("gpu", ""))
            devices[(node, index)] = GpuDevice(
                node=node,
                index=index,
                model=str(metric.get("modelName", "")),
                util=_sample_value(sample),
            )

        def _apply(samples: list[dict], setter) -> None:
            for sample in samples:
                metric = sample.get("metric", {})
                key = (_node_key(metric), str(metric.get("gpu", "")))
                dev = devices.get(key)
                if dev is not None:
                    setter(dev, _sample_value(sample))

        _apply(fb_used, lambda d, v: setattr(d, "mem_used_mib", v))
        _apply(fb_free, lambda d, v: setattr(d, "mem_total_mib", d.mem_used_mib + v))
        _apply(temp, lambda d, v: setattr(d, "temp_c", v))

        # Stable order: node, then numeric GPU index.
        return sorted(devices.values(), key=lambda d: (d.node, _int_or(d.index)))

    def storage_filesystems(self) -> list[StorageFS]:
        """Pull node_exporter filesystem metrics and return the LARGEST real
        filesystem per node (the data volume). Skips pseudo filesystems
        (tmpfs/overlay/etc). Empty when no node_exporter reports fs metrics."""
        size = self.query_instant(_FS_SIZE_QUERY)
        avail = self.query_instant(_FS_AVAIL_QUERY)

        avail_by_key: dict[tuple[str, str], float] = {}
        for s in avail:
            m = s.get("metric", {})
            avail_by_key[(_node_key(m), str(m.get("mountpoint", "")))] = _sample_value(s)

        # Largest mount per node wins (the bulk data volume).
        best: dict[str, StorageFS] = {}
        for s in size:
            m = s.get("metric", {})
            node = _node_key(m)
            mount = str(m.get("mountpoint", ""))
            total = _sample_value(s)
            used = max(0.0, total - avail_by_key.get((node, mount), 0.0))
            cur = best.get(node)
            if cur is None or total > cur.size_bytes:
                best[node] = StorageFS(
                    node=node,
                    mountpoint=mount,
                    fstype=str(m.get("fstype", "")),
                    size_bytes=total,
                    used_bytes=used,
                )
        return sorted(best.values(), key=lambda f: f.node)

    # ---- MLflow -------------------------------------------------------------
    def recent_runs(self, limit: int = 20) -> list[dict]:
        """Return the most recent runs across all experiments as raw MLflow run
        dicts, each annotated with `_experiment_name`. Raises ValueError on
        connect/timeout/non-2xx."""
        if self._mlflow is None:
            raise ValueError("MLflow URL이 설정되지 않았습니다 (ORTHUS_NOVA_ML_MLFLOW_URL).")
        experiments = self._mlflow_get(
            "/api/2.0/mlflow/experiments/search", params={"max_results": 200}
        ).get("experiments", [])
        if not experiments:
            return []
        names = {e["experiment_id"]: e.get("name", "") for e in experiments}
        body = self._mlflow_post(
            "/api/2.0/mlflow/runs/search",
            json={
                "experiment_ids": list(names.keys()),
                "order_by": ["attributes.start_time DESC"],
                "max_results": limit,
            },
        )
        runs = body.get("runs", [])
        for run in runs:
            exp_id = run.get("info", {}).get("experiment_id", "")
            run["_experiment_name"] = names.get(exp_id, "")
        return runs

    # ---- Grafana (best-effort discovery; never raises) ----------------------
    def grafana_health(self) -> bool:
        """True if Grafana answers /api/health (no auth needed). Best-effort:
        any error → False, so the page can fall back instead of embedding a
        broken iframe."""
        if self._grafana is None:
            return False
        try:
            resp = self._grafana.get("/api/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def resolve_dashboard_uid(self, preferred: str = "") -> str:
        """Pick a dashboard uid to embed WITHOUT hardcoding one.

        Order: (1) `preferred` if it still exists; (2) first dashboard whose
        title mentions GPU; (3) first dashboard of any kind. Returns "" when
        Grafana is unreachable or has no dashboards — caller then shows a link
        instead of an iframe. Tolerant of Grafana version/dashboard changes."""
        if self._grafana is None:
            return ""
        try:
            if preferred:
                resp = self._grafana.get(f"/api/dashboards/uid/{preferred}")
                if resp.status_code == 200:
                    return preferred
            resp = self._grafana.get("/api/search", params={"type": "dash-db", "limit": 100})
            if resp.status_code != 200:
                return ""
            items = resp.json()
        except httpx.HTTPError:
            return ""
        if not isinstance(items, list) or not items:
            return ""
        gpu = [d for d in items if "gpu" in str(d.get("title", "")).lower()]
        pick = gpu[0] if gpu else items[0]
        return str(pick.get("uid", ""))

    def _mlflow_get(self, path: str, *, params: dict | None = None) -> dict:
        try:
            resp = self._mlflow.get(path, params=params)
        except httpx.HTTPError as e:
            raise ValueError(f"MLflow 접속 실패: {e}") from e
        return self._mlflow_json(resp)

    def _mlflow_post(self, path: str, *, json: dict) -> dict:
        try:
            resp = self._mlflow.post(path, json=json)
        except httpx.HTTPError as e:
            raise ValueError(f"MLflow 접속 실패: {e}") from e
        return self._mlflow_json(resp)

    @staticmethod
    def _mlflow_json(resp: httpx.Response) -> dict:
        if resp.status_code != 200:
            raise ValueError(f"MLflow 질의 실패 (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()


def _node_key(metric: dict[str, Any]) -> str:
    """Resolve a stable node identity from DCGM labels. dcgm-exporter emits both
    `node` and `Hostname` on the Nova cluster; fall back to `instance` then a
    constant so a missing label never silently merges distinct GPUs."""
    return metric.get("node") or metric.get("Hostname") or metric.get("instance") or "gpu"


def _sample_value(sample: dict) -> float:
    """Extract the float value from a PromQL instant-vector sample
    ({"value": [<ts>, "<str>"]}). Returns 0.0 if absent/unparseable."""
    value = sample.get("value")
    if not value or len(value) < 2:
        return 0.0
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return 0.0


def _int_or(index: str, default: int = 0) -> int:
    try:
        return int(index)
    except (TypeError, ValueError):
        return default
