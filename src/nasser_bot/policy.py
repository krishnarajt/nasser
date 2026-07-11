from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Thresholds:
    disk_usage_warning_percent: float = 85.0
    disk_usage_critical_percent: float = 95.0
    disk_temp_warning_c: float = 45.0
    disk_temp_critical_c: float = 55.0
    memory_warning_percent: float = 90.0
    memory_critical_percent: float = 97.0
    cpu_warning_percent: float = 90.0
    cpu_temp_warning_c: float = 80.0
    cpu_temp_critical_c: float = 90.0
    load1_warning_per_cpu: float = 2.0
    backup_stale_warning_hours: float = 30.0


@dataclass(frozen=True)
class DockerRestartPolicy:
    allow_all: bool = True
    allowed_names: set[str] = field(default_factory=set)
    required_labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeProject:
    name: str
    path: Path
    files: list[Path] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    sudo: bool = False


@dataclass(frozen=True)
class DockerPolicy:
    restart: DockerRestartPolicy = field(default_factory=DockerRestartPolicy)
    compose_projects: list[ComposeProject] = field(default_factory=list)


@dataclass(frozen=True)
class SystemPolicy:
    restart_allowed: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class BackupJob:
    name: str
    run_command: list[str] = field(default_factory=list)
    status_command: list[str] = field(default_factory=list)
    log_unit: str | None = None
    status_file: Path | None = None
    stale_after_hours: float | None = None
    sudo: bool = False


@dataclass(frozen=True)
class NetworkPortCheck:
    name: str
    host: str
    port: int
    timeout_seconds: float = 3.0


@dataclass(frozen=True)
class NetworkPolicy:
    public_ip_url: str = "https://api.ipify.org"
    ping_hosts: list[str] = field(default_factory=lambda: ["1.1.1.1"])
    dns_hosts: list[str] = field(default_factory=lambda: ["debian.org"])
    port_checks: list[NetworkPortCheck] = field(default_factory=list)


@dataclass(frozen=True)
class UPSPolicy:
    enabled: bool = False
    target: str = "ups@localhost"


@dataclass(frozen=True)
class Policy:
    thresholds: Thresholds = field(default_factory=Thresholds)
    system: SystemPolicy = field(default_factory=SystemPolicy)
    docker: DockerPolicy = field(default_factory=DockerPolicy)
    backup_jobs: list[BackupJob] = field(default_factory=list)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    ups: UPSPolicy = field(default_factory=UPSPolicy)


def load_policy(path: Path) -> Policy:
    if not path.exists():
        return Policy()
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        return Policy()

    thresholds = _thresholds(raw.get("thresholds") or {})
    system = _system(raw.get("system") or {})
    docker = _docker(raw.get("docker") or {})
    backups = _backup_jobs(raw.get("backups") or {})
    network = _network(raw.get("network") or {})
    ups = _ups(raw.get("ups") or {})
    return Policy(
        thresholds=thresholds,
        system=system,
        docker=docker,
        backup_jobs=backups,
        network=network,
        ups=ups,
    )


def _thresholds(raw: dict[str, Any]) -> Thresholds:
    return Thresholds(
        disk_usage_warning_percent=_float(raw, "disk_usage_warning_percent", 85.0),
        disk_usage_critical_percent=_float(raw, "disk_usage_critical_percent", 95.0),
        disk_temp_warning_c=_float(raw, "disk_temp_warning_c", 45.0),
        disk_temp_critical_c=_float(raw, "disk_temp_critical_c", 55.0),
        memory_warning_percent=_float(raw, "memory_warning_percent", 90.0),
        memory_critical_percent=_float(raw, "memory_critical_percent", 97.0),
        cpu_warning_percent=_float(raw, "cpu_warning_percent", 90.0),
        cpu_temp_warning_c=_float(raw, "cpu_temp_warning_c", 80.0),
        cpu_temp_critical_c=_float(raw, "cpu_temp_critical_c", 90.0),
        load1_warning_per_cpu=_float(raw, "load1_warning_per_cpu", 2.0),
        backup_stale_warning_hours=_float(raw, "backup_stale_warning_hours", 30.0),
    )


def _docker(raw: dict[str, Any]) -> DockerPolicy:
    restart_raw = raw.get("restart") or {}
    restart = DockerRestartPolicy(
        allow_all=bool(restart_raw.get("allow_all", True)),
        allowed_names=set(_str_list(restart_raw.get("allowed_names"))),
        required_labels=_str_dict(restart_raw.get("required_labels") or {}),
    )
    projects: list[ComposeProject] = []
    for item in raw.get("compose_projects") or []:
        if not isinstance(item, dict) or not item.get("name") or not item.get("path"):
            continue
        projects.append(
            ComposeProject(
                name=str(item["name"]),
                path=Path(str(item["path"])).expanduser(),
                files=[Path(str(value)).expanduser() for value in _str_list(item.get("files"))],
                services=_str_list(item.get("services")),
                sudo=bool(item.get("sudo", False)),
            )
        )
    return DockerPolicy(restart=restart, compose_projects=projects)


def _system(raw: dict[str, Any]) -> SystemPolicy:
    return SystemPolicy(restart_allowed=set(_str_list(raw.get("restart_allowed"))))


def _backup_jobs(raw: dict[str, Any]) -> list[BackupJob]:
    jobs: list[BackupJob] = []
    for item in raw.get("jobs") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        status_file = item.get("status_file")
        jobs.append(
            BackupJob(
                name=str(item["name"]),
                run_command=_command(item.get("run_command")),
                status_command=_command(item.get("status_command")),
                log_unit=str(item["log_unit"]) if item.get("log_unit") else None,
                status_file=Path(str(status_file)).expanduser() if status_file else None,
                stale_after_hours=_optional_float(item.get("stale_after_hours")),
                sudo=bool(item.get("sudo", False)),
            )
        )
    return jobs


def _network(raw: dict[str, Any]) -> NetworkPolicy:
    ports: list[NetworkPortCheck] = []
    for item in raw.get("port_checks") or []:
        if not isinstance(item, dict) or not item.get("host") or not item.get("port"):
            continue
        ports.append(
            NetworkPortCheck(
                name=str(item.get("name") or f"{item['host']}:{item['port']}"),
                host=str(item["host"]),
                port=int(item["port"]),
                timeout_seconds=float(item.get("timeout_seconds", 3.0)),
            )
        )
    return NetworkPolicy(
        public_ip_url=str(raw.get("public_ip_url") or "https://api.ipify.org"),
        ping_hosts=_str_list(raw.get("ping_hosts")) or ["1.1.1.1"],
        dns_hosts=_str_list(raw.get("dns_hosts")) or ["debian.org"],
        port_checks=ports,
    )


def _ups(raw: dict[str, Any]) -> UPSPolicy:
    return UPSPolicy(
        enabled=bool(raw.get("enabled", False)),
        target=str(raw.get("target") or "ups@localhost"),
    )


def _float(raw: dict[str, Any], key: str, default: float) -> float:
    value = _optional_float(raw.get(key))
    return default if value is None else value


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _str_dict(value: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(val) for key, val in value.items()}


def _command(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return shlex.split(value)
    return []
