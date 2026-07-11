from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from nasser_bot.command import CommandRunner, CommandResult
from nasser_bot.config import Config


SUPPORTED_FS_TYPES = {
    "ext2",
    "ext3",
    "ext4",
    "ntfs",
    "fuseblk",
    "exfat",
    "vfat",
    "xfs",
    "btrfs",
}

# Container/runtime bind mounts of already-listed filesystems; scanning them
# adds noise and can hit permission errors as the non-root bot user.
IGNORED_MOUNT_PREFIXES = (
    "/var/lib/kubelet/",
    "/var/lib/docker/",
    "/var/lib/containerd/",
    "/var/lib/rancher/",
    "/run/",
    "/snap/",
)


@dataclass(frozen=True)
class MountUsage:
    device: str
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


@dataclass(frozen=True)
class HostSnapshot:
    hostname: str
    platform: str
    os_name: str
    uptime_seconds: int
    boot_time: datetime
    load_average: tuple[float, float, float] | None
    cpu_percent: float
    memory_total: int
    memory_used: int
    memory_percent: float
    swap_total: int
    swap_used: int
    swap_percent: float


@dataclass(frozen=True)
class DiscoveredDisk:
    path: str
    stable_path: str
    model: str | None
    size: str | None


@dataclass(frozen=True)
class SystemdUnit:
    name: str
    active: str
    sub: str
    description: str


@dataclass(frozen=True)
class SensorReading:
    chip: str
    label: str
    current: float
    high: float | None
    critical: float | None


@dataclass(frozen=True)
class LoggedInUser:
    name: str
    terminal: str | None
    host: str | None
    started: datetime


@dataclass(frozen=True)
class ListeningPort:
    proto: str
    address: str
    port: int


@dataclass(frozen=True)
class UpdatesStatus:
    available: bool
    count: int
    packages: list[str]
    reboot_required: bool
    reboot_packages: list[str]
    error: str | None = None


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    active: str
    enabled: str


@dataclass(frozen=True)
class DiskSmart:
    device: str
    ok: bool
    health: str
    model: str | None
    serial: str | None
    temperature_celsius: int | float | None
    power_on_hours: int | None
    raw: dict[str, Any]
    error: str | None = None


