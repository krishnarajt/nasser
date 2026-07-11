from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiException

from nasser_bot.config import Config


def shorten_k8s_error(exc: BaseException | str) -> str:
    """Collapse noisy urllib3/kubernetes errors into one readable line."""
    text = str(exc)
    if "FileNotFoundError" in text or "Invalid kube-config" in text or "kube-config file" in text:
        return "kubeconfig not found or invalid. Check the KUBECONFIG env setting."
    if any(marker in text for marker in ("Max retries exceeded", "Connection refused", "NewConnectionError")):
        match = re.search(r"host='([^']+)', port=(\d+)", text)
        target = f" at {match.group(1)}:{match.group(2)}" if match else ""
        return f"API unreachable{target}. Is the k3s service running?"
    if "timed out" in text.lower():
        return "API request timed out. k3s may be starting or overloaded."
    first = text.splitlines()[0] if text else "unknown error"
    return first if len(first) <= 200 else f"{first[:197]}..."


@dataclass(frozen=True)
class K8sNodeSummary:
    name: str
    ready: str
    roles: list[str]
    kubelet_version: str | None


@dataclass(frozen=True)
class K8sDeploymentSummary:
    namespace: str
    name: str
    desired: int
    ready: int
    available: int
    updated: int


@dataclass(frozen=True)
class K8sPodSummary:
    namespace: str
    name: str
    phase: str
    node: str | None
    restarts: int
    containers: list[str]
    issues: list[str]


@dataclass(frozen=True)
class K8sEventSummary:
    namespace: str
    type: str
    reason: str
    object_kind: str
    object_name: str
    message: str
    count: int | None
    timestamp: datetime | None


@dataclass(frozen=True)
class K8sSummary:
    available: bool
    nodes: list[K8sNodeSummary]
    namespaces: int
    deployments: int
    pods: int
    error: str | None = None


@dataclass(frozen=True)
class K8sPodMetrics:
    namespace: str
    name: str
    cpu_millicores: float
    memory_bytes: int


@dataclass(frozen=True)
class K8sNodeMetrics:
    name: str
    cpu_millicores: float
    memory_bytes: int


