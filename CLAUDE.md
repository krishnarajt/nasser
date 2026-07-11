# Nasser — Claude context

Deterministic Telegram control bot for a Debian NAS (Docker, Compose, single-node k3s, SMART, UPS/NUT, backups, vnstat). Not agentic: it only runs pre-defined commands behind inline-button menus. Deployed as a systemd service (`nasser`) at `/opt/nasser` on the NAS, running as the unprivileged `nasser` user with a narrow sudoers file.

## Layout

- `src/nasser_bot/app.py` — Telegram UI: routing, screens, keyboards, formatters. All callback data is `namespace:action:args`, routed by the `routes` dict in `on_callback`.
- `src/nasser_bot/services/` — one module per subsystem (system, docker, k8s, network, ups, backup, compose, health, alerts). Pattern: `async def x()` wraps a `_x_sync()` via `asyncio.to_thread`; never block the event loop (psutil, SDK calls, subprocess all go through threads or `CommandRunner`).
- `src/nasser_bot/command.py` — `CommandRunner.run(args, sudo=...)`; the only way shell commands execute. No shell=True, ever; args are always lists.
- `src/nasser_bot/config.py` — immutable env config (`Config.from_env`).
- `src/nasser_bot/settings.py` — `SettingsStore` (JSON at `NASSER_STATE_PATH`, default `/var/lib/nasser/settings.json`) + `RuntimeConfig`/`RuntimePolicy` wrappers that let the Telegram Settings menu override env/policy values live. Precedence: settings.json > policy.yaml > env defaults.
- `src/nasser_bot/policy.py` — policy.yaml parsing (thresholds, restart allowlists, compose projects, backups, network checks, UPS).
- `src/nasser_bot/tokens.py` — `TokenStore`: callback payloads > 64 bytes go through short random tokens (1h TTL). Confirm tokens are consumed with `pop()` so they are one-shot.

## Conventions that matter

- Telegram messages are HTML (`ParseMode.HTML`). Always escape with `h()`/`code()`/`bold()` from `formatting.py`. `truncate_text()` is HTML-aware (closes `<b>/<code>/<pre>`); don't hand-truncate.
- Callback data max 64 bytes — anything dynamic (device paths, unit names, dicts) must go through `tokens(context).put(...)`.
- Every state-changing action goes through `ConfirmAction` + `show_confirm`; policy re-checked in `execute_confirmed_action`.
- Health items carry a stable `key` (used for alert dedupe) — never put fluctuating values (percentages, addresses) in the key; alert dedupe is on status transitions only.
- Services take the `RuntimeConfig`/`RuntimePolicy` wrappers, not raw `Config`/`Policy`, so settings changes apply without restart. Read policy sub-objects per call (e.g. `self._policy.network`), don't cache them at init.
- Monitoring toggles (`monitor_docker`, `monitor_k3s`, `ups_enabled`) silence health/alerts for intentionally-off subsystems; menus keep working.

## Checks

No test suite in-repo. Verify with:

```sh
ruff check src/                      # line-length 100, py311
python -c "import nasser_bot.app"    # import sanity
```

Manual test needs a real bot token + allowlisted user ID; most service calls degrade gracefully off-NAS (docker/k3s unavailable screens).

## Deploy notes

Runs on the NAS at `/opt/nasser` (git pull + `pip install -e .` + `systemctl restart nasser` — see README "Updating"). Remote is a private Gitea (`git.krishnarajthadesar.in`). sudoers template in `deploy/sudoers.d/nasser` must list every unit restart and smartctl pattern the bot may run with sudo.
