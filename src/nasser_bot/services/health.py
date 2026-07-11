from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from nasser_bot.config import Config
from nasser_bot.policy import Policy
from nasser_bot.services.backup import BackupService
from nasser_bot.services.docker_service import DockerService
from nasser_bot.services.k8s_service import K8sService
from nasser_bot.services.network import NetworkService
from nasser_bot.services.system import SystemService, cpu_temperature
from nasser_bot.services.ups import UPSService


STATUS_ORDER = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}


@dataclass(frozen=True)
class HealthItem:
    key: str
    label: str
    status: str
    detail: str


@dataclass(frozen=True)
class HealthReport:
    generated_at: datetime
    items: list[HealthItem]

    @property
    def worst_status(self) -> str:
        if not self.items:
            return "unknown"
        return max(self.items, key=lambda item: STATUS_ORDER.get(item.status, 0)).status

    @property
    def problems(self) -> list[HealthItem]:
        return [item for item in self.items if item.status in {"warn", "crit"}]


class HealthService:
    def __init__(
        self,
        config: Config,
        policy: Policy,
        system: SystemService,
        docker: DockerService,
        k8s: K8sService,
        network: NetworkService,
        ups: UPSService,
        backups: BackupService,
    ) -> None:
        self._config = config
        self._policy = policy
        self._system = system
        self._docker = docker
        self._k8s = k8s
        self._network = network
        self._ups = ups
        self._backups = backups

    async def report(self) -> HealthReport:
        items: list[HealthItem] = []
        await self._host(items)
        await self._disks(items)
        await self._docker_health(items)
        await self._k8s_health(items)
        await self._services(items)
        await self._system_extras(items)
        await self._network_health(items)
        await self._ups_health(items)
        await self._backup_health(items)
        return HealthReport(generated_at=datetime.now(tz=timezone.utc), items=items)

    def _monitor_docker(self) -> bool:
        return bool(getattr(self._config, "monitor_docker", True))

    def _monitor_k3s(self) -> bool:
        return bool(getattr(self._config, "monitor_k3s", True))

    async def _host(self, items: list[HealthItem]) -> None:
        try:
            snapshot = await self._system.snapshot()
        except Exception as exc:
            items.append(HealthItem("host.snapshot", "Host", "crit", str(exc)))
            return

        thresholds = self._policy.thresholds
        cpu_count = os.cpu_count() or 1
        load1 = snapshot.load_average[0] if snapshot.load_average else 0.0
        load_per_cpu = load1 / cpu_count
        items.append(
            HealthItem(
                "host.cpu",
                "CPU",
                "warn" if snapshot.cpu_percent >= thresholds.cpu_warning_percent else "ok",
                f"{snapshot.cpu_percent:.1f}%",
            )
        )
        memory_status = "ok"
        if snapshot.memory_percent >= thresholds.memory_critical_percent:
            memory_status = "crit"
        elif snapshot.memory_percent >= thresholds.memory_warning_percent:
            memory_status = "warn"
        items.append(
            HealthItem(
                "host.memory",
                "Memory",
                memory_status,
                f"{snapshot.memory_percent:.1f}%",
            )
        )
        items.append(
            HealthItem(
                "host.load",
                "Load",
                "warn" if load_per_cpu >= thresholds.load1_warning_per_cpu else "ok",
                f"load1 {load1:.2f}, {load_per_cpu:.2f}/CPU",
            )
        )
        try:
            readings = await self._system.sensors()
        except Exception:
            readings = []
        temperature = cpu_temperature(readings)
        if temperature is not None:
            temp_status = "ok"
            if temperature >= thresholds.cpu_temp_critical_c:
                temp_status = "crit"
            elif temperature >= thresholds.cpu_temp_warning_c:
                temp_status = "warn"
            items.append(
                HealthItem("host.cpu_temp", "CPU temp", temp_status, f"{temperature:.0f} C")
            )

    async def _disks(self, items: list[HealthItem]) -> None:
        thresholds = self._policy.thresholds
        try:
            mounts = await self._system.mount_usage()
        except Exception as exc:
            items.append(HealthItem("disk.mounts", "Disk usage", "crit", str(exc)))
            mounts = []
        for mount in mounts:
            status = "ok"
            if mount.percent >= thresholds.disk_usage_critical_percent:
                status = "crit"
            elif mount.percent >= thresholds.disk_usage_warning_percent:
                status = "warn"
            items.append(
                HealthItem(
                    f"disk.usage.{mount.mountpoint}",
                    f"Disk {mount.mountpoint}",
                    status,
                    f"{mount.percent:.1f}% used on {mount.device}",
                )
            )

        for device in self._config.disk_devices:
            try:
                smart = await self._system.smart(device)
            except Exception as exc:
                items.append(HealthItem(f"disk.smart.{device}", f"SMART {device}", "crit", str(exc)))
                continue
            status = "ok"
            detail = smart.health
            if not smart.ok:
                status = "crit"
            if smart.temperature_celsius is not None:
                detail = f"{detail}, {smart.temperature_celsius} C"
                if smart.temperature_celsius >= thresholds.disk_temp_critical_c:
                    status = "crit"
                elif smart.temperature_celsius >= thresholds.disk_temp_warning_c and status == "ok":
                    status = "warn"
            items.append(HealthItem(f"disk.smart.{device}", f"SMART {device}", status, detail))

    async def _docker_health(self, items: list[HealthItem]) -> None:
        if not self._monitor_docker():
            return
        try:
            summary = await self._docker.summary()
        except Exception as exc:
            items.append(HealthItem("docker", "Docker", "crit", str(exc)))
            return
        if not summary.available:
            items.append(HealthItem("docker", "Docker", "crit", summary.error or "unavailable"))
            return
        detail = f"{summary.running} running, {summary.exited} exited, {summary.total} total"
        if summary.exited_names:
            names = ", ".join(summary.exited_names[:5])
            if len(summary.exited_names) > 5:
                names = f"{names}, +{len(summary.exited_names) - 5} more"
            detail = f"{detail} (exited: {names})"
        items.append(
            HealthItem(
                "docker.summary",
                "Docker",
                "warn" if summary.exited else "ok",
                detail,
            )
        )

    async def _k8s_health(self, items: list[HealthItem]) -> None:
        if not self._monitor_k3s():
            return
        try:
            summary = await self._k8s.summary()
        except Exception as exc:
            items.append(HealthItem("k3s", "k3s", "crit", str(exc)))
            return
        if not summary.available:
            items.append(HealthItem("k3s", "k3s", "crit", summary.error or "unavailable"))
            return
        not_ready = [node.name for node in summary.nodes if node.ready != "ready"]
        items.append(
            HealthItem(
                "k3s.nodes",
                "k3s nodes",
                "crit" if not_ready else "ok",
                ", ".join(not_ready) if not_ready else f"{len(summary.nodes)} ready",
            )
        )
        try:
            pods = await self._k8s.all_pods()
        except Exception as exc:
            items.append(HealthItem("k3s.pods", "k3s pods", "crit", str(exc)))
            return
        problem_pods = [pod for pod in pods if pod.issues]
        detail = ", ".join(f"{pod.namespace}/{pod.name}" for pod in problem_pods[:5])
        if len(problem_pods) > 5:
            detail = f"{detail}, +{len(problem_pods) - 5} more"
        items.append(
            HealthItem(
                "k3s.pods",
                "k3s pods",
                "warn" if problem_pods else "ok",
                detail or f"{len(pods)} pods checked",
            )
        )

    async def _services(self, items: list[HealthItem]) -> None:
        for service in self._config.restartable_services:
            # A deliberately disabled subsystem should not page about its unit.
            if service == self._config.k3s_service and not self._monitor_k3s():
                continue
            if service == self._config.docker_service and not self._monitor_docker():
                continue
            try:
                status = await self._system.service_status(service)
            except Exception as exc:
                items.append(HealthItem(f"service.{service}", service, "crit", str(exc)))
                continue
            items.append(
                HealthItem(
                    f"service.{service}",
                    f"Service {service}",
                    "ok" if status.active == "active" else "crit",
                    f"active={status.active}, enabled={status.enabled}",
                )
            )

    async def _system_extras(self, items: list[HealthItem]) -> None:
        try:
            failed = await self._system.failed_units()
        except Exception:
            failed = []
        if failed:
            names = ", ".join(unit.name for unit in failed[:5])
            if len(failed) > 5:
                names = f"{names}, +{len(failed) - 5} more"
            items.append(HealthItem("system.failed_units", "Failed units", "warn", names))
        else:
            items.append(HealthItem("system.failed_units", "Failed units", "ok", "none"))

        try:
            updates = await self._system.pending_updates()
        except Exception:
            return
        if updates.reboot_required:
            count = len(updates.reboot_packages)
            detail = f"{count} package(s) need a reboot" if count else "reboot required"
            items.append(HealthItem("system.reboot", "Reboot", "warn", detail))
        if updates.available:
            items.append(
                HealthItem(
                    "system.updates",
                    "Updates",
                    "ok",
                    f"{updates.count} package(s) upgradable",
                )
            )

    async def _network_health(self, items: list[HealthItem]) -> None:
        try:
            snapshot = await self._network.snapshot()
        except Exception as exc:
            items.append(HealthItem("network", "Network", "warn", str(exc)))
            return
        failed = [check for check in snapshot.checks if not check.ok]
        items.append(
            HealthItem(
                "network.checks",
                "Network checks",
                "warn" if failed else "ok",
                ", ".join(check.name for check in failed) if failed else "all checks passed",
            )
        )

    async def _ups_health(self, items: list[HealthItem]) -> None:
        try:
            ups = await self._ups.snapshot()
        except Exception as exc:
            items.append(HealthItem("ups", "UPS", "warn", str(exc)))
            return
        if not ups.available and ups.status == "disabled":
            items.append(HealthItem("ups", "UPS", "ok", "disabled"))
            return
        if not ups.available:
            items.append(HealthItem("ups", "UPS", "warn", ups.error or "unavailable"))
            return
        status = "ok"
        if "OB" in ups.status:
            status = "warn"
        if ups.battery_charge is not None and ups.battery_charge < 20:
            status = "crit"
        detail = f"status={ups.status}"
        if ups.battery_charge is not None:
            detail = f"{detail}, charge={ups.battery_charge:.0f}%"
        items.append(HealthItem("ups", "UPS", status, detail))

    async def _backup_health(self, items: list[HealthItem]) -> None:
        jobs = self._backups.jobs()
        if not jobs:
            items.append(HealthItem("backups", "Backups", "ok", "no jobs configured"))
            return
        for status in await self._backups.statuses():
            items.append(
                HealthItem(
                    f"backup.{status.job.name}",
                    f"Backup {status.job.name}",
                    "ok" if status.ok else "warn",
                    status.detail.splitlines()[0] if status.detail else "no detail",
                )
            )