class K8sService:
    def __init__(self, app_config: Config) -> None:
        self._config = app_config

    def _load(self) -> None:
        if self._config.kubeconfig:
            config.load_kube_config(config_file=str(self._config.kubeconfig))
        else:
            config.load_kube_config()

    async def summary(self) -> K8sSummary:
        return await asyncio.to_thread(self._summary_sync)

    def _summary_sync(self) -> K8sSummary:
        try:
            self._load()
            core = client.CoreV1Api()
            apps = client.AppsV1Api()
            nodes = _nodes_from_response(core.list_node())
            namespaces = core.list_namespace().items
            deployments = apps.list_deployment_for_all_namespaces().items
            pods = core.list_pod_for_all_namespaces().items
            return K8sSummary(
                available=True,
                nodes=nodes,
                namespaces=len(namespaces),
                deployments=len(deployments),
                pods=len(pods),
            )
        except Exception as exc:
            return K8sSummary(
                available=False,
                nodes=[],
                namespaces=0,
                deployments=0,
                pods=0,
                error=shorten_k8s_error(exc),
            )

    async def namespaces(self) -> list[str]:
        return await asyncio.to_thread(self._namespaces_sync)

    def _namespaces_sync(self) -> list[str]:
        self._load()
        core = client.CoreV1Api()
        return sorted(ns.metadata.name for ns in core.list_namespace().items)

    async def nodes(self) -> list[K8sNodeSummary]:
        return await asyncio.to_thread(self._nodes_sync)

    def _nodes_sync(self) -> list[K8sNodeSummary]:
        self._load()
        core = client.CoreV1Api()
        return _nodes_from_response(core.list_node())

    async def deployments(self, namespace: str) -> list[K8sDeploymentSummary]:
        return await asyncio.to_thread(self._deployments_sync, namespace)

    def _deployments_sync(self, namespace: str) -> list[K8sDeploymentSummary]:
        self._load()
        apps = client.AppsV1Api()
        rows: list[K8sDeploymentSummary] = []
        for deployment in apps.list_namespaced_deployment(namespace).items:
            status = deployment.status
            spec = deployment.spec
            rows.append(
                K8sDeploymentSummary(
                    namespace=namespace,
                    name=deployment.metadata.name,
                    desired=spec.replicas or 0,
                    ready=status.ready_replicas or 0,
                    available=status.available_replicas or 0,
                    updated=status.updated_replicas or 0,
                )
            )
        return sorted(rows, key=lambda item: item.name.lower())

    async def pods(self, namespace: str) -> list[K8sPodSummary]:
        return await asyncio.to_thread(self._pods_sync, namespace)

    def _pods_sync(self, namespace: str) -> list[K8sPodSummary]:
        self._load()
        core = client.CoreV1Api()
        rows: list[K8sPodSummary] = []
        for pod in core.list_namespaced_pod(namespace).items:
            rows.append(_pod_summary(pod))
        return sorted(rows, key=lambda item: (item.phase != "Running", item.name.lower()))

    async def all_pods(self) -> list[K8sPodSummary]:
        return await asyncio.to_thread(self._all_pods_sync)

    def _all_pods_sync(self) -> list[K8sPodSummary]:
        self._load()
        core = client.CoreV1Api()
        rows = [_pod_summary(pod) for pod in core.list_pod_for_all_namespaces().items]
        return sorted(rows, key=lambda item: (item.namespace, item.name))

    async def events(
        self,
        namespace: str | None = None,
        *,
        warnings_only: bool = False,
        limit: int = 30,
    ) -> list[K8sEventSummary]:
        return await asyncio.to_thread(self._events_sync, namespace, warnings_only, limit)

    def _events_sync(
        self,
        namespace: str | None,
        warnings_only: bool,
        limit: int,
    ) -> list[K8sEventSummary]:
        self._load()
        core = client.CoreV1Api()
        if namespace:
            events = core.list_namespaced_event(namespace).items
        else:
            events = core.list_event_for_all_namespaces().items

        rows: list[K8sEventSummary] = []
        for event in events:
            event_type = event.type or "Normal"
            if warnings_only and event_type != "Warning":
                continue
            involved = event.involved_object
            rows.append(
                K8sEventSummary(
                    namespace=event.metadata.namespace or namespace or "default",
                    type=event_type,
                    reason=event.reason or "unknown",
                    object_kind=involved.kind if involved else "Object",
                    object_name=involved.name if involved else "unknown",
                    message=event.message or "",
                    count=event.count,
                    timestamp=event.last_timestamp
                    or getattr(event, "event_time", None)
                    or event.metadata.creation_timestamp,
                )
            )
        rows.sort(key=lambda item: item.timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:limit]

    async def pod_logs(self, namespace: str, pod: str, container: str | None, lines: int) -> str:
        return await asyncio.to_thread(self._pod_logs_sync, namespace, pod, container, lines)

    def _pod_logs_sync(self, namespace: str, pod: str, container: str | None, lines: int) -> str:
        self._load()
        core = client.CoreV1Api()
        try:
            return core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container,
                tail_lines=lines,
                timestamps=True,
            )
        except ApiException as exc:
            raise RuntimeError(exc.reason or str(exc)) from exc

    async def top_pods(self) -> list[K8sPodMetrics]:
        return await asyncio.to_thread(self._top_pods_sync)

    def _top_pods_sync(self) -> list[K8sPodMetrics]:
        self._load()
        custom = client.CustomObjectsApi()
        try:
            response = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
        except ApiException as exc:
            raise RuntimeError(_metrics_error(exc)) from exc
        rows: list[K8sPodMetrics] = []
        for item in response.get("items", []):
            metadata = item.get("metadata", {})
            cpu = 0.0
            memory = 0
            for container in item.get("containers", []):
                usage = container.get("usage", {})
                cpu += parse_cpu_quantity(usage.get("cpu", "0"))
                memory += parse_memory_quantity(usage.get("memory", "0"))
            rows.append(
                K8sPodMetrics(
                    namespace=metadata.get("namespace", "default"),
                    name=metadata.get("name", "unknown"),
                    cpu_millicores=cpu,
                    memory_bytes=memory,
                )
            )
        return sorted(rows, key=lambda row: row.cpu_millicores, reverse=True)

    async def top_nodes(self) -> list[K8sNodeMetrics]:
        return await asyncio.to_thread(self._top_nodes_sync)

    def _top_nodes_sync(self) -> list[K8sNodeMetrics]:
        self._load()
        custom = client.CustomObjectsApi()
        try:
            response = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        except ApiException as exc:
            raise RuntimeError(_metrics_error(exc)) from exc
        rows: list[K8sNodeMetrics] = []
        for item in response.get("items", []):
            usage = item.get("usage", {})
            rows.append(
                K8sNodeMetrics(
                    name=item.get("metadata", {}).get("name", "unknown"),
                    cpu_millicores=parse_cpu_quantity(usage.get("cpu", "0")),
                    memory_bytes=parse_memory_quantity(usage.get("memory", "0")),
                )
            )
        return sorted(rows, key=lambda row: row.name.lower())

    async def rollout_restart_deployment(self, namespace: str, deployment: str) -> str:
        return await asyncio.to_thread(self._rollout_restart_deployment_sync, namespace, deployment)

    def _rollout_restart_deployment_sync(self, namespace: str, deployment: str) -> str:
        self._load()
        apps = client.AppsV1Api()
        restarted_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        body: dict[str, Any] = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": restarted_at,
                        }
                    }
                }
            }
        }
        apps.patch_namespaced_deployment(name=deployment, namespace=namespace, body=body)
        return f"Requested rollout restart for deployment {namespace}/{deployment}"


