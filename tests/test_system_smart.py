import asyncio
import unittest
from pathlib import Path

from nasser_bot.command import CommandResult
from nasser_bot.config import Config
from nasser_bot.services.system import SystemService


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self._result = result
        self.calls: list[tuple[list[str], bool]] = []

    async def run(self, args: list[str], *, sudo: bool = False) -> CommandResult:
        self.calls.append((args, sudo))
        return self._result


class SmartServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(
            telegram_bot_token="token",
            allowed_user_ids={1},
            allowed_chat_ids=set(),
            nas_name="NAS",
            policy_path=Path("/tmp/policy.yaml"),
            state_path=Path("/tmp/state.json"),
            kubeconfig=None,
            docker_base_url=None,
            alert_chat_ids=set(),
            alerts_enabled=True,
            alert_interval_seconds=300,
            alert_repeat_seconds=3600,
            disk_devices=["/dev/disk/by-id/ata-test"],
            log_units=["docker"],
            restartable_services=["docker"],
            k3s_service="k3s",
            docker_service="docker",
            log_tail_lines=200,
            command_timeout_seconds=20,
            use_sudo=True,
            sudo_bin="/usr/bin/sudo",
            systemctl_bin="/bin/systemctl",
            journalctl_bin="/bin/journalctl",
            smartctl_bin="/usr/sbin/smartctl",
            lsblk_bin="/usr/bin/lsblk",
            docker_bin="/usr/bin/docker",
            ping_bin="/bin/ping",
            upsc_bin="/usr/bin/upsc",
            vnstat_bin="/usr/bin/vnstat",
            ss_bin="/usr/bin/ss",
            apt_bin="/usr/bin/apt-get",
        )

    def test_smart_reports_actionable_sudo_error(self) -> None:
        runner = FakeRunner(
            CommandResult(
                args=["/usr/sbin/smartctl", "-a", "-j", "/dev/disk/by-id/ata-test"],
                returncode=1,
                stdout="",
                stderr="sudo: a password is required\n",
            )
        )
        service = SystemService(self.config, runner)

        smart = asyncio.run(service.smart("/dev/disk/by-id/ata-test"))

        self.assertFalse(smart.ok)
        self.assertEqual(smart.health, "unavailable")
        self.assertIn("passwordless sudo", smart.error)
        self.assertIn("deploy/sudoers.d/nasser", smart.error)


if __name__ == "__main__":
    unittest.main()