class SystemService:
    def __init__(self, config: Config, runner: CommandRunner) -> None:
        self._config = config
        self._runner = runner
        self._updates_cache: UpdatesStatus | None = None
        self._updates_cached_at = 0.0

    async def snapshot(self) -> HostSnapshot:
        # cpu_percent(interval=0.1) sleeps; keep it off the event loop.
        return await asyncio.to_thread(self._snapshot_sync)

    def _snapshot_sync(self) -> HostSnapshot:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        boot_ts = psutil.boot_time()
        load_average = os.getloadavg() if hasattr(os, "getloadavg") else None
        return HostSnapshot(
            hostname=socket.gethostname(),
            platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
            os_name=_os_pretty_name(),
            uptime_seconds=max(0, int(datetime.now(tz=timezone.utc).timestamp() - boot_ts)),
            boot_time=datetime.fromtimestamp(boot_ts, tz=timezone.utc),
            load_average=load_average,
            cpu_percent=psutil.cpu_percent(interval=0.1),
            memory_total=memory.total,
            memory_used=memory.used,
            memory_percent=memory.percent,
            swap_total=swap.total,
            swap_used=swap.used,
            swap_percent=swap.percent,
        )

    async def mount_usage(self) -> list[MountUsage]:
        # Stat calls can stall on a sleeping or failing disk; keep them off the event loop.
        return await asyncio.to_thread(self._mount_usage_sync)

    def _mount_usage_sync(self) -> list[MountUsage]:
        mounts: list[MountUsage] = []
        seen_devices: set[str] = set()
        partitions = sorted(psutil.disk_partitions(all=False), key=lambda p: len(p.mountpoint))
        for partition in partitions:
            try:
                if partition.fstype.lower() not in SUPPORTED_FS_TYPES:
                    continue
                if partition.mountpoint.startswith(IGNORED_MOUNT_PREFIXES):
                    continue
                # Bind mounts (e.g. k8s local volumes) repeat a device already
                # listed at its shortest mountpoint.
                if partition.device in seen_devices:
                    continue
                usage = psutil.disk_usage(partition.mountpoint)
            except OSError:
                continue
            seen_devices.add(partition.device)
            mounts.append(
                MountUsage(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype,
                    total=usage.total,
                    used=usage.used,
                    free=usage.free,
                    percent=usage.percent,
                )
            )
        return sorted(mounts, key=lambda mount: mount.mountpoint)

    async def lsblk_json(self) -> CommandResult:
        return await self._runner.run(
            [
                self._config.lsblk_bin,
                "-J",
                "-o",
                "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL",
            ]
        )

    async def service_status(self, service: str) -> ServiceStatus:
        active = await self._runner.run([self._config.systemctl_bin, "is-active", service])
        enabled = await self._runner.run([self._config.systemctl_bin, "is-enabled", service])
        return ServiceStatus(
            name=service,
            active=active.stdout.strip() or active.stderr.strip() or "unknown",
            enabled=enabled.stdout.strip() or enabled.stderr.strip() or "unknown",
        )

    async def restart_service(self, service: str) -> CommandResult:
        if service not in self._config.restartable_services:
            return CommandResult(
                args=[self._config.systemctl_bin, "restart", service],
                returncode=126,
                stdout="",
                stderr=f"{service!r} is not in NASSER_RESTARTABLE_SERVICES",
            )
        return await self._runner.run([self._config.systemctl_bin, "restart", service], sudo=True)

    async def journal_tail(self, unit: str, lines: int | None = None) -> CommandResult:
        if unit not in self._config.log_units and unit not in self._config.restartable_services:
            return CommandResult(
                args=[self._config.journalctl_bin, "-u", unit],
                returncode=126,
                stdout="",
                stderr=f"{unit!r} is not in NASSER_LOG_UNITS",
            )
        tail_lines = str(lines or self._config.log_tail_lines)
        return await self._runner.run(
            [
                self._config.journalctl_bin,
                "-u",
                unit,
                "-n",
                tail_lines,
                "--no-pager",
                "-o",
                "short-iso",
            ]
        )

    async def smart(self, device: str) -> DiskSmart:
        if device not in self._config.disk_devices:
            return DiskSmart(
                device=device,
                ok=False,
                health="not configured",
                model=None,
                serial=None,
                temperature_celsius=None,
                power_on_hours=None,
                raw={},
                error=f"{device!r} is not in NASSER_DISK_DEVICES",
            )

        result = await self._runner.run([self._config.smartctl_bin, "-a", "-j", device], sudo=True)
        if not result.ok and not result.stdout.strip():
            error_message = _smart_error_message(result, device, self._config)
            return DiskSmart(
                device=device,
                ok=False,
                health="unavailable",
                model=None,
                serial=None,
                temperature_celsius=None,
                power_on_hours=None,
                raw={},
                error=error_message,
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return DiskSmart(
                device=device,
                ok=False,
                health="invalid smartctl output",
                model=None,
                serial=None,
                temperature_celsius=None,
                power_on_hours=None,
                raw={},
                error=str(exc),
            )

        health = _smart_health(payload)
        temperature = _smart_temperature(payload)
        power_on_hours = _smart_power_on_hours(payload)
        return DiskSmart(
            device=device,
            ok=health.lower() in {"passed", "ok"} and result.returncode in {0, 4, 64},
            health=health,
            model=payload.get("model_name") or payload.get("device", {}).get("model_name"),
            serial=payload.get("serial_number"),
            temperature_celsius=temperature,
            power_on_hours=power_on_hours,
            raw=payload,
            error=None if result.ok else result.stderr.strip() or None,
        )

    async def discover_disks(self) -> list[DiscoveredDisk]:
        result = await self._runner.run(
            [self._config.lsblk_bin, "-J", "-o", "NAME,PATH,TYPE,SIZE,MODEL,SERIAL"]
        )
        if not result.ok:
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        stable = await asyncio.to_thread(_stable_disk_paths)
        disks: list[DiscoveredDisk] = []
        for device in payload.get("blockdevices") or []:
            if device.get("type") != "disk":
                continue
            name = str(device.get("name") or "")
            if name.startswith(("loop", "ram", "zram")):
                continue
            path = str(device.get("path") or f"/dev/{name}")
            disks.append(
                DiscoveredDisk(
                    path=path,
                    stable_path=stable.get(os.path.realpath(path), path),
                    model=(device.get("model") or "").strip() or None,
                    size=device.get("size"),
                )
            )
        return sorted(disks, key=lambda disk: disk.path)

    async def list_service_units(self) -> list[SystemdUnit]:
        return await self._list_units("--type=service", "--all")

    async def failed_units(self) -> list[SystemdUnit]:
        return await self._list_units("--state=failed")

    async def _list_units(self, *filters: str) -> list[SystemdUnit]:
        base = [self._config.systemctl_bin, "list-units", *filters, "--no-pager"]
        result = await self._runner.run([*base, "--output=json"])
        units = _units_from_json(result.stdout) if result.ok else []
        if not units:
            result = await self._runner.run([*base, "--no-legend", "--plain"])
            units = _units_from_plain(result.stdout)
        return sorted(units, key=lambda unit: unit.name.lower())

    async def sensors(self) -> list[SensorReading]:
        return await asyncio.to_thread(_sensors_sync)

    async def pending_updates(self) -> UpdatesStatus:
        now = time.monotonic()
        if self._updates_cache is not None and now - self._updates_cached_at < 600:
            return self._updates_cache
        result = await self._runner.run(
            [self._config.apt_bin, "-s", "-o", "Debug::NoLocking=1", "upgrade"]
        )
        reboot_required, reboot_packages = await asyncio.to_thread(_reboot_required_sync)
        if not result.ok:
            status = UpdatesStatus(
                available=False,
                count=0,
                packages=[],
                reboot_required=reboot_required,
                reboot_packages=reboot_packages,
                error=(result.combined_output or "apt failed")[:300],
            )
        else:
            packages = [
                line.split()[1]
                for line in result.stdout.splitlines()
                if line.startswith("Inst ") and len(line.split()) > 1
            ]
            status = UpdatesStatus(
                available=True,
                count=len(packages),
                packages=packages,
                reboot_required=reboot_required,
                reboot_packages=reboot_packages,
            )
        self._updates_cache = status
        self._updates_cached_at = now
        return status

    async def logged_in_users(self) -> list[LoggedInUser]:
        return await asyncio.to_thread(_users_sync)

    async def listening_ports(self) -> list[ListeningPort]:
        result = await self._runner.run([self._config.ss_bin, "-tulnH"])
        ports: dict[tuple[str, str, int], ListeningPort] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            proto = parts[0]
            address, _, port_text = parts[4].rpartition(":")
            try:
                port = int(port_text)
            except ValueError:
                continue
            key = (proto, address, port)
            ports.setdefault(key, ListeningPort(proto=proto, address=address, port=port))
        return sorted(ports.values(), key=lambda item: (item.port, item.proto, item.address))

    async def raid_status(self) -> str | None:
        return await asyncio.to_thread(_raid_status_sync)


def _os_pretty_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


_BY_ID_PREFERENCE = ("ata-", "scsi-", "nvme-", "usb-", "mmc-")


def _stable_disk_paths() -> dict[str, str]:
    """Map real device paths (/dev/sda) to their preferred /dev/disk/by-id link."""
    base = Path("/dev/disk/by-id")
    if not base.is_dir():
        return {}
    best: dict[str, tuple[int, str]] = {}
    for link in sorted(base.iterdir()):
        if "-part" in link.name:
            continue
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        if link.name.startswith("wwn-"):
            rank = 90
        else:
            rank = next(
                (i for i, prefix in enumerate(_BY_ID_PREFERENCE) if link.name.startswith(prefix)),
                50,
            )
        current = best.get(target)
        if current is None or rank < current[0]:
            best[target] = (rank, str(link))
    return {target: path for target, (_, path) in best.items()}


def _units_from_json(stdout: str) -> list[SystemdUnit]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    units: list[SystemdUnit] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("unit") or "")
        if not name:
            continue
        units.append(
            SystemdUnit(
                name=name.removesuffix(".service"),
                active=str(item.get("active") or "unknown"),
                sub=str(item.get("sub") or ""),
                description=str(item.get("description") or ""),
            )
        )
    return units


