from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nasser_bot.command import CommandRunner
from nasser_bot.config import Config
from nasser_bot.policy import UPSPolicy


@dataclass(frozen=True)
class UPSSnapshot:
    available: bool
    target: str
    status: str
    battery_charge: float | None
    runtime_seconds: float | None
    details: dict[str, str]
    error: str | None = None


class UPSService:
    def __init__(self, config: Config, runner: CommandRunner, policy: Any) -> None:
        # policy exposes a live .ups attribute (RuntimePolicy).
        self._config = config
        self._runner = runner
        self._policy = policy

    @property
    def _ups(self) -> UPSPolicy:
        return self._policy.ups

    async def snapshot(self) -> UPSSnapshot:
        if not self._ups.enabled:
            return UPSSnapshot(
                available=False,
                target=self._ups.target,
                status="disabled",
                battery_charge=None,
                runtime_seconds=None,
                details={},
            )
        result = await self._runner.run([self._config.upsc_bin, self._ups.target])
        if not result.ok:
            return UPSSnapshot(
                available=False,
                target=self._ups.target,
                status="unavailable",
                battery_charge=None,
                runtime_seconds=None,
                details={},
                error=result.combined_output,
            )
        details = _parse_upsc(result.stdout)
        return UPSSnapshot(
            available=True,
            target=self._ups.target,
            status=details.get("ups.status", "unknown"),
            battery_charge=_float(details.get("battery.charge")),
            runtime_seconds=_float(details.get("battery.runtime")),
            details=details,
        )


def _parse_upsc(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        data[key.strip()] = value.strip()
    return data


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None

