from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from docker.errors import NotFound as DockerNotFound
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from nasser_bot.command import CommandRunner
from nasser_bot.config import Config, ConfigError
from nasser_bot.formatting import bold, code, h, human_bytes, percent, truncate_text
from nasser_bot.policy import BackupJob, ComposeProject, DockerRestartPolicy, Policy, load_policy
from nasser_bot.services.alerts import STATUS_ICONS, AlertService, alert_loop, short_detail
from nasser_bot.services.backup import BackupService, BackupStatus
from nasser_bot.services.compose import ComposeService
from nasser_bot.services.docker_service import (
    DockerContainerDetail,
    DockerContainerStats,
    DockerContainerSummary,
    DockerService,
)
from nasser_bot.services.health import HealthReport, HealthService
from nasser_bot.services.k8s_service import (
    K8sDeploymentSummary,
    K8sEventSummary,
    K8sNodeMetrics,
    K8sPodMetrics,
    K8sPodSummary,
    K8sService,
    shorten_k8s_error,
)
from nasser_bot.services.network import NetworkService, NetworkSnapshot, TrafficSummary
from nasser_bot.services.system import (
    DiskSmart,
    HostSnapshot,
    ListeningPort,
    LoggedInUser,
    MountUsage,
    SensorReading,
    SystemdUnit,
    SystemService,
    UpdatesStatus,
)
from nasser_bot.services.ups import UPSSnapshot, UPSService
from nasser_bot.settings import RuntimeConfig, RuntimePolicy, SettingsStore
from nasser_bot.tokens import TokenStore


LOG = logging.getLogger(__name__)
PAGE_SIZE = 8
LOG_MESSAGE_LIMIT = 3300


@dataclass(frozen=True)
class ConfirmAction:
    kind: str
    label: str
    args: dict[str, str]
    back: str
    impact: str


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE, list[str]], Awaitable[None]]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    settings = SettingsStore(config.state_path)
    runtime_config = RuntimeConfig(config, settings)
    policy = RuntimePolicy(load_policy(config.policy_path), settings)
    runner = CommandRunner(runtime_config)
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .concurrent_updates(False)
        .post_init(_post_init)
        .build()
    )
    system = SystemService(runtime_config, runner)
    docker = DockerService(runtime_config)
    k8s = K8sService(runtime_config)
    network = NetworkService(runtime_config, runner, policy)
    ups = UPSService(runtime_config, runner, policy)
    backups = BackupService(runtime_config, runner, policy.backup_jobs)
    compose = ComposeService(runtime_config, runner, policy.docker.compose_projects)
    health = HealthService(runtime_config, policy, system, docker, k8s, network, ups, backups)

    application.bot_data["config"] = runtime_config
    application.bot_data["policy"] = policy
    application.bot_data["settings"] = settings
    application.bot_data["known_chat_ids"] = set()
    application.bot_data["tokens"] = TokenStore()
    application.bot_data["system"] = system
    application.bot_data["docker"] = docker
    application.bot_data["k8s"] = k8s
    application.bot_data["network"] = network
    application.bot_data["ups"] = ups
    application.bot_data["backups"] = backups
    application.bot_data["compose"] = compose
    application.bot_data["health"] = health
    application.bot_data["alerts"] = AlertService(config, health)

    application.add_handler(CommandHandler(["start", "menu"], show_main_menu))
    application.add_handler(CommandHandler("status", show_status))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("id", show_ids))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(on_error)

    LOG.info("Starting nasser bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("menu", "Open the NAS control menu"),
            BotCommand("status", "Show NAS health summary"),
            BotCommand("id", "Show your Telegram user/chat IDs"),
            BotCommand("help", "Show bot help"),
        ]
    )
    # The loop checks alerts_enabled each cycle so it can be toggled from
    # the Telegram settings menu without a restart.
    application.create_task(alert_loop(application))


def cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


def policy(context: ContextTypes.DEFAULT_TYPE) -> Policy:
    return context.application.bot_data["policy"]


def tokens(context: ContextTypes.DEFAULT_TYPE) -> TokenStore:
    return context.application.bot_data["tokens"]


def settings_store(context: ContextTypes.DEFAULT_TYPE) -> SettingsStore:
    return context.application.bot_data["settings"]


def system_service(context: ContextTypes.DEFAULT_TYPE) -> SystemService:
    return context.application.bot_data["system"]


def docker_service(context: ContextTypes.DEFAULT_TYPE) -> DockerService:
    return context.application.bot_data["docker"]


def k8s_service(context: ContextTypes.DEFAULT_TYPE) -> K8sService:
    return context.application.bot_data["k8s"]


def network_service(context: ContextTypes.DEFAULT_TYPE) -> NetworkService:
    return context.application.bot_data["network"]


def ups_service(context: ContextTypes.DEFAULT_TYPE) -> UPSService:
    return context.application.bot_data["ups"]


def backup_service(context: ContextTypes.DEFAULT_TYPE) -> BackupService:
    return context.application.bot_data["backups"]


def compose_service(context: ContextTypes.DEFAULT_TYPE) -> ComposeService:
    return context.application.bot_data["compose"]


def health_service(context: ContextTypes.DEFAULT_TYPE) -> HealthService:
    return context.application.bot_data["health"]