def _units_from_plain(stdout: str) -> list[SystemdUnit]:
    units: list[SystemdUnit] = []
    for line in stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or "." not in parts[0]:
            continue
        units.append(
            SystemdUnit(
                name=parts[0].removesuffix(".service"),
                active=parts[2],
                sub=parts[3],
                description=parts[4] if len(parts) > 4 else "",
            )
        )
    return units


def _sensors_sync() -> list[SensorReading]:
    try:
        data = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return []
    readings: list[SensorReading] = []
    for chip, entries in data.items():
        for entry in entries:
            if entry.current is None:
                continue
            readings.append(
                SensorReading(
                    chip=chip,
                    label=entry.label or chip,
                    current=float(entry.current),
                    high=float(entry.high) if entry.high is not None else None,
                    critical=float(entry.critical) if entry.critical is not None else None,
                )
            )
    return readings


_CPU_SENSOR_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")


def cpu_temperature(readings: list[SensorReading]) -> float | None:
    for chip_prefix in _CPU_SENSOR_CHIPS:
        candidates = [
            reading.current for reading in readings if reading.chip.startswith(chip_prefix)
        ]
        if candidates:
            return max(candidates)
    return None


def _reboot_required_sync() -> tuple[bool, list[str]]:
    marker = Path("/run/reboot-required")
    if not marker.exists():
        return False, []
    packages: list[str] = []
    try:
        pkgs = Path("/run/reboot-required.pkgs")
        if pkgs.exists():
            packages = [line.strip() for line in pkgs.read_text().splitlines() if line.strip()]
    except OSError:
        pass
    return True, packages


