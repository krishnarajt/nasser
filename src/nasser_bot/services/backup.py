from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nasser_bot.command import CommandResult, CommandRunner
from nasser_bot.config import Config
from nasser_bot.policy import BackupJob


@dataclass(frozen=True)
class BackupStatus:
    job: BackupJob
    configured: bool
    ok: bool
    detail: str
    last_success: datetime | None = None
    stale: bool = False


class BackupService:
    def __init__(self, config: Config, runner: CommandRunner, jobs: list[BackupJob]) -> None:
        self._config = config
        self._runner = runner
        self._jobs = jobs

    def jobs(self) -> list[BackupJob]:
        return self._jobs

    def job(self, name: str) -> BackupJob | None:
        return next((job for job in self._jobs if job.name == name), None)

    async def statuses(self) -> list[BackupStatus]:
        return [await self.status(job) for job in self._jobs]

    async def status(self, job: BackupJob) -> BackupStatus:
        file_status = self._status_file_status(job)
        command_status = await self._status_command_status(job)
        if file_status and command_status:
            return BackupStatus(
                job=job,
                configured=True,
                ok=file_status.ok and command_status.ok,
                detail=f"{file_status.detail}\n{command_status.detail}",
                last_success=file_status.last_success,
                stale=file_status.stale,
            )
        if file_status:
            return file_status
        if command_status:
            return command_status
        return BackupStatus(
            job=job,
            configured=bool(job.run_command),
            ok=False,
            detail="No status_command or status_file configured.",
        )

    async def run(self, job: BackupJob) -> CommandResult:
        if not job.run_command:
            return CommandResult(
                args=[],
                returncode=126,
                stdout="",
                stderr=f"Backup job {job.name!r} has no run_command.",
            )
        return await self._runner.run(job.run_command, sudo=job.sudo)

    async def logs(self, job: BackupJob, lines: int) -> CommandResult:
        if not job.log_unit:
            return CommandResult(
                args=[],
                returncode=126,
                stdout="",
                stderr=f"Backup job {job.name!r} has no log_unit.",
            )
        return await self._runner.run(
            [
                self._config.journalctl_bin,
                "-u",
                job.log_unit,
                "-n",
                str(lines),
                "--no-pager",
                "-o",
                "short-iso",
            ]
        )

    def _status_file_status(self, job: BackupJob) -> BackupStatus | None:
        if not job.status_file:
            return None
        if not job.status_file.exists():
            return BackupStatus(
                job=job,
                configured=True,
                ok=False,
                detail=f"Status file missing: {job.status_file}",
            )
        mtime = datetime.fromtimestamp(job.status_file.stat().st_mtime, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        stale_after = job.stale_after_hours
        stale = bool(stale_after and (now - mtime).total_seconds() > stale_after * 3600)
        return BackupStatus(
            job=job,
            configured=True,
            ok=not stale,
            detail=f"Last status file update: {mtime.isoformat(timespec='seconds')}",
            last_success=mtime,
            stale=stale,
        )

    async def _status_command_status(self, job: BackupJob) -> BackupStatus | None:
        if not job.status_command:
            return None
        result = await self._runner.run(job.status_command, sudo=job.sudo)
        output = result.combined_output or "(no output)"
        return BackupStatus(
            job=job,
            configured=True,
            ok=result.ok,
            detail=output.strip(),
        )

