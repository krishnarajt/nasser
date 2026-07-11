from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _split_int_csv(value: str | None) -> set[int]:
    ids: set[int] = set()
    for item in _split_csv(value):
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"Expected integer ID in comma-separated list, got {item!r}") from exc
    return ids


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_ids: set[int]
    allowed_chat_ids: set[int]
    nas_name: str
    policy_path: Path
    state_path: Path
    kubeconfig: Path | None
    docker_base_url: str | None
    alert_chat_ids: set[int]
    alerts_enabled: bool
    alert_interval_seconds: int
    alert_repeat_seconds: int
    disk_devices: list[str]
    log_units: list[str]
    restartable_services: list[str]
    k3s_service: str
    docker_service: str
    log_tail_lines: int
    command_timeout_seconds: int
    use_sudo: bool
    sudo_bin: str
    systemctl_bin: str
    journalctl_bin: str
    smartctl_bin: str
    lsblk_bin: str
    docker_bin: str
    ping_bin: str
    upsc_bin: str
    vnstat_bin: str
    ss_bin: str
    apt_bin: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required")

        allowed_user_ids = _split_int_csv(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))
        if not allowed_user_ids:
            raise ConfigError("TELEGRAM_ALLOWED_USER_IDS is required")

        kubeconfig_raw = os.getenv("KUBECONFIG", "").strip()
        kubeconfig = Path(kubeconfig_raw) if kubeconfig_raw else None

        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed_user_ids,
            allowed_chat_ids=_split_int_csv(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")),
            nas_name=os.getenv("NASSER_NAME", "NAS").strip() or "NAS",
            policy_path=Path(os.getenv("NASSER_POLICY_PATH", "/etc/nasser/policy.yaml").strip()),
            state_path=Path(
                os.getenv("NASSER_STATE_PATH", "/var/lib/nasser/settings.json").strip()
            ),
            kubeconfig=kubeconfig,
            docker_base_url=os.getenv("DOCKER_HOST", "").strip() or None,
            alert_chat_ids=_split_int_csv(os.getenv("NASSER_ALERT_CHAT_IDS")),
            alerts_enabled=_env_bool("NASSER_ALERTS_ENABLED", True),
            alert_interval_seconds=_env_int("NASSER_ALERT_INTERVAL_SECONDS", 300),
            alert_repeat_seconds=_env_int("NASSER_ALERT_REPEAT_SECONDS", 3600),
            disk_devices=_split_csv(os.getenv("NASSER_DISK_DEVICES")),
            log_units=_split_csv(os.getenv("NASSER_LOG_UNITS")) or ["k3s", "docker", "containerd"],
            restartable_services=_split_csv(os.getenv("NASSER_RESTARTABLE_SERVICES"))
            or ["k3s", "docker"],
            k3s_service=os.getenv("NASSER_K3S_SERVICE", "k3s").strip() or "k3s",
            docker_service=os.getenv("NASSER_DOCKER_SERVICE", "docker").strip() or "docker",
            log_tail_lines=_env_int("NASSER_LOG_TAIL_LINES", 200),
            command_timeout_seconds=_env_int("NASSER_COMMAND_TIMEOUT_SECONDS", 20),
            use_sudo=_env_bool("NASSER_USE_SUDO", True),
            sudo_bin=os.getenv("NASSER_SUDO_BIN", "/usr/bin/sudo"),
            systemctl_bin=os.getenv("NASSER_SYSTEMCTL_BIN", "/bin/systemctl"),
            journalctl_bin=os.getenv("NASSER_JOURNALCTL_BIN", "/bin/journalctl"),
            smartctl_bin=os.getenv("NASSER_SMARTCTL_BIN", "/usr/sbin/smartctl"),
            lsblk_bin=os.getenv("NASSER_LSBLK_BIN", "/usr/bin/lsblk"),
            docker_bin=os.getenv("NASSER_DOCKER_BIN", "/usr/bin/docker"),
            ping_bin=os.getenv("NASSER_PING_BIN", "/bin/ping"),
            upsc_bin=os.getenv("NASSER_UPSC_BIN", "/usr/bin/upsc"),
            vnstat_bin=os.getenv("NASSER_VNSTAT_BIN", "/usr/bin/vnstat"),
            ss_bin=os.getenv("NASSER_SS_BIN", "/usr/bin/ss"),
            apt_bin=os.getenv("NASSER_APT_BIN", "/usr/bin/apt-get"),
        )

    def is_allowed(self, user_id: int | None, chat_id: int | None) -> bool:
        if user_id is None or user_id not in self.allowed_user_ids:
            return False
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            return False
        return True
