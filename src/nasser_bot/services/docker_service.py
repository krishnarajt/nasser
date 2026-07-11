from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from nasser_bot.config import Config


@dataclass(frozen=True)
class DockerContainerSummary:
    id: str
    short_id: str
    name: str
    image: str
    status: str
    labels: dict[str, str]


@dataclass(frozen=True)
class DockerContainerDetail:
    id: str
    short_id: str
    name: str
    image: str
    status: str
    created: str | None
    state: dict[str, Any]
    ports: dict[str, Any]
    labels: dict[str, str]
    mounts: list[dict[str, Any]]


@dataclass(frozen=True)
class DockerSummary:
    available: bool
    total: int
    running: int
    exited: int
    paused: int
    exited_names: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class DockerContainerStats:
    name: str
    cpu_percent: float
    memory_used: int
    memory_limit: int | None
    net_rx: int
    net_tx: int


class DockerService:
    def __init__(self, config: Config) -> None:
        self._config = config

    def _client(self) -> docker.DockerClient:
        if self._config.docker_base_url:
            return docker.DockerClient(base_url=self._config.docker_base_url)
        return docker.from_env()

    async def summary(self) -> DockerSummary:
        return await asyncio.to_thread(self._summary_sync)

    def _summary_sync(self) -> DockerSummary:
        client = self._client()
        try:
            containers = client.containers.list(all=True)
        except DockerException as exc:
            return DockerSummary(
                available=False,
                total=0,
                running=0,
                exited=0,
                paused=0,
                error=str(exc),
            )
        finally:
            client.close()
        statuses = [container.status for container in containers]
        return DockerSummary(
            available=True,
            total=len(containers),
            running=statuses.count("running"),
            exited=statuses.count("exited"),
            paused=statuses.count("paused"),
            exited_names=sorted(
                container.name for container in containers if container.status == "exited"
            ),
        )

    async def list_containers(self, status_filter: str = "all") -> list[DockerContainerSummary]:
        return await asyncio.to_thread(self._list_containers_sync, status_filter)

    def _list_containers_sync(self, status_filter: str) -> list[DockerContainerSummary]:
        client = self._client()
        try:
            containers = client.containers.list(all=True)
            rows = []
            for container in containers:
                if status_filter != "all" and container.status != status_filter:
                    continue
                rows.append(
                    DockerContainerSummary(
                        id=container.id,
                        short_id=container.short_id,
                        name=container.name,
                        image=_image_name(container),
                        status=container.status,
                        labels=container.labels or {},
                    )
                )
            return sorted(rows, key=lambda item: (item.status != "running", item.name.lower()))
        finally:
            client.close()

    async def container_detail(self, container_id: str) -> DockerContainerDetail:
        return await asyncio.to_thread(self._container_detail_sync, container_id)

    def _container_detail_sync(self, container_id: str) -> DockerContainerDetail:
        client = self._client()
        try:
            container = client.containers.get(container_id)
            container.reload()
            attrs = container.attrs
            return DockerContainerDetail(
                id=container.id,
                short_id=container.short_id,
                name=container.name,
                image=_image_name(container),
                status=container.status,
                created=attrs.get("Created"),
                state=attrs.get("State") or {},
                ports=attrs.get("NetworkSettings", {}).get("Ports") or {},
                labels=container.labels or {},
                mounts=attrs.get("Mounts") or [],
            )
        finally:
            client.close()

    async def logs(self, container_id: str, lines: int) -> str:
        return await asyncio.to_thread(self._logs_sync, container_id, lines)

    def _logs_sync(self, container_id: str, lines: int) -> str:
        client = self._client()
        try:
            container = client.containers.get(container_id)
            output = container.logs(tail=lines, timestamps=True)
            return output.decode("utf-8", errors="replace")
        finally:
            client.close()

    async def stats(self) -> list[DockerContainerStats]:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> list[DockerContainerStats]:
        client = self._client()
        try:
            containers = client.containers.list()
            # Each stats call blocks ~1s while the daemon samples twice, so
            # fan out; the client is thread-safe.
            with ThreadPoolExecutor(max_workers=16) as pool:
                rows = list(pool.map(self._container_stats_sync, containers))
            return sorted(
                (row for row in rows if row is not None),
                key=lambda item: item.cpu_percent,
                reverse=True,
            )
        finally:
            client.close()

    def _container_stats_sync(self, container: Any) -> DockerContainerStats | None:
        try:
            raw = container.stats(stream=False)
        except DockerException:
            return None
        return calculate_container_stats(container.name, raw)

    async def restart_container(self, container_id: str) -> str:
        return await asyncio.to_thread(self._restart_container_sync, container_id)

    def _restart_container_sync(self, container_id: str) -> str:
        client = self._client()
        try:
            container = client.containers.get(container_id)
            name = container.name
            container.restart(timeout=20)
            return f"Restarted Docker container {name} at {datetime.now().isoformat(timespec='seconds')}"
        except NotFound as exc:
            raise RuntimeError("Container no longer exists") from exc
        finally:
            client.close()


def _image_name(container: Any) -> str:
    tags = getattr(container.image, "tags", None) or []
    if tags:
        return tags[0]
    return getattr(container.image, "short_id", "unknown")


def calculate_container_stats(name: str, raw: dict[str, Any]) -> DockerContainerStats:
    cpu_stats = raw.get("cpu_stats") or {}
    precpu_stats = raw.get("precpu_stats") or {}
    cpu_usage = (cpu_stats.get("cpu_usage") or {}).get("total_usage", 0)
    precpu_usage = (precpu_stats.get("cpu_usage") or {}).get("total_usage", 0)
    cpu_delta = cpu_usage - precpu_usage
    system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
    online_cpus = (
        cpu_stats.get("online_cpus")
        or len((cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or [])
        or 1
    )
    cpu_percent = 0.0
    if system_delta > 0 and cpu_delta >= 0:
        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0

    memory = raw.get("memory_stats") or {}
    usage = memory.get("usage", 0)
    # cgroup v2 counts reclaimable page cache; subtract it like `docker stats`.
    usage -= (memory.get("stats") or {}).get("inactive_file", 0)
    limit = memory.get("limit")

    networks = raw.get("networks") or {}
    rx = sum(entry.get("rx_bytes", 0) for entry in networks.values())
    tx = sum(entry.get("tx_bytes", 0) for entry in networks.values())
    return DockerContainerStats(
        name=name,
        cpu_percent=cpu_percent,
        memory_used=max(0, usage),
        memory_limit=limit if isinstance(limit, int) and limit > 0 else None,
        net_rx=rx,
        net_tx=tx,
    )
