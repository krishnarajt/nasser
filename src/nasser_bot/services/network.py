from __future__ import annotations

import asyncio
import json
import socket
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from nasser_bot.command import CommandRunner
from nasser_bot.config import Config
from nasser_bot.policy import NetworkPolicy, NetworkPortCheck


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    is_up: bool
    speed_mbps: int
    addresses: list[str]


@dataclass(frozen=True)
class NetworkCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class NetworkSnapshot:
    interfaces: list[NetworkInterface]
    public_ip: str | None
    checks: list[NetworkCheck]


@dataclass(frozen=True)
class TrafficPeriod:
    label: str
    rx: int
    tx: int


@dataclass(frozen=True)
class InterfaceTraffic:
    name: str
    days: list[TrafficPeriod]
    months: list[TrafficPeriod]
    total_rx: int
    total_tx: int


@dataclass(frozen=True)
class TrafficSummary:
    available: bool
    interfaces: list[InterfaceTraffic]
    error: str | None = None


class NetworkService:
    def __init__(self, config: Config, runner: CommandRunner, policy: Any) -> None:
        # policy exposes a live .network attribute (RuntimePolicy).
        self._config = config
        self._runner = runner
        self._policy = policy

    @property
    def _net(self) -> NetworkPolicy:
        return self._policy.network

    async def snapshot(self) -> NetworkSnapshot:
        interfaces, public_ip, pings, dns, ports = await asyncio.gather(
            asyncio.to_thread(self.interfaces),
            asyncio.to_thread(self._public_ip_sync),
            self._ping_checks(),
            asyncio.to_thread(self._dns_checks_sync),
            asyncio.to_thread(self._port_checks_sync),
        )
        return NetworkSnapshot(
            interfaces=interfaces,
            public_ip=public_ip,
            checks=[*pings, *dns, *ports],
        )

    def interfaces(self) -> list[NetworkInterface]:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        rows: list[NetworkInterface] = []
        for name, values in addresses.items():
            stat = stats.get(name)
            ips: list[str] = []
            for addr in values:
                if addr.family in {socket.AF_INET, socket.AF_INET6}:
                    ips.append(addr.address)
            rows.append(
                NetworkInterface(
                    name=name,
                    is_up=bool(stat.isup) if stat else False,
                    speed_mbps=int(stat.speed) if stat else 0,
                    addresses=ips,
                )
            )
        return sorted(rows, key=lambda item: item.name)

    async def _ping_checks(self) -> list[NetworkCheck]:
        checks = []
        for host in self._net.ping_hosts:
            result = await self._runner.run([self._config.ping_bin, "-c", "1", "-W", "2", host])
            detail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr.strip()
            checks.append(NetworkCheck(name=f"ping {host}", ok=result.ok, detail=detail or "no output"))
        return checks

    def _dns_checks_sync(self) -> list[NetworkCheck]:
        rows: list[NetworkCheck] = []
        for host in self._net.dns_hosts:
            try:
                resolved = socket.gethostbyname(host)
                rows.append(NetworkCheck(name=f"dns {host}", ok=True, detail=resolved))
            except OSError as exc:
                rows.append(NetworkCheck(name=f"dns {host}", ok=False, detail=str(exc)))
        return rows

    def _port_checks_sync(self) -> list[NetworkCheck]:
        rows: list[NetworkCheck] = []
        for check in self._net.port_checks:
            rows.append(self._port_check_sync(check))
        return rows

    def _port_check_sync(self, check: NetworkPortCheck) -> NetworkCheck:
        try:
            with socket.create_connection(
                (check.host, check.port),
                timeout=check.timeout_seconds,
            ):
                return NetworkCheck(
                    name=f"port {check.name}",
                    ok=True,
                    detail=f"{check.host}:{check.port} reachable",
                )
        except OSError as exc:
            return NetworkCheck(
                name=f"port {check.name}",
                ok=False,
                detail=f"{check.host}:{check.port} failed: {exc}",
            )

    def _public_ip_sync(self) -> str | None:
        if not self._net.public_ip_url:
            return None
        try:
            with urllib.request.urlopen(self._net.public_ip_url, timeout=5) as response:
                return response.read(128).decode("utf-8", errors="replace").strip()
        except OSError:
            return None

    async def default_gateway(self) -> str | None:
        return await asyncio.to_thread(_default_gateway_sync)

    async def traffic(self) -> TrafficSummary:
        result = await self._runner.run([self._config.vnstat_bin, "--json"])
        if not result.ok:
            if result.returncode == 127:
                error = "vnstat is not installed. Install it with: sudo apt install vnstat"
            else:
                output = result.combined_output
                error = output.splitlines()[0][:200] if output else "vnstat failed"
            return TrafficSummary(available=False, interfaces=[], error=error)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return TrafficSummary(available=False, interfaces=[], error="Could not parse vnstat output.")
        return TrafficSummary(available=True, interfaces=parse_vnstat_json(payload))


def _default_gateway_sync() -> str | None:
    try:
        for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "00000000" and fields[2] != "00000000":
                return socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little"))
    except (OSError, ValueError):
        return None
    return None


def parse_vnstat_json(payload: dict[str, Any]) -> list[InterfaceTraffic]:
    # vnstat 1.x JSON (jsonversion "1") reports KiB; 2.x reports bytes.
    scale = 1024 if str(payload.get("jsonversion")) == "1" else 1
    interfaces: list[InterfaceTraffic] = []
    for iface in payload.get("interfaces") or []:
        traffic = iface.get("traffic") or {}
        days = _traffic_periods(traffic.get("day") or traffic.get("days") or [], scale, monthly=False)
        months = _traffic_periods(
            traffic.get("month") or traffic.get("months") or [], scale, monthly=True
        )
        total = traffic.get("total") or {}
        interfaces.append(
            InterfaceTraffic(
                name=str(iface.get("name") or iface.get("id") or "unknown"),
                days=days,
                months=months,
                total_rx=int(total.get("rx", 0)) * scale,
                total_tx=int(total.get("tx", 0)) * scale,
            )
        )
    return interfaces


def _traffic_periods(entries: list[Any], scale: int, *, monthly: bool) -> list[TrafficPeriod]:
    periods: list[tuple[tuple[int, int, int], TrafficPeriod]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date = entry.get("date") or {}
        year = int(date.get("year", 0))
        month = int(date.get("month", 0))
        day = int(date.get("day", 0))
        label = f"{year:04d}-{month:02d}" if monthly else f"{year:04d}-{month:02d}-{day:02d}"
        periods.append(
            (
                (year, month, day),
                TrafficPeriod(
                    label=label,
                    rx=int(entry.get("rx", 0)) * scale,
                    tx=int(entry.get("tx", 0)) * scale,
                ),
            )
        )
    periods.sort(key=lambda item: item[0], reverse=True)
    return [period for _, period in periods]

