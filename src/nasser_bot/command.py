from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined_output(self) -> str:
        output = self.stdout.strip()
        error = self.stderr.strip()
        if output and error:
            return f"{output}\n\nstderr:\n{error}"
        return output or error


class CommandRunner:
    def __init__(self, config: Config) -> None:
        self._config = config

    async def run(self, args: list[str], *, sudo: bool = False) -> CommandResult:
        full_args = list(args)
        if sudo and self._config.use_sudo:
            full_args = [self._config.sudo_bin, "-n", *full_args]
        return await asyncio.to_thread(self._run_sync, full_args)

    def _run_sync(self, args: list[str]) -> CommandResult:
        try:
            completed = subprocess.run(
                args,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._config.command_timeout_seconds,
            )
            return CommandResult(
                args=args,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                args=args,
                returncode=124,
                stdout=stdout,
                stderr=stderr or f"Command timed out after {self._config.command_timeout_seconds}s",
            )
        except FileNotFoundError as exc:
            return CommandResult(args=args, returncode=127, stdout="", stderr=str(exc))