def _users_sync() -> list[LoggedInUser]:
    users: list[LoggedInUser] = []
    for entry in psutil.users():
        users.append(
            LoggedInUser(
                name=entry.name,
                terminal=entry.terminal,
                host=entry.host or None,
                started=datetime.fromtimestamp(entry.started, tz=timezone.utc),
            )
        )
    return users


def _raid_status_sync() -> str | None:
    path = Path("/proc/mdstat")
    try:
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    has_array = any(line.startswith("md") for line in content.splitlines())
    return content.strip() if has_array else None


def _smart_error_message(result: CommandResult, device: str, config: Config) -> str:
    stderr = (result.stderr or result.stdout).strip()
    if not stderr:
        return result.combined_output

    lowered = stderr.lower()
    if any(token in lowered for token in ("password is required", "a password is required")):
        return (
            "passwordless sudo is required for smartctl. Install the sudoers template "
            f"from deploy/sudoers.d/nasser or add a matching rule for {config.smartctl_bin} "
            f"-a -j {device}."
        )
    if "not in sudoers" in lowered or "not allowed" in lowered:
        return (
            "sudo permission for smartctl is not configured. Install the sudoers template "
            f"from deploy/sudoers.d/nasser or add a matching rule for {config.smartctl_bin} "
            f"-a -j {device}."
        )
    return stderr


def _smart_health(payload: dict[str, Any]) -> str:
    status = payload.get("smart_status") or {}
    if status.get("passed") is True:
        return "passed"
    if status.get("passed") is False:
        return "failed"
    nvme = payload.get("nvme_smart_health_information_log") or {}
    if nvme.get("critical_warning") == 0:
        return "passed"
    return "unknown"


def _smart_temperature(payload: dict[str, Any]) -> int | float | None:
    temp = payload.get("temperature") or {}
    if isinstance(temp, dict):
        current = temp.get("current")
        if isinstance(current, int | float):
            return current

    nvme = payload.get("nvme_smart_health_information_log") or {}
    current = nvme.get("temperature")
    if isinstance(current, int | float):
        return current

    ata = payload.get("ata_smart_attributes", {}).get("table") or []
    for attr in ata:
        name = str(attr.get("name", "")).lower()
        if "temperature" not in name:
            continue
        raw = attr.get("raw", {})
        value = raw.get("value") if isinstance(raw, dict) else None
        # Some drives pack min/max into the raw value's high bits, producing
        # huge numbers; only trust values in a plausible Celsius range.
        if isinstance(value, int | float) and 0 < value < 150:
            return value
    return None


def _smart_power_on_hours(payload: dict[str, Any]) -> int | None:
    power_on = payload.get("power_on_time") or {}
    hours = power_on.get("hours") if isinstance(power_on, dict) else None
    if isinstance(hours, int):
        return hours

    ata = payload.get("ata_smart_attributes", {}).get("table") or []
    for attr in ata:
        name = str(attr.get("name", "")).lower()
        if name != "power_on_hours":
            continue
        raw = attr.get("raw", {})
        value = raw.get("value") if isinstance(raw, dict) else None
        if isinstance(value, int):
            return value
    return None