async def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if cfg(context).is_allowed(user.id if user else None, chat.id if chat else None):
        if chat:
            context.application.bot_data.setdefault("known_chat_ids", set()).add(chat.id)
        return True

    if update.callback_query:
        await update.callback_query.answer("Not authorized", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("Not authorized.")
    return False


async def show_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    lines = [
        bold("Telegram IDs"),
        "",
        f"User ID: {code(user.id if user else 'unknown')}",
        f"Chat ID: {code(chat.id if chat else 'unknown')}",
    ]
    if update.effective_message:
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized(update, context):
        return
    await send_screen(
        update,
        context,
        "\n".join(
            [
                bold("Nasser help"),
                "",
                "Use /menu to open the control panel.",
                "All restarts require a confirmation button.",
                "The bot only exposes configured actions; it does not run arbitrary commands.",
            ]
        ),
        [[button("Main menu", "menu")]],
    )


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized(update, context):
        return
    report = await health_service(context).report()
    text = format_health_report(report)
    traffic = await network_service(context).traffic()
    brief = format_traffic_brief(traffic)
    if brief:
        text = f"{text}\n\n{brief}"
    await send_screen(update, context, text, health_keyboard())


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized(update, context):
        return
    snapshot = await system_service(context).snapshot()
    text = "\n".join(
        [
            bold(cfg(context).nas_name),
            f"Host: {code(snapshot.hostname)}",
            f"Uptime: {h(format_duration(snapshot.uptime_seconds))}",
            "",
            "Choose a section.",
        ]
    )
    keyboard = [
        [button("Health", "health"), button("Network", "network")],
        [button("NAS", "nas"), button("Disks", "disks")],
        [button("Docker", "docker"), button("k3s", "k3s")],
        [button("Compose", "compose"), button("Backups", "backups")],
        [button("UPS", "ups"), button("Logs", "logs")],
        [button("Services", "services"), button("Settings", "set")],
        [button("Refresh", "menu")],
    ]
    await send_screen(update, context, text, keyboard)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is None:
        return
    if not await is_authorized(update, context):
        return

    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    head = parts[0]

    routes: dict[str, Handler] = {
        "menu": _route_menu,
        "health": _route_health,
        "nas": _route_nas,
        "disks": _route_disks,
        "disk": _route_disk,
        "docker": _route_docker,
        "compose": _route_compose,
        "k3s": _route_k3s,
        "network": _route_network,
        "ups": _route_ups,
        "backups": _route_backups,
        "backup": _route_backup,
        "logs": _route_logs,
        "log": _route_log,
        "services": _route_services,
        "svc": _route_service_action,
        "confirm": _route_confirm,
        "set": _route_settings,
    }
    handler = routes.get(head)
    if handler is None:
        await send_screen(update, context, "Unknown action.", [[button("Main menu", "menu")]])
        return
    await handler(update, context, parts[1:])


async def _route_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    await show_main_menu(update, context)


async def _route_health(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    report = await health_service(context).report()
    await send_screen(update, context, format_health_report(report), health_keyboard())


async def _route_nas(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    action = parts[0] if parts else ""
    back_row = [button("NAS", "nas"), button("Main menu", "menu")]

    if action == "sensors":
        readings = await system_service(context).sensors()
        await send_screen(update, context, format_sensors(readings), [back_row])
        return
    if action == "ports":
        ports = await system_service(context).listening_ports()
        await send_screen(update, context, format_ports(ports), [back_row])
        return
    if action == "updates":
        updates = await system_service(context).pending_updates()
        await send_screen(update, context, format_updates(updates), [back_row])
        return
    if action == "users":
        users = await system_service(context).logged_in_users()
        await send_screen(update, context, format_users(users), [back_row])
        return
    if action == "failed":
        failed = await system_service(context).failed_units()
        await send_screen(update, context, format_failed_units(failed), [back_row])
        return
    if action == "raid":
        status = await system_service(context).raid_status()
        text = f"{bold('RAID (mdstat)')}\n\n"
        text += f"<pre>{h(status)}</pre>" if status else "No mdadm RAID arrays found."
        await send_screen(update, context, text, [back_row])
        return

    snapshot = await system_service(context).snapshot()
    await send_screen(update, context, format_host_snapshot(snapshot), nas_keyboard())


async def _route_disks(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    mounts = await system_service(context).mount_usage()
    await send_screen(update, context, format_mounts(mounts, cfg(context).disk_devices), disks_keyboard(context))


async def _route_disk(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if not parts:
        await _route_disks(update, context, [])
        return
    action = parts[0]
    if action == "lsblk":
        result = await system_service(context).lsblk_json()
        await send_log(update, context, "lsblk", result.combined_output or "(no output)", back="disks")
        return
    if action == "smart" and len(parts) >= 2:
        device = token_payload(context, parts[1])
        if device is None:
            await expired(update, context)
            return
        smart = await system_service(context).smart(str(device))
        await send_screen(update, context, format_smart(smart), smart_keyboard())
        return
    await _route_disks(update, context, [])


async def _route_docker(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if not parts:
        summary = await docker_service(context).summary()
        await send_screen(update, context, format_docker_summary(summary), docker_keyboard(context))
        return

    action = parts[0]
    if action == "list":
        status_filter = parts[1] if len(parts) > 1 else "all"
        page = safe_int(parts[2], 0) if len(parts) > 2 else 0
        await show_docker_list(update, context, status_filter, page)
        return

    if action == "stats":
        chat = update.effective_chat
        if chat:
            await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        stats = await docker_service(context).stats()
        keyboard = [
            [button("Refresh", "docker:stats")],
            [button("Docker", "docker"), button("Main menu", "menu")],
        ]
        await send_screen(update, context, format_docker_stats(stats), keyboard)
        return

    if action == "container" and len(parts) >= 2:
        container_id = token_payload(context, parts[1])
        if container_id is None:
            await expired(update, context)
            return
        try:
            await show_docker_container(update, context, str(container_id))
        except DockerNotFound:
            await container_gone(update, context)
        return

    if action == "logs" and len(parts) >= 2:
        container_id = token_payload(context, parts[1])
        if container_id is None:
            await expired(update, context)
            return
        try:
            detail = await docker_service(context).container_detail(str(container_id))
            logs = await docker_service(context).logs(str(container_id), cfg(context).log_tail_lines)
        except DockerNotFound:
            await container_gone(update, context)
            return
        await send_log(
            update,
            context,
            f"Docker logs: {detail.name}",
            logs or "(no output)",
            back=f"docker:container:{tokens(context).put(str(container_id))}",
        )
        return

    if action == "restart" and len(parts) >= 2:
        container_id = token_payload(context, parts[1])
        if container_id is None:
            await expired(update, context)
            return
        try:
            detail = await docker_service(context).container_detail(str(container_id))
        except DockerNotFound:
            await container_gone(update, context)
            return
        allowed, reason = docker_restart_allowed(detail, policy(context).docker.restart)
        if not allowed:
            await send_screen(
                update,
                context,
                f"{bold('Blocked by policy')}\n\n{h(reason)}",
                [[button("Docker", "docker"), button("Main menu", "menu")]],
            )
            return
        confirm = ConfirmAction(
            kind="docker_restart_container",
            label=f"Restart Docker container {detail.name}",
            args={"container_id": str(container_id)},
            back=f"docker:container:{tokens(context).put(str(container_id))}",
            impact="The container will stop and start again. Any active sessions may disconnect.",
        )
        await show_confirm(update, context, confirm)
        return

    await _route_docker(update, context, [])


async def show_docker_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_filter: str,
    page: int,
) -> None:
    containers = await docker_service(context).list_containers(status_filter)
    page_items, page, total_pages = paginate(containers, page)
    title = f"Docker containers: {status_filter}"
    if not containers:
        text = f"{bold(title)}\n\nNo containers found."
    else:
        text = "\n".join(
            [
                bold(title),
                f"Page {page + 1} of {total_pages}",
                "",
                *[format_container_line(item) for item in page_items],
            ]
        )

    rows: list[list[InlineKeyboardButton]] = []
    for item in page_items:
        token = tokens(context).put(item.id)
        rows.append([button(short_button(f"{item.name} · {item.status}"), f"docker:container:{token}")])
    rows.extend(pager_rows(f"docker:list:{status_filter}", page, total_pages))
    rows.append([button("Docker", "docker"), button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


async def show_docker_container(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container_id: str,
) -> None:
    detail = await docker_service(context).container_detail(container_id)
    token = tokens(context).put(container_id)
    keyboard = [
        [button("Logs", f"docker:logs:{token}"), button("Restart", f"docker:restart:{token}")],
        [button("Docker", "docker"), button("Main menu", "menu")],
    ]
    await send_screen(update, context, format_container_detail(detail), keyboard)


async def _route_k3s(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    try:
        await _route_k3s_inner(update, context, parts)
    except TelegramError:
        raise
    except Exception as exc:
        LOG.warning("k3s action failed: %s", exc)
        retry = "k3s" if not parts else "k3s:" + ":".join(parts)
        await send_screen(
            update,
            context,
            f"{bold('k3s unavailable')}\n\n{h(shorten_k8s_error(exc))}",
            [[button("Retry", retry), button("Main menu", "menu")]],
        )


async def _route_k3s_inner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    if not parts:
        summary = await k8s_service(context).summary()
        await send_screen(update, context, format_k8s_summary(summary), k3s_keyboard(context))
        return

    action = parts[0]
    if action == "top":
        pods = await k8s_service(context).top_pods()
        nodes = await k8s_service(context).top_nodes()
        keyboard = [
            [button("Refresh", "k3s:top"), button("By namespace", "k3s:topns")],
            [button("k3s", "k3s"), button("Main menu", "menu")],
        ]
        await send_screen(update, context, format_k8s_top(pods, nodes), keyboard)
        return

    if action == "topns":
        pods = await k8s_service(context).top_pods()
        keyboard = [
            [button("Refresh", "k3s:topns"), button("Top pods", "k3s:top")],
            [button("k3s", "k3s"), button("Main menu", "menu")],
        ]
        await send_screen(update, context, format_k8s_top_namespaces(pods), keyboard)
        return

    if action == "nodes":
        nodes = await k8s_service(context).nodes()
        await send_screen(update, context, format_k8s_nodes(nodes), [[button("k3s", "k3s")]])
        return

    if action == "events":
        mode = parts[1] if len(parts) > 1 else "warn"
        events = await k8s_service(context).events(warnings_only=mode != "all", limit=30)
        await send_screen(update, context, format_k8s_events(events, mode), k3s_events_keyboard(mode))
        return

    if action == "ns" and len(parts) >= 2:
        await show_namespaces(update, context, parts[1])
        return

    if action == "deploys" and len(parts) >= 2:
        namespace = token_payload(context, parts[1])
        if namespace is None:
            await expired(update, context)
            return
        page = safe_int(parts[2], 0) if len(parts) > 2 else 0
        await show_deployments(update, context, str(namespace), page)
        return

    if action == "dep" and len(parts) >= 2:
        payload = token_payload(context, parts[1])
        if payload is None:
            await expired(update, context)
            return
        namespace, deployment = payload["namespace"], payload["deployment"]
        deployments = await k8s_service(context).deployments(namespace)
        match = next((item for item in deployments if item.name == deployment), None)
        if match is None:
            await send_screen(update, context, "Deployment not found.", [[button("k3s", "k3s")]])
            return
        await show_deployment(update, context, match)
        return

    if action == "restart-dep" and len(parts) >= 2:
        payload = token_payload(context, parts[1])
        if payload is None:
            await expired(update, context)
            return
        confirm = ConfirmAction(
            kind="k8s_rollout_restart_deployment",
            label=f"Rollout restart deployment {payload['namespace']}/{payload['deployment']}",
            args={"namespace": payload["namespace"], "deployment": payload["deployment"]},
            back="k3s",
            impact="Kubernetes will gradually replace pods managed by this deployment.",
        )
        await show_confirm(update, context, confirm)
        return

    if action == "pods" and len(parts) >= 2:
        namespace = token_payload(context, parts[1])
        if namespace is None:
            await expired(update, context)
            return
        page = safe_int(parts[2], 0) if len(parts) > 2 else 0
        await show_pods(update, context, str(namespace), page)
        return

    if action == "pod" and len(parts) >= 2:
        payload = token_payload(context, parts[1])
        if payload is None:
            await expired(update, context)
            return
        pods = await k8s_service(context).pods(payload["namespace"])
        match = next((item for item in pods if item.name == payload["pod"]), None)
        if match is None:
            await send_screen(update, context, "Pod not found.", [[button("k3s", "k3s")]])
            return
        await show_pod(update, context, match)
        return

    if action == "podlogs" and len(parts) >= 2:
        payload = token_payload(context, parts[1])
        if payload is None:
            await expired(update, context)
            return
        logs = await k8s_service(context).pod_logs(
            payload["namespace"],
            payload["pod"],
            payload.get("container"),
            cfg(context).log_tail_lines,
        )
        title = f"Pod logs: {payload['namespace']}/{payload['pod']}"
        if payload.get("container"):
            title = f"{title} [{payload['container']}]"
        await send_log(
            update,
            context,
            title,
            logs or "(no output)",
            back=f"k3s:pod:{tokens(context).put({'namespace': payload['namespace'], 'pod': payload['pod']})}",
        )
        return

    await _route_k3s(update, context, [])


async def show_namespaces(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    view: str,
) -> None:
    namespaces = await k8s_service(context).namespaces()
    rows: list[list[InlineKeyboardButton]] = []
    for namespace in namespaces:
        token = tokens(context).put(namespace)
        target = "deploys" if view == "deploys" else "pods"
        rows.append([button(namespace, f"k3s:{target}:{token}:0")])
    rows.append([button("k3s", "k3s"), button("Main menu", "menu")])
    await send_screen(
        update,
        context,
        f"{bold('Namespaces')}\n\nChoose a namespace for {h(view)}.",
        rows,
    )


async def show_deployments(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    namespace: str,
    page: int,
) -> None:
    deployments = await k8s_service(context).deployments(namespace)
    page_items, page, total_pages = paginate(deployments, page)
    title = f"Deployments in {namespace}"
    text = f"{bold(title)}\n\nNo deployments found."
    if deployments:
        text = "\n".join(
            [
                bold(title),
                f"Page {page + 1} of {total_pages}",
                "",
                *[format_deployment_line(item) for item in page_items],
            ]
        )
    ns_token = tokens(context).put(namespace)
    rows: list[list[InlineKeyboardButton]] = []
    for deployment in page_items:
        token = tokens(context).put({"namespace": namespace, "deployment": deployment.name})
        rows.append([button(short_button(deployment.name), f"k3s:dep:{token}")])
    rows.extend(pager_rows(f"k3s:deploys:{ns_token}", page, total_pages))
    rows.append([button("Namespaces", "k3s:ns:deploys"), button("k3s", "k3s")])
    await send_screen(update, context, text, rows)


async def show_deployment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deployment: K8sDeploymentSummary,
) -> None:
    token = tokens(context).put({"namespace": deployment.namespace, "deployment": deployment.name})
    text = "\n".join(
        [
            bold(f"Deployment {deployment.namespace}/{deployment.name}"),
            "",
            f"Desired: {code(deployment.desired)}",
            f"Ready: {code(deployment.ready)}",
            f"Available: {code(deployment.available)}",
            f"Updated: {code(deployment.updated)}",
        ]
    )
    rows = [
        [button("Rollout restart", f"k3s:restart-dep:{token}")],
        [button("Deployments", f"k3s:deploys:{tokens(context).put(deployment.namespace)}:0")],
        [button("k3s", "k3s"), button("Main menu", "menu")],
    ]
    await send_screen(update, context, text, rows)


async def show_pods(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    namespace: str,
    page: int,
) -> None:
    pods = await k8s_service(context).pods(namespace)
    page_items, page, total_pages = paginate(pods, page)
    title = f"Pods in {namespace}"
    text = f"{bold(title)}\n\nNo pods found."
    if pods:
        text = "\n".join(
            [
                bold(title),
                f"Page {page + 1} of {total_pages}",
                "",
                *[format_pod_line(item) for item in page_items],
            ]
        )
    ns_token = tokens(context).put(namespace)
    rows: list[list[InlineKeyboardButton]] = []
    for pod in page_items:
        token = tokens(context).put({"namespace": namespace, "pod": pod.name})
        rows.append([button(short_button(f"{pod.name} · {pod.phase}"), f"k3s:pod:{token}")])
    rows.extend(pager_rows(f"k3s:pods:{ns_token}", page, total_pages))
    rows.append([button("Namespaces", "k3s:ns:pods"), button("k3s", "k3s")])
    await send_screen(update, context, text, rows)


async def show_pod(update: Update, context: ContextTypes.DEFAULT_TYPE, pod: K8sPodSummary) -> None:
    text = "\n".join(
        [
            bold(f"Pod {pod.namespace}/{pod.name}"),
            "",
            f"Phase: {code(pod.phase)}",
            f"Node: {code(pod.node or 'unknown')}",
            f"Restarts: {code(pod.restarts)}",
            f"Containers: {code(', '.join(pod.containers) or 'none')}",
            f"Issues: {code(', '.join(pod.issues) or 'none')}",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    if len(pod.containers) <= 1:
        rows.append(
            [
                button(
                    "Logs",
                    f"k3s:podlogs:{tokens(context).put({'namespace': pod.namespace, 'pod': pod.name, 'container': pod.containers[0] if pod.containers else None})}",
                )
            ]
        )
    else:
        for container in pod.containers:
            token = tokens(context).put(
                {"namespace": pod.namespace, "pod": pod.name, "container": container}
            )
            rows.append([button(short_button(f"Logs: {container}"), f"k3s:podlogs:{token}")])
    rows.append([button("Pods", f"k3s:pods:{tokens(context).put(pod.namespace)}:0")])
    rows.append([button("k3s", "k3s"), button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


async def _route_network(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if parts and parts[0] == "traffic":
        summary = await network_service(context).traffic()
        keyboard = [
            [button("Refresh", "network:traffic")],
            [button("Network", "network"), button("Main menu", "menu")],
        ]
        await send_screen(update, context, format_traffic(summary), keyboard)
        return
    snapshot = await network_service(context).snapshot()
    await send_screen(update, context, format_network_snapshot(snapshot), network_keyboard())


async def _route_ups(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    snapshot = await ups_service(context).snapshot()
    await send_screen(update, context, format_ups_snapshot(snapshot), ups_keyboard())


async def _route_backups(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    statuses = await backup_service(context).statuses()
    await send_screen(update, context, format_backup_statuses(statuses), backups_keyboard(context, statuses))


async def _route_backup(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if len(parts) < 2:
        await _route_backups(update, context, [])
        return
    action = parts[0]
    payload = token_payload(context, parts[1])
    if payload is None:
        await expired(update, context)
        return
    job = backup_service(context).job(str(payload))
    if job is None:
        await send_screen(update, context, "Backup job not found.", [[button("Backups", "backups")]])
        return

    if action == "job":
        status = await backup_service(context).status(job)
        await send_screen(update, context, format_backup_status(status), backup_job_keyboard(context, job))
        return

    if action == "logs":
        result = await backup_service(context).logs(job, cfg(context).log_tail_lines)
        await send_log(
            update,
            context,
            f"Backup logs: {job.name}",
            result.combined_output or "(no output)",
            back=f"backup:job:{tokens(context).put(job.name)}",
        )
        return

    if action == "run":
        confirm = ConfirmAction(
            kind="backup_run",
            label=f"Run backup job {job.name}",
            args={"job": job.name},
            back=f"backup:job:{tokens(context).put(job.name)}",
            impact="The configured backup command will run on the NAS.",
        )
        await show_confirm(update, context, confirm)
        return

    await _route_backups(update, context, [])


async def _route_compose(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if not parts:
        projects = compose_service(context).projects()
        await send_screen(update, context, format_compose_projects(projects), compose_keyboard(context, projects))
        return

    action = parts[0]
    if action == "project" and len(parts) >= 2:
        name = token_payload(context, parts[1])
        if name is None:
            await expired(update, context)
            return
        project = compose_service(context).project(str(name))
        if project is None:
            await send_screen(update, context, "Compose project not found.", [[button("Compose", "compose")]])
            return
        result = await compose_service(context).ps(project)
        await send_screen(
            update,
            context,
            format_compose_project(project, result.combined_output or "(no output)"),
            compose_project_keyboard(context, project),
        )
        return

    if action == "ps" and len(parts) >= 2:
        name = token_payload(context, parts[1])
        project = compose_service(context).project(str(name)) if name is not None else None
        if project is None:
            await expired(update, context)
            return
        result = await compose_service(context).ps(project)
        await send_log(
            update,
            context,
            f"Compose ps: {project.name}",
            result.combined_output or "(no output)",
            back=f"compose:project:{tokens(context).put(project.name)}",
        )
        return

    if action in {"logs", "restart"} and len(parts) >= 2:
        payload = token_payload(context, parts[1])
        if payload is None:
            await expired(update, context)
            return
        project = compose_service(context).project(payload["project"])
        if project is None:
            await send_screen(update, context, "Compose project not found.", [[button("Compose", "compose")]])
            return
        service = payload.get("service") or None
        if action == "logs":
            result = await compose_service(context).logs(project, service, cfg(context).log_tail_lines)
            title = f"Compose logs: {project.name}" + (f"/{service}" if service else "")
            await send_log(
                update,
                context,
                title,
                result.combined_output or "(no output)",
                back=f"compose:project:{tokens(context).put(project.name)}",
            )
            return
        label = f"Restart compose {project.name}" + (f"/{service}" if service else "")
        confirm = ConfirmAction(
            kind="compose_restart",
            label=label,
            args={"project": project.name, "service": service or ""},
            back=f"compose:project:{tokens(context).put(project.name)}",
            impact="Docker Compose will restart the selected service or project.",
        )
        await show_confirm(update, context, confirm)
        return

    await _route_compose(update, context, [])


async def _route_logs(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    rows: list[list[InlineKeyboardButton]] = []
    for unit in cfg(context).log_units:
        token = tokens(context).put(unit)
        rows.append([button(unit, f"log:unit:{token}")])
    rows.append([button("Main menu", "menu")])
    await send_screen(update, context, f"{bold('Logs')}\n\nChoose a journal unit.", rows)


async def _route_log(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if len(parts) >= 2 and parts[0] == "unit":
        unit = token_payload(context, parts[1])
        if unit is None:
            await expired(update, context)
            return
        result = await system_service(context).journal_tail(str(unit))
        await send_log(
            update,
            context,
            f"journalctl -u {unit}",
            result.combined_output or "(no output)",
            back="logs",
        )
        return
    await _route_logs(update, context, [])


async def _route_services(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    statuses = [
        await system_service(context).service_status(service)
        for service in cfg(context).restartable_services
    ]
    text = "\n".join(
        [
            bold("Services"),
            "",
            *[
                f"{code(status.name)} active={code(status.active)} enabled={code(status.enabled)}"
                for status in statuses
            ],
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    for service in cfg(context).restartable_services:
        if not service_restart_allowed(context, service):
            continue
        token = tokens(context).put(service)
        rows.append([button(f"Restart {service}", f"svc:restart:{token}")])
    rows.append([button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


async def _route_service_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    if len(parts) >= 2 and parts[0] == "restart":
        service = token_payload(context, parts[1])
        if service is None:
            await expired(update, context)
            return
        if not service_restart_allowed(context, str(service)):
            await send_screen(
                update,
                context,
                f"{bold('Blocked by policy')}\n\n{code(service)} is not allowed for restart.",
                [[button("Services", "services"), button("Main menu", "menu")]],
            )
            return
        confirm = ConfirmAction(
            kind="system_restart_service",
            label=f"Restart service {service}",
            args={"service": str(service)},
            back="services",
            impact="This may interrupt Docker containers, k3s workloads, or NAS services depending on the unit.",
        )
        await show_confirm(update, context, confirm)
        return
    await _route_services(update, context, [])


async def show_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: ConfirmAction,
) -> None:
    token = tokens(context).put(action)
    text = "\n".join(
        [
            bold("Confirm action"),
            "",
            code(action.label),
            "",
            h(action.impact),
        ]
    )
    rows = [[button("Confirm", f"confirm:{token}"), button("Cancel", action.back)]]
    await send_screen(update, context, text, rows)


async def _route_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    if not parts:
        await expired(update, context)
        return
    # Pop instead of get so a confirm button can only fire once.
    payload = tokens(context).pop(parts[0])
    if payload is None or not isinstance(payload, ConfirmAction):
        await expired(update, context)
        return

    user = update.effective_user
    chat = update.effective_chat
    LOG.warning(
        "Confirmed action requested user_id=%s chat_id=%s kind=%s label=%s args=%s",
        user.id if user else None,
        chat.id if chat else None,
        payload.kind,
        payload.label,
        payload.args,
    )
    await send_screen(
        update,
        context,
        f"{bold('Running action')}\n\n{code(payload.label)}",
        [[button("Main menu", "menu")]],
    )
    try:
        result = await execute_confirmed_action(context, payload)
    except Exception as exc:
        LOG.exception("Confirmed action failed")
        result = f"Failed: {exc}"
    else:
        LOG.warning("Confirmed action completed kind=%s label=%s result=%s", payload.kind, payload.label, result)
    await send_screen(
        update,
        context,
        f"{bold('Action result')}\n\n{h(result)}",
        [[button("Back", payload.back), button("Main menu", "menu")]],
    )


async def execute_confirmed_action(
    context: ContextTypes.DEFAULT_TYPE,
    action: ConfirmAction,
) -> str:
    if action.kind == "docker_restart_container":
        detail = await docker_service(context).container_detail(action.args["container_id"])
        allowed, reason = docker_restart_allowed(detail, policy(context).docker.restart)
        if not allowed:
            return f"Blocked by policy: {reason}"
        return await docker_service(context).restart_container(action.args["container_id"])
    if action.kind == "k8s_rollout_restart_deployment":
        return await k8s_service(context).rollout_restart_deployment(
            action.args["namespace"],
            action.args["deployment"],
        )
    if action.kind == "compose_restart":
        project = compose_service(context).project(action.args["project"])
        if project is None:
            return "Compose project no longer exists."
        result = await compose_service(context).restart(project, action.args.get("service") or None)
        if result.ok:
            target = project.name + (f"/{action.args['service']}" if action.args.get("service") else "")
            return f"Restarted compose {target}"
        return result.combined_output or f"docker compose exited with {result.returncode}"
    if action.kind == "backup_run":
        job = backup_service(context).job(action.args["job"])
        if job is None:
            return "Backup job no longer exists."
        result = await backup_service(context).run(job)
        if result.ok:
            return f"Backup job {job.name} completed.\n{result.combined_output}".strip()
        return result.combined_output or f"backup exited with {result.returncode}"
    if action.kind == "system_restart_service":
        if not service_restart_allowed(context, action.args["service"]):
            return f"Blocked by policy: {action.args['service']} is not allowed for restart."
        result = await system_service(context).restart_service(action.args["service"])
        if result.ok:
            return f"Restarted service {action.args['service']}"
        return result.combined_output or f"systemctl exited with {result.returncode}"
    raise RuntimeError(f"Unknown action kind {action.kind!r}")


ALERT_INTERVAL_PRESETS = [60, 300, 900, 1800, 3600]
ALERT_REPEAT_PRESETS = [1800, 3600, 10800, 21600, 43200, 86400]
PING_PRESET_HOSTS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
DNS_PRESET_HOSTS = ["debian.org", "github.com", "cloudflare.com"]
UNIT_KIND_KEYS = {"logs": "log_units", "svcs": "restartable_services"}
THRESHOLD_FIELDS = [
    ("disk_usage_warning_percent", "Disk use warn %", 5.0),
    ("disk_usage_critical_percent", "Disk use crit %", 5.0),
    ("disk_temp_warning_c", "Disk temp warn C", 5.0),
    ("disk_temp_critical_c", "Disk temp crit C", 5.0),
    ("memory_warning_percent", "Memory warn %", 5.0),
    ("memory_critical_percent", "Memory crit %", 5.0),
    ("cpu_warning_percent", "CPU warn %", 5.0),
    ("cpu_temp_warning_c", "CPU temp warn C", 5.0),
    ("cpu_temp_critical_c", "CPU temp crit C", 5.0),
    ("load1_warning_per_cpu", "Load1 per CPU", 0.5),
    ("backup_stale_warning_hours", "Backup stale h", 6.0),
]


async def _route_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    section = parts[0] if parts else ""
    rest = parts[1:]
    if section == "alerts":
        await _settings_alerts(update, context, rest)
    elif section == "mon":
        await _settings_monitoring(update, context, rest)
    elif section == "disks":
        await _settings_disks(update, context, rest)
    elif section == "units":
        await _settings_units(update, context, rest)
    elif section == "thr":
        await _settings_thresholds(update, context, rest)
    elif section == "net":
        await _settings_network(update, context, rest)
    else:
        await show_settings_home(update, context)


async def show_settings_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = cfg(context)
    store = settings_store(context)
    pol = policy(context)
    persistence = (
        f"Saved to {code(str(store.path))}"
        if store.persisted
        else "⚠️ Settings are NOT persisted (state path not writable). See the README."
    )
    lines = [
        f"⚙️ {bold('Settings')}",
        "",
        f"Alerts: {code(_onoff(config.alerts_enabled))} · every {code(_dur_label(int(config.alert_interval_seconds)))} · repeat {code(_dur_label(int(config.alert_repeat_seconds)))}",
        f"Monitoring: docker {code(_onoff(config.monitor_docker))} · k3s {code(_onoff(config.monitor_k3s))} · ups {code(_onoff(pol.ups.enabled))}",
        f"SMART disks: {code(', '.join(config.disk_devices) or 'none')}",
        f"Log units: {code(', '.join(config.log_units) or 'none')}",
        f"Restartable: {code(', '.join(config.restartable_services) or 'none')}",
        "",
        persistence,
    ]
    keyboard = [
        [button("Alerts", "set:alerts"), button("Monitoring", "set:mon")],
        [button("SMART disks", "set:disks"), button("Thresholds", "set:thr")],
        [button("Log units", "set:units:logs:0"), button("Restart services", "set:units:svcs:0")],
        [button("Network checks", "set:net")],
        [button("Main menu", "menu")],
    ]
    await send_screen(update, context, "\n".join(lines), keyboard)


async def _settings_alerts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    store = settings_store(context)
    if parts:
        action = parts[0]
        if action == "toggle":
            store.set("alerts_enabled", not cfg(context).alerts_enabled)
        elif action == "int" and len(parts) >= 2:
            store.set("alert_interval_seconds", max(60, safe_int(parts[1], 300)))
        elif action == "rep" and len(parts) >= 2:
            store.set("alert_repeat_seconds", max(300, safe_int(parts[1], 3600)))
    config = cfg(context)
    enabled = config.alerts_enabled
    interval = int(config.alert_interval_seconds)
    repeat = int(config.alert_repeat_seconds)
    text = "\n".join(
        [
            bold("Alert settings"),
            "",
            f"Alerts: {code(_onoff(enabled))}",
            f"Check interval: {code(_dur_label(interval))}",
            f"Repeat unresolved after: {code(_dur_label(repeat))}",
            "",
            "Alerts fire when a problem appears, changes status, or recovers.",
            "Unresolved problems are re-sent after the repeat interval.",
            "⏱ sets the check interval, 🔁 sets the repeat interval.",
        ]
    )
    rows = [[button(f"Alerts: {_onoff(enabled)} (tap to toggle)", "set:alerts:toggle")]]
    rows.extend(
        _preset_rows("set:alerts:int", "⏱", ALERT_INTERVAL_PRESETS, interval)
    )
    rows.extend(_preset_rows("set:alerts:rep", "🔁", ALERT_REPEAT_PRESETS, repeat))
    rows.append([button("Settings", "set"), button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


def _preset_rows(
    prefix: str,
    icon: str,
    presets: list[int],
    current: int,
    per_row: int = 3,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value in presets:
        marker = "• " if value == current else ""
        row.append(button(f"{marker}{icon} {_dur_label(value)}", f"{prefix}:{value}"))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


async def _settings_monitoring(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    store = settings_store(context)
    if parts:
        target = parts[0]
        if target == "docker":
            store.toggle_flag("monitor_docker", True)
        elif target == "k3s":
            store.toggle_flag("monitor_k3s", True)
        elif target == "ups":
            store.set("ups_enabled", not policy(context).ups.enabled)
    config = cfg(context)
    ups_on = policy(context).ups.enabled
    text = "\n".join(
        [
            bold("Monitoring"),
            "",
            "Turn a subsystem off to stop health checks and alerts for it,",
            "for example when k3s is intentionally stopped.",
            "The menu buttons keep working either way.",
        ]
    )
    rows = [
        [button(f"Docker monitoring: {_onoff(config.monitor_docker)}", "set:mon:docker")],
        [button(f"k3s monitoring: {_onoff(config.monitor_k3s)}", "set:mon:k3s")],
        [button(f"UPS monitoring: {_onoff(ups_on)}", "set:mon:ups")],
        [button("Settings", "set"), button("Main menu", "menu")],
    ]
    await send_screen(update, context, text, rows)


async def _settings_disks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    store = settings_store(context)
    if len(parts) >= 2 and parts[0] == "t":
        device = token_payload(context, parts[1])
        if device is None:
            await expired(update, context)
            return
        store.toggle_list_item("disk_devices", str(device), list(cfg(context).disk_devices))

    disks = await system_service(context).discover_disks()
    configured = list(cfg(context).disk_devices)
    configured_real = {os.path.realpath(entry) for entry in configured}
    rows: list[list[InlineKeyboardButton]] = []
    detected_real: set[str] = set()
    for disk in disks:
        real = os.path.realpath(disk.path)
        detected_real.add(real)
        selected = real in configured_real
        # Toggle the alias already configured for this disk, else the stable path.
        value = next(
            (entry for entry in configured if os.path.realpath(entry) == real),
            disk.stable_path,
        )
        label = f"{'✅' if selected else '⬜'} {disk.path}"
        if disk.model:
            label = f"{label} · {disk.model}"
        if disk.size:
            label = f"{label} · {disk.size}"
        rows.append([button(label, f"set:disks:t:{tokens(context).put(value)}")])
    for entry in configured:
        if os.path.realpath(entry) not in detected_real:
            rows.append(
                [button(f"❌ {entry} (remove)", f"set:disks:t:{tokens(context).put(entry)}")]
            )
    rows.append([button("Settings", "set"), button("Main menu", "menu")])
    text = "\n".join(
        [
            bold("SMART disks"),
            "",
            "Select the disks to monitor for SMART health and temperature.",
            "Stable /dev/disk/by-id paths are stored, so this survives reboots.",
            "❌ marks configured entries that no longer exist.",
        ]
    )
    await send_screen(update, context, text, rows)


async def _settings_units(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    kind = parts[0] if parts and parts[0] in UNIT_KIND_KEYS else "logs"
    key = UNIT_KIND_KEYS[kind]
    store = settings_store(context)
    page = 0
    rest = parts[1:]
    if len(rest) >= 2 and rest[0] == "t":
        name = token_payload(context, rest[1])
        if name is None:
            await expired(update, context)
            return
        store.toggle_list_item(key, str(name), list(getattr(cfg(context), key)))
        page = safe_int(rest[2], 0) if len(rest) > 2 else 0
    elif rest:
        page = safe_int(rest[0], 0)

    units = await system_service(context).list_service_units()
    selected = set(getattr(cfg(context), key))
    known = {unit.name for unit in units}
    entries = [
        SystemdUnit(name=name, active="?", sub="", description="")
        for name in sorted(selected - known)
    ]
    entries.extend(units)
    entries.sort(key=lambda unit: (unit.name not in selected, unit.name.lower()))
    page_items, page, total_pages = paginate(entries, page, page_size=10)

    if kind == "logs":
        title = "Journal log units"
        note = "Selected units appear in the Logs menu and can be tailed."
    else:
        title = "Restartable services"
        note = (
            "Selected units get restart buttons. Restarting via sudo also needs "
            "a matching sudoers rule; see the README."
        )
    text = "\n".join(
        [bold(title), f"Page {page + 1} of {total_pages}", "", note, "Selected units sort first."]
    )
    rows: list[list[InlineKeyboardButton]] = []
    for unit in page_items:
        mark = "✅" if unit.name in selected else "⬜"
        label = f"{mark} {unit.name}"
        if unit.active not in {"?", ""}:
            label = f"{label} · {unit.active}"
        token = tokens(context).put(unit.name)
        rows.append([button(label, f"set:units:{kind}:t:{token}:{page}")])
    rows.extend(pager_rows(f"set:units:{kind}", page, total_pages))
    rows.append([button("Settings", "set"), button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


async def _settings_thresholds(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    store = settings_store(context)
    fields = {key: (label, step) for key, label, step in THRESHOLD_FIELDS}
    if len(parts) >= 2 and parts[0] in fields and parts[1] in {"+", "-"}:
        key = parts[0]
        step = fields[key][1]
        current = float(getattr(policy(context).thresholds, key))
        value = max(0.0, current + step if parts[1] == "+" else current - step)
        overrides = dict(store.get("thresholds") or {})
        overrides[key] = value
        store.set("thresholds", overrides)

    thresholds = policy(context).thresholds
    text = "\n".join(
        [
            bold("Thresholds"),
            "",
            "Use − and ＋ to adjust. Changes apply to health checks and alerts immediately.",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    for key, label, _ in THRESHOLD_FIELDS:
        value = getattr(thresholds, key)
        rows.append(
            [
                button("−", f"set:thr:{key}:-"),
                button(f"{label}: {value:g}", "set:thr"),
                button("＋", f"set:thr:{key}:+"),
            ]
        )
    rows.append([button("Settings", "set"), button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


async def _settings_network(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    store = settings_store(context)
    if len(parts) >= 2 and parts[0] in {"ping", "dns"}:
        host = token_payload(context, parts[1])
        if host is None:
            await expired(update, context)
            return
        key = "ping_hosts" if parts[0] == "ping" else "dns_hosts"
        net = policy(context).network
        base = list(net.ping_hosts if key == "ping_hosts" else net.dns_hosts)
        store.toggle_list_item(key, str(host), base)

    net = policy(context).network
    gateway = await network_service(context).default_gateway()
    ping_options = _merged_options(
        [*PING_PRESET_HOSTS, *( [gateway] if gateway else [] )], net.ping_hosts
    )
    dns_options = _merged_options(DNS_PRESET_HOSTS, net.dns_hosts)
    text = "\n".join(
        [
            bold("Network checks"),
            "",
            "Toggle the hosts used for the ping and DNS health checks.",
            f"Detected gateway: {code(gateway or 'unknown')}",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    for host in ping_options:
        mark = "✅" if host in net.ping_hosts else "⬜"
        rows.append([button(f"{mark} ping {host}", f"set:net:ping:{tokens(context).put(host)}")])
    for host in dns_options:
        mark = "✅" if host in net.dns_hosts else "⬜"
        rows.append([button(f"{mark} dns {host}", f"set:net:dns:{tokens(context).put(host)}")])
    rows.append([button("Settings", "set"), button("Main menu", "menu")])
    await send_screen(update, context, text, rows)


def _merged_options(presets: list[str], current: list[str]) -> list[str]:
    merged: list[str] = []
    for host in [*current, *presets]:
        if host not in merged:
            merged.append(host)
    return merged


def _onoff(value: bool) -> str:
    return "on ✅" if value else "off ⛔"


def _dur_label(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


async def send_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: list[list[InlineKeyboardButton]],
) -> None:
    markup = InlineKeyboardMarkup(keyboard)
    text = truncate_text(text)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return
            raise
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def send_log(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    body: str,
    *,
    back: str = "logs",
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    body = body.strip() or "(no output)"
    if len(body) <= LOG_MESSAGE_LIMIT:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"{bold(title)}\n\n<pre>{h(body)}</pre>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        payload = io.BytesIO(body.encode("utf-8", errors="replace"))
        payload.name = safe_filename(f"{title}.log")
        await context.bot.send_document(
            chat_id=chat.id,
            document=payload,
            filename=payload.name,
            caption=title,
        )
    await send_screen(
        update,
        context,
        f"{bold('Logs sent')}\n\n{h(title)}",
        [[button("Back", back), button("Main menu", "menu")]],
    )


async def expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_screen(
        update,
        context,
        "That button expired. Open the menu again.",
        [[button("Main menu", "menu")]],
    )


async def container_gone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_screen(
        update,
        context,
        "Container not found. It may have been removed or recreated.",
        [[button("Docker", "docker"), button("Main menu", "menu")]],
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOG.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Something failed while handling that action. Check the service logs.",
        )


def button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(short_button(text), callback_data=callback_data)


def health_keyboard() -> list[list[InlineKeyboardButton]]:
    return [
        [button("Refresh", "health"), button("Network", "network")],
        [button("Docker", "docker"), button("k3s", "k3s")],
        [button("Backups", "backups"), button("Disks", "disks")],
        [button("Main menu", "menu")],
    ]


def nas_keyboard() -> list[list[InlineKeyboardButton]]:
    return [
        [button("Refresh", "nas"), button("Disks", "disks")],
        [button("Sensors", "nas:sensors"), button("Ports", "nas:ports")],
        [button("Updates", "nas:updates"), button("Users", "nas:users")],
        [button("Failed units", "nas:failed"), button("RAID", "nas:raid")],
        [button("Logs", "logs"), button("Main menu", "menu")],
    ]


def disks_keyboard(context: ContextTypes.DEFAULT_TYPE) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for device in cfg(context).disk_devices:
        rows.append([button(f"SMART {device}", f"disk:smart:{tokens(context).put(device)}")])
    rows.append([button("lsblk", "disk:lsblk"), button("Refresh", "disks")])
    rows.append([button("NAS", "nas"), button("Main menu", "menu")])
    return rows


def smart_keyboard() -> list[list[InlineKeyboardButton]]:
    return [[button("Disks", "disks"), button("Main menu", "menu")]]


def docker_keyboard(context: ContextTypes.DEFAULT_TYPE) -> list[list[InlineKeyboardButton]]:
    service_token = tokens(context).put(cfg(context).docker_service)
    return [
        [button("Running", "docker:list:running:0"), button("All", "docker:list:all:0")],
        [button("Exited", "docker:list:exited:0"), button("Paused", "docker:list:paused:0")],
        [button("Stats (CPU/RAM/net)", "docker:stats")],
        [button(f"Restart {cfg(context).docker_service}", f"svc:restart:{service_token}")],
        [button("Main menu", "menu")],
    ]


def compose_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    projects: list[ComposeProject],
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for project in projects:
        rows.append([button(project.name, f"compose:project:{tokens(context).put(project.name)}")])
    rows.append([button("Main menu", "menu")])
    return rows


def compose_project_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    project: ComposeProject,
) -> list[list[InlineKeyboardButton]]:
    rows = [
        [
            button("ps", f"compose:ps:{tokens(context).put(project.name)}"),
            button(
                "Logs",
                f"compose:logs:{tokens(context).put({'project': project.name, 'service': ''})}",
            ),
        ],
        [
            button(
                "Restart project",
                f"compose:restart:{tokens(context).put({'project': project.name, 'service': ''})}",
            )
        ],
    ]
    for service in project.services:
        payload = {"project": project.name, "service": service}
        rows.append(
            [
                button(short_button(f"Logs {service}", 28), f"compose:logs:{tokens(context).put(payload)}"),
                button(
                    short_button(f"Restart {service}", 28),
                    f"compose:restart:{tokens(context).put(payload)}",
                ),
            ]
        )
    rows.append([button("Compose", "compose"), button("Main menu", "menu")])
    return rows


def k3s_keyboard(context: ContextTypes.DEFAULT_TYPE) -> list[list[InlineKeyboardButton]]:
    service_token = tokens(context).put(cfg(context).k3s_service)
    return [
        [button("Nodes", "k3s:nodes"), button("Deployments", "k3s:ns:deploys")],
        [button("Pods", "k3s:ns:pods"), button("Events", "k3s:events:warn")],
        [button("Top (CPU/RAM)", "k3s:top")],
        [button(f"Restart {cfg(context).k3s_service}", f"svc:restart:{service_token}")],
        [button("Main menu", "menu")],
    ]


def network_keyboard() -> list[list[InlineKeyboardButton]]:
    return [
        [button("Refresh", "network"), button("Traffic", "network:traffic")],
        [button("Main menu", "menu")],
    ]


def k3s_events_keyboard(mode: str) -> list[list[InlineKeyboardButton]]:
    return [
        [button("Warnings", "k3s:events:warn"), button("All", "k3s:events:all")],
        [button("k3s", "k3s"), button("Main menu", "menu")],
    ]


def ups_keyboard() -> list[list[InlineKeyboardButton]]:
    return [[button("Refresh", "ups"), button("Main menu", "menu")]]


def backups_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    statuses: list[BackupStatus],
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for status in statuses:
        rows.append([button(status.job.name, f"backup:job:{tokens(context).put(status.job.name)}")])
    rows.append([button("Refresh", "backups"), button("Main menu", "menu")])
    return rows


def backup_job_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    job: BackupJob,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    actions: list[InlineKeyboardButton] = []
    if job.run_command:
        actions.append(button("Run now", f"backup:run:{tokens(context).put(job.name)}"))
    if job.log_unit:
        actions.append(button("Logs", f"backup:logs:{tokens(context).put(job.name)}"))
    if actions:
        rows.append(actions)
    rows.append([button("Backups", "backups"), button("Main menu", "menu")])
    return rows


def format_health_report(report: HealthReport) -> str:
    counts = {status: len([item for item in report.items if item.status == status]) for status in ["crit", "warn", "unknown", "ok"]}
    summary = f"🔴 {counts['crit']}  🟠 {counts['warn']}  🟢 {counts['ok']}"
    if counts["unknown"]:
        summary = f"{summary}  ⚪ {counts['unknown']}"
    lines = [
        f"{STATUS_ICONS.get(report.worst_status, '⚪')} {bold('Health')}",
        f"Generated: {code(report.generated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S'))}",
        "",
        summary,
        "",
    ]
    problems = report.problems
    if problems:
        lines.append(bold("Problems"))
        for item in problems[:18]:
            icon = STATUS_ICONS.get(item.status, "⚪")
            lines.append(f"{icon} {bold(item.label)}: {h(short_detail(item.detail))}")
        if len(problems) > 18:
            lines.append(f"+{len(problems) - 18} more")
    else:
        lines.append("All configured health checks are OK.")
    return "\n".join(lines)


def format_network_snapshot(snapshot: NetworkSnapshot) -> str:
    lines = [bold("Network"), ""]
    lines.append(f"Public IP: {code(snapshot.public_ip or 'unknown')}")
    lines.append("")
    lines.append(bold("Interfaces"))
    for iface in snapshot.interfaces:
        if iface.name == "lo":
            continue
        state = "up" if iface.is_up else "down"
        addrs = ", ".join(iface.addresses) or "no IP"
        speed = f", {iface.speed_mbps} Mbps" if iface.speed_mbps else ""
        lines.append(f"{code(iface.name)} {state}{speed}: {h(addrs)}")
    lines.append("")
    lines.append(bold("Checks"))
    if snapshot.checks:
        for check in snapshot.checks:
            lines.append(f"{code('OK' if check.ok else 'FAIL')} {h(check.name)}: {h(check.detail)}")
    else:
        lines.append("No network checks configured.")
    return "\n".join(lines)


def format_ups_snapshot(snapshot: UPSSnapshot) -> str:
    lines = [
        bold("UPS"),
        "",
        f"Target: {code(snapshot.target)}",
        f"Status: {code(snapshot.status)}",
    ]
    if snapshot.battery_charge is not None:
        lines.append(f"Battery: {code(f'{snapshot.battery_charge:.0f}%')}")
    if snapshot.runtime_seconds is not None:
        lines.append(f"Runtime: {code(format_duration(int(snapshot.runtime_seconds)))}")
    if snapshot.error:
        lines.extend(["", f"Error: {h(snapshot.error)}"])
    if snapshot.details:
        lines.extend(
            [
                "",
                f"Input voltage: {code(snapshot.details.get('input.voltage', 'unknown'))}",
                f"Load: {code(snapshot.details.get('ups.load', 'unknown'))}",
            ]
        )
    return "\n".join(lines)


def format_backup_statuses(statuses: list[BackupStatus]) -> str:
    lines = [bold("Backups"), ""]
    if not statuses:
        lines.append("No backup jobs configured in policy.yaml.")
        return "\n".join(lines)
    for status in statuses:
        state = "OK" if status.ok else "WARN"
        first_line = status.detail.splitlines()[0] if status.detail else "no detail"
        lines.append(f"{code(state)} {h(status.job.name)}: {h(first_line)}")
    return "\n".join(lines)


def format_backup_status(status: BackupStatus) -> str:
    lines = [
        bold(f"Backup {status.job.name}"),
        "",
        f"Status: {code('OK' if status.ok else 'WARN')}",
        f"Run command: {code('configured' if status.job.run_command else 'not configured')}",
        f"Status command: {code('configured' if status.job.status_command else 'not configured')}",
        f"Log unit: {code(status.job.log_unit or 'not configured')}",
    ]
    if status.last_success:
        lines.append(f"Last status file update: {code(status.last_success.isoformat(timespec='seconds'))}")
    lines.extend(["", h(status.detail)])
    return "\n".join(lines)


def format_compose_projects(projects: list[ComposeProject]) -> str:
    lines = [bold("Docker Compose"), ""]
    if not projects:
        lines.append("No compose projects configured in policy.yaml.")
        return "\n".join(lines)
    for project in projects:
        services = ", ".join(project.services) if project.services else "all services"
        lines.append(f"{code(project.name)} {h(project.path)}")
        lines.append(f"  {h(services)}")
    return "\n".join(lines)


def format_compose_project(project: ComposeProject, ps_output: str) -> str:
    output = ps_output.strip() or "(no output)"
    if len(output) > 2200:
        output = f"{output[:2200]}\n[truncated]"
    return "\n".join(
        [
            bold(f"Compose {project.name}"),
            f"Path: {code(project.path)}",
            "",
            f"<pre>{h(output)}</pre>",
        ]
    )


def format_k8s_events(events: list[K8sEventSummary], mode: str) -> str:
    title = "k3s warning events" if mode != "all" else "k3s events"
    lines = [bold(title), ""]
    if not events:
        lines.append("No events found.")
        return "\n".join(lines)
    for event in events[:20]:
        timestamp = event.timestamp.isoformat(timespec="seconds") if event.timestamp else "unknown"
        message = event.message.replace("\n", " ")
        if len(message) > 140:
            message = f"{message[:137]}..."
        lines.append(
            f"{code(event.type)} {h(event.namespace)} {h(event.object_kind)}/{h(event.object_name)} "
            f"{h(event.reason)} x{h(event.count or 1)}"
        )
        lines.append(f"  {code(timestamp)} {h(message)}")
    if len(events) > 20:
        lines.append(f"+{len(events) - 20} more")
    return "\n".join(lines)


def format_host_snapshot(snapshot: HostSnapshot) -> str:
    load = "unknown"
    if snapshot.load_average:
        load = ", ".join(f"{value:.2f}" for value in snapshot.load_average)
    return "\n".join(
        [
            bold("NAS"),
            "",
            f"Hostname: {code(snapshot.hostname)}",
            f"OS: {code(snapshot.os_name)}",
            f"Platform: {code(snapshot.platform)}",
            f"Uptime: {code(format_duration(snapshot.uptime_seconds))}",
            f"Boot: {code(snapshot.boot_time.isoformat(timespec='seconds'))}",
            f"Load: {code(load)}",
            f"CPU: {code(percent(snapshot.cpu_percent))}",
            f"Memory: {code(f'{human_bytes(snapshot.memory_used)} / {human_bytes(snapshot.memory_total)} ({percent(snapshot.memory_percent)})')}",
            f"Swap: {code(f'{human_bytes(snapshot.swap_used)} / {human_bytes(snapshot.swap_total)} ({percent(snapshot.swap_percent)})')}",
        ]
    )


def format_mounts(mounts: list[MountUsage], devices: list[str]) -> str:
    lines = [bold("Disks"), ""]
    if mounts:
        for mount in mounts:
            lines.append(
                f"{code(mount.mountpoint)} {h(mount.fstype)} "
                f"{human_bytes(mount.used)} / {human_bytes(mount.total)} "
                f"({percent(mount.percent)})"
            )
            lines.append(f"  {h(mount.device)}")
    else:
        lines.append("No ext/NTFS-style mounts found.")
    lines.append("")
    if devices:
        lines.append(f"SMART devices: {code(', '.join(devices))}")
    else:
        lines.append("No SMART devices configured in NASSER_DISK_DEVICES.")
    return "\n".join(lines)


def format_smart(smart: DiskSmart) -> str:
    lines = [
        bold(f"SMART {smart.device}"),
        "",
        f"Health: {code(smart.health)}",
        f"Model: {code(smart.model or 'unknown')}",
        f"Serial: {code(smart.serial or 'unknown')}",
        f"Temperature: {code(str(smart.temperature_celsius) + ' C' if smart.temperature_celsius is not None else 'unknown')}",
        f"Power on: {code(str(smart.power_on_hours) + ' hours' if smart.power_on_hours is not None else 'unknown')}",
    ]
    if smart.error:
        lines.extend(["", f"Note: {h(smart.error)}"])
    return "\n".join(lines)


def format_docker_summary(summary: Any) -> str:
    if not summary.available:
        return "\n".join([bold("Docker"), "", "Docker is unavailable.", "", h(summary.error or "")])
    return "\n".join(
        [
            bold("Docker"),
            "",
            f"Total: {code(summary.total)}",
            f"Running: {code(summary.running)}",
            f"Exited: {code(summary.exited)}",
            f"Paused: {code(summary.paused)}",
        ]
    )


def format_container_line(container: DockerContainerSummary) -> str:
    return (
        f"{code(container.name)} {h(container.status)} "
        f"{h(container.image)} {code(container.short_id)}"
    )


def format_container_detail(detail: DockerContainerDetail) -> str:
    health = detail.state.get("Health", {}).get("Status") or "unknown"
    started = detail.state.get("StartedAt") or "unknown"
    mounts = ", ".join(mount.get("Destination", "") for mount in detail.mounts[:4]) or "none"
    return "\n".join(
        [
            bold(f"Docker {detail.name}"),
            "",
            f"ID: {code(detail.short_id)}",
            f"Image: {code(detail.image)}",
            f"Status: {code(detail.status)}",
            f"Health: {code(health)}",
            f"Started: {code(started)}",
            f"Mounts: {code(mounts)}",
        ]
    )


def format_k8s_summary(summary: Any) -> str:
    if not summary.available:
        return "\n".join([bold("k3s"), "", "Kubernetes is unavailable.", "", h(summary.error or "")])
    node_line = ", ".join(f"{node.name}={node.ready}" for node in summary.nodes) or "none"
    return "\n".join(
        [
            bold("k3s"),
            "",
            f"Nodes: {code(node_line)}",
            f"Namespaces: {code(summary.namespaces)}",
            f"Deployments: {code(summary.deployments)}",
            f"Pods: {code(summary.pods)}",
        ]
    )


def format_k8s_nodes(nodes: list[Any]) -> str:
    if not nodes:
        return f"{bold('Nodes')}\n\nNo nodes found."
    lines = [bold("Nodes"), ""]
    for node in nodes:
        roles = ", ".join(node.roles) or "none"
        lines.append(
            f"{code(node.name)} ready={code(node.ready)} roles={code(roles)} kubelet={code(node.kubelet_version or 'unknown')}"
        )
    return "\n".join(lines)


def format_deployment_line(deployment: K8sDeploymentSummary) -> str:
    return (
        f"{code(deployment.name)} "
        f"ready={code(f'{deployment.ready}/{deployment.desired}')} "
        f"available={code(deployment.available)}"
    )


def format_pod_line(pod: K8sPodSummary) -> str:
    issues = f" issues={h(', '.join(pod.issues[:2]))}" if pod.issues else ""
    return (
        f"{code(pod.name)} phase={code(pod.phase)} "
        f"restarts={code(pod.restarts)} node={code(pod.node or 'unknown')}{issues}"
    )


def format_sensors(readings: list[SensorReading]) -> str:
    lines = [bold("Sensors"), ""]
    if not readings:
        lines.append("No temperature sensors found.")
        lines.append("Install lm-sensors and run sensors-detect; see the README.")
        return "\n".join(lines)
    current_chip = None
    for reading in readings[:40]:
        if reading.chip != current_chip:
            if current_chip is not None:
                lines.append("")
            current_chip = reading.chip
            lines.append(bold(reading.chip))
        extra = f" (high {reading.high:.0f} C)" if reading.high is not None else ""
        lines.append(f"{code(reading.label)} {reading.current:.1f} C{extra}")
    if len(readings) > 40:
        lines.append(f"+{len(readings) - 40} more")
    return "\n".join(lines)


def format_ports(ports: list[ListeningPort]) -> str:
    lines = [bold("Listening ports"), ""]
    if not ports:
        lines.append("No listening ports found (or ss is unavailable).")
        return "\n".join(lines)
    for port in ports[:40]:
        lines.append(f"{code(f'{port.port}/{port.proto}')} {h(port.address)}")
    if len(ports) > 40:
        lines.append(f"+{len(ports) - 40} more")
    return "\n".join(lines)


def format_updates(status: UpdatesStatus) -> str:
    lines = [bold("Pending updates"), ""]
    if not status.available:
        lines.append("Could not query apt for upgradable packages.")
        if status.error:
            lines.append(h(status.error))
    else:
        lines.append(f"Upgradable packages: {code(status.count)}")
        for package in status.packages[:15]:
            lines.append(code(package))
        if status.count > 15:
            lines.append(f"+{status.count - 15} more")
    lines.append("")
    if status.reboot_required:
        packages = ", ".join(status.reboot_packages[:5]) or "unknown packages"
        lines.append(f"⚠️ Reboot required ({h(packages)})")
    else:
        lines.append("No reboot required.")
    lines.append("")
    lines.append("Counts reflect the last apt refresh; Debian refreshes daily by default.")
    lines.append("Upgrade over SSH: sudo apt update && sudo apt upgrade")
    return "\n".join(lines)


def format_users(users: list[LoggedInUser]) -> str:
    lines = [bold("Logged-in users"), ""]
    if not users:
        lines.append("No interactive sessions.")
        return "\n".join(lines)
    for user in users[:20]:
        since = user.started.astimezone().strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{code(user.name)} on {h(user.terminal or '?')} "
            f"from {h(user.host or 'local')} since {code(since)}"
        )
    return "\n".join(lines)


def format_failed_units(units: list[SystemdUnit]) -> str:
    lines = [bold("Failed systemd units"), ""]
    if not units:
        lines.append("No failed units. 🟢")
        return "\n".join(lines)
    for unit in units[:20]:
        lines.append(f"🔴 {code(unit.name)} {h(unit.active)}/{h(unit.sub)}")
        if unit.description:
            lines.append(f"  {h(unit.description)}")
    if len(units) > 20:
        lines.append(f"+{len(units) - 20} more")
    return "\n".join(lines)


def format_docker_stats(stats: list[DockerContainerStats]) -> str:
    lines = [bold("Docker stats"), ""]
    if not stats:
        lines.append("No running containers.")
        return "\n".join(lines)
    lines.append("Sorted by CPU. Network counters are totals since container start.")
    lines.append("")
    for item in stats[:20]:
        memory = human_bytes(item.memory_used)
        if item.memory_limit:
            memory = f"{memory} / {human_bytes(item.memory_limit)}"
        lines.append(f"{code(item.name)} CPU {item.cpu_percent:.1f}% · RAM {memory}")
        lines.append(f"  net ↓{human_bytes(item.net_rx)} ↑{human_bytes(item.net_tx)}")
    if len(stats) > 20:
        lines.append(f"+{len(stats) - 20} more")
    return "\n".join(lines)


def format_k8s_top(pods: list[K8sPodMetrics], nodes: list[K8sNodeMetrics]) -> str:
    lines = [bold("k3s top"), ""]
    if nodes:
        lines.append(bold("Nodes"))
        for node in nodes:
            lines.append(
                f"{code(node.name)} CPU {node.cpu_millicores:.0f}m · RAM {human_bytes(node.memory_bytes)}"
            )
        lines.append("")
    lines.append(bold("Top pods by CPU"))
    if not pods:
        lines.append("No pod metrics available.")
    for pod in pods[:15]:
        lines.append(f"{code(f'{pod.namespace}/{pod.name}')}")
        lines.append(f"  CPU {pod.cpu_millicores:.0f}m · RAM {human_bytes(pod.memory_bytes)}")
    if len(pods) > 15:
        lines.append(f"+{len(pods) - 15} more")
    lines.append("")
    lines.append("Per-pod network usage is not exposed by metrics-server.")
    return "\n".join(lines)


def format_k8s_top_namespaces(pods: list[K8sPodMetrics]) -> str:
    lines = [bold("k3s usage by namespace"), ""]
    if not pods:
        lines.append("No pod metrics available.")
        return "\n".join(lines)
    totals: dict[str, list[float]] = {}
    for pod in pods:
        entry = totals.setdefault(pod.namespace, [0.0, 0.0, 0.0])
        entry[0] += pod.cpu_millicores
        entry[1] += pod.memory_bytes
        entry[2] += 1
    ordered = sorted(totals.items(), key=lambda item: item[1][0], reverse=True)
    for namespace, (cpu, memory, count) in ordered:
        lines.append(
            f"{code(namespace)} CPU {cpu:.0f}m · RAM {human_bytes(memory)} · {int(count)} pod(s)"
        )
    return "\n".join(lines)


def format_traffic(summary: TrafficSummary) -> str:
    lines = [bold("Traffic (vnstat)"), ""]
    if not summary.available:
        lines.append(h(summary.error or "vnstat unavailable"))
        return "\n".join(lines)
    if not summary.interfaces:
        lines.append("vnstat has no interfaces yet. Give it a few minutes after install.")
        return "\n".join(lines)
    for iface in summary.interfaces:
        lines.append(bold(iface.name))
        for index, day in enumerate(iface.days[:7]):
            marker = " (today)" if index == 0 else ""
            lines.append(f"{code(day.label)} ↓{human_bytes(day.rx)} ↑{human_bytes(day.tx)}{marker}")
        for month in iface.months[:3]:
            lines.append(f"{code(month.label)} ↓{human_bytes(month.rx)} ↑{human_bytes(month.tx)}")
        lines.append(f"Total ↓{human_bytes(iface.total_rx)} ↑{human_bytes(iface.total_tx)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_traffic_brief(summary: TrafficSummary) -> str | None:
    if not summary.available or not summary.interfaces:
        return None
    lines = [bold("Traffic")]
    for iface in summary.interfaces[:3]:
        today = iface.days[0] if iface.days else None
        month = iface.months[0] if iface.months else None
        pieces: list[str] = []
        if today:
            pieces.append(f"today ↓{human_bytes(today.rx)} ↑{human_bytes(today.tx)}")
        if month:
            pieces.append(f"month ↓{human_bytes(month.rx)} ↑{human_bytes(month.tx)}")
        if pieces:
            lines.append(f"{code(iface.name)} {' · '.join(pieces)}")
    return "\n".join(lines) if len(lines) > 1 else None


def format_duration(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    pieces: list[str] = []
    if days:
        pieces.append(f"{days}d")
    if hours or pieces:
        pieces.append(f"{hours}h")
    pieces.append(f"{minutes}m")
    return " ".join(pieces)


def paginate(items: list[Any], page: int, page_size: int = PAGE_SIZE) -> tuple[list[Any], int, int]:
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return items[start : start + page_size], page, total_pages


def pager_rows(prefix: str, page: int, total_pages: int) -> list[list[InlineKeyboardButton]]:
    if total_pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(button("Prev", f"{prefix}:{page - 1}"))
    if page < total_pages - 1:
        row.append(button("Next", f"{prefix}:{page + 1}"))
    return [row] if row else []


def token_payload(context: ContextTypes.DEFAULT_TYPE, token: str) -> Any | None:
    return tokens(context).get(token)


def docker_restart_allowed(
    detail: DockerContainerDetail,
    restart_policy: DockerRestartPolicy,
) -> tuple[bool, str]:
    if restart_policy.allow_all:
        return True, "allowed by allow_all"
    if detail.name in restart_policy.allowed_names:
        return True, "allowed by name"
    if restart_policy.required_labels:
        missing = [
            f"{key}={value}"
            for key, value in restart_policy.required_labels.items()
            if detail.labels.get(key) != value
        ]
        if not missing:
            return True, "allowed by labels"
        return False, f"{detail.name} is missing required label(s): {', '.join(missing)}"
    return False, f"{detail.name} is not allowed by Docker restart policy."


def service_restart_allowed(context: ContextTypes.DEFAULT_TYPE, service: str) -> bool:
    if service not in cfg(context).restartable_services:
        return False
    allowed = policy(context).system.restart_allowed
    return not allowed or service in allowed


def safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def short_button(text: str, limit: int = 56) -> str:
    text = str(text).strip() or "Open"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return safe[:80] or "nasser.log"
