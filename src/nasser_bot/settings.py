from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from nasser_bot.config import Config
from nasser_bot.policy import NetworkPolicy, Policy, SystemPolicy, Thresholds, UPSPolicy


LOG = logging.getLogger(__name__)

# Config fields that the Telegram settings menu may override at runtime.
CONFIG_OVERRIDE_KEYS = {
    "disk_devices",
    "log_units",
    "restartable_services",
    "alerts_enabled",
    "alert_interval_seconds",
    "alert_repeat_seconds",
    "log_tail_lines",
}


class SettingsStore:
    """Runtime settings changed from Telegram, persisted as JSON on disk.

    Values here override the env config and policy.yaml. A missing key means
    "no override". Writes are atomic; if the state path is not writable the
    store keeps working in memory and flags itself as not persisted.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.persisted = True
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
        except (OSError, ValueError):
            LOG.exception("Failed to load settings from %s", self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def unset(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._save()

    def toggle_flag(self, key: str, default: bool) -> bool:
        current = bool(self.get(key, default))
        self.set(key, not current)
        return not current

    def toggle_list_item(self, key: str, value: str, base: list[str]) -> list[str]:
        """Toggle membership of value in the override list for key.

        The override list starts from base (env/policy value) the first time
        it is edited, so turning one item off keeps the rest.
        """
        override = self.get(key)
        current = list(override) if isinstance(override, list) else list(base)
        if value in current:
            current.remove(value)
        else:
            current.append(value)
        self.set(key, current)
        return current

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self.persisted = True
        except OSError:
            LOG.exception("Failed to persist settings to %s", self._path)
            self.persisted = False


class RuntimeConfig:
    """Config view that lets the SettingsStore override selected fields live."""

    def __init__(self, base: Config, settings: SettingsStore) -> None:
        # Avoid __setattr__ recursion pitfalls by assigning via __dict__.
        self.__dict__["base"] = base
        self.__dict__["settings"] = settings

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.base, name)
        if name in CONFIG_OVERRIDE_KEYS:
            override = self.settings.get(name)
            if override is not None:
                return override
        return value

    def is_allowed(self, user_id: int | None, chat_id: int | None) -> bool:
        return self.base.is_allowed(user_id, chat_id)

    # Monitoring toggles have no env equivalent; they live only in settings.
    @property
    def monitor_docker(self) -> bool:
        return bool(self.settings.get("monitor_docker", True))

    @property
    def monitor_k3s(self) -> bool:
        return bool(self.settings.get("monitor_k3s", True))


class RuntimePolicy:
    """Policy view with live threshold/UPS/network overrides from settings."""

    def __init__(self, base: Policy, settings: SettingsStore) -> None:
        self.__dict__["base"] = base
        self.__dict__["settings"] = settings

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    @property
    def thresholds(self) -> Thresholds:
        overrides = self.settings.get("thresholds")
        if not isinstance(overrides, dict) or not overrides:
            return self.base.thresholds
        valid = {
            key: float(value)
            for key, value in overrides.items()
            if hasattr(self.base.thresholds, key) and isinstance(value, (int, float))
        }
        return replace(self.base.thresholds, **valid)

    @property
    def ups(self) -> UPSPolicy:
        enabled = self.settings.get("ups_enabled")
        if enabled is None:
            return self.base.ups
        return replace(self.base.ups, enabled=bool(enabled))

    @property
    def network(self) -> NetworkPolicy:
        ping = self.settings.get("ping_hosts")
        dns = self.settings.get("dns_hosts")
        if ping is None and dns is None:
            return self.base.network
        return replace(
            self.base.network,
            ping_hosts=list(ping) if ping is not None else self.base.network.ping_hosts,
            dns_hosts=list(dns) if dns is not None else self.base.network.dns_hosts,
        )

    @property
    def system(self) -> SystemPolicy:
        # When services are managed from Telegram, that list is the allowlist;
        # otherwise policy.yaml's restart_allowed applies.
        override = self.settings.get("restartable_services")
        if isinstance(override, list):
            return SystemPolicy(restart_allowed=set(override))
        return self.base.system