def _metrics_error(exc: ApiException) -> str:
    if exc.status == 404:
        return "Metrics API not available. Is metrics-server running in the cluster?"
    return shorten_k8s_error(exc)


_MEMORY_SUFFIXES = [
    ("Ki", 1024),
    ("Mi", 1024**2),
    ("Gi", 1024**3),
    ("Ti", 1024**4),
    ("Pi", 1024**5),
    ("K", 1000),
    ("M", 1000**2),
    ("G", 1000**3),
    ("T", 1000**4),
    ("P", 1000**5),
]


def parse_cpu_quantity(value: str) -> float:
    """Kubernetes CPU quantity ("250m", "1", "123456n") to millicores."""
    text = str(value).strip()
    try:
        if text.endswith("n"):
            return int(text[:-1]) / 1_000_000
        if text.endswith("u"):
            return int(text[:-1]) / 1_000
        if text.endswith("m"):
            return float(text[:-1])
        return float(text) * 1000.0
    except ValueError:
        return 0.0


def parse_memory_quantity(value: str) -> int:
    """Kubernetes memory quantity ("128Mi", "1Gi", "1024") to bytes."""
    text = str(value).strip()
    for suffix, multiplier in _MEMORY_SUFFIXES:
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _nodes_from_response(response: Any) -> list[K8sNodeSummary]:
    rows: list[K8sNodeSummary] = []
    for node in response.items:
        labels = node.metadata.labels or {}
        roles = [
            label.removeprefix("node-role.kubernetes.io/")
            for label in labels
            if label.startswith("node-role.kubernetes.io/")
        ]
        ready = "unknown"
        for condition in node.status.conditions or []:
            if condition.type == "Ready":
                ready = "ready" if condition.status == "True" else "not ready"
                break
        rows.append(
            K8sNodeSummary(
                name=node.metadata.name,
                ready=ready,
                roles=sorted(role or "control-plane" for role in roles),
                kubelet_version=node.status.node_info.kubelet_version
                if node.status.node_info
                else None,
            )
        )
    return sorted(rows, key=lambda item: item.name.lower())


def _pod_summary(pod: Any) -> K8sPodSummary:
    statuses = pod.status.container_statuses or []
    issues: list[str] = []
    for status in statuses:
        state = status.state
        waiting = state.waiting if state and state.waiting else None
        terminated = state.terminated if state and state.terminated else None
        if waiting and waiting.reason:
            issues.append(f"{status.name}: {waiting.reason}")
        if terminated and terminated.reason and terminated.reason not in {"Completed"}:
            issues.append(f"{status.name}: {terminated.reason}")
        if status.restart_count and status.restart_count >= 3:
            issues.append(f"{status.name}: {status.restart_count} restarts")

    phase = pod.status.phase or "unknown"
    if phase not in {"Running", "Succeeded"}:
        issues.append(f"phase: {phase}")

    return K8sPodSummary(
        namespace=pod.metadata.namespace or "default",
        name=pod.metadata.name,
        phase=phase,
        node=pod.spec.node_name,
        restarts=sum(status.restart_count or 0 for status in statuses),
        containers=[container.name for container in pod.spec.containers or []],
        issues=issues,
    )
