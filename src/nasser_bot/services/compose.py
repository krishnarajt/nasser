from __future__ import annotations

from nasser_bot.command import CommandResult, CommandRunner
from nasser_bot.config import Config
from nasser_bot.policy import ComposeProject


class ComposeService:
    def __init__(self, config: Config, runner: CommandRunner, projects: list[ComposeProject]) -> None:
        self._config = config
        self._runner = runner
        self._projects = projects

    def projects(self) -> list[ComposeProject]:
        return self._projects

    def project(self, name: str) -> ComposeProject | None:
        return next((project for project in self._projects if project.name == name), None)

    async def ps(self, project: ComposeProject) -> CommandResult:
        return await self._runner.run([*self._base_args(project), "ps"], sudo=project.sudo)

    async def logs(
        self,
        project: ComposeProject,
        service: str | None,
        lines: int,
    ) -> CommandResult:
        args = [*self._base_args(project), "logs", "--no-color", "--tail", str(lines)]
        if service:
            if not self._service_allowed(project, service):
                return self._blocked(project, service)
            args.append(service)
        return await self._runner.run(args, sudo=project.sudo)

    async def restart(self, project: ComposeProject, service: str | None) -> CommandResult:
        args = [*self._base_args(project), "restart"]
        if service:
            if not self._service_allowed(project, service):
                return self._blocked(project, service)
            args.append(service)
        return await self._runner.run(args, sudo=project.sudo)

    def _base_args(self, project: ComposeProject) -> list[str]:
        args = [self._config.docker_bin, "compose", "--project-directory", str(project.path)]
        for file in project.files:
            args.extend(["-f", str(file if file.is_absolute() else project.path / file)])
        return args

    def _service_allowed(self, project: ComposeProject, service: str) -> bool:
        return not project.services or service in project.services

    def _blocked(self, project: ComposeProject, service: str) -> CommandResult:
        return CommandResult(
            args=[],
            returncode=126,
            stdout="",
            stderr=f"{service!r} is not allowed for compose project {project.name!r}.",
        )

