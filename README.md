# Nasser

Nasser is a deterministic Telegram control bot for a Debian NAS. It gives you inline-button menus for host status, health checks, alerts, disks, Docker, Docker Compose, single-node k3s, backups, network checks, UPS status, logs, and guarded restart actions.

It is intentionally not agentic. It does not run arbitrary shell commands from chat.

## Features

- Telegram inline menu UI with `/menu`
- `/status` health dashboard with emoji status icons and vnstat traffic summary
- Telegram user/chat allowlist
- **Settings menu in Telegram** — configure almost everything from chat, by picking
  from auto-discovered lists (disks, systemd units) instead of editing files:
  - SMART disks (auto-discovered via lsblk, stored as stable `/dev/disk/by-id` paths)
  - journal log units and restartable services (picked from `systemctl list-units`)
  - alert on/off, check interval, and repeat interval
  - monitoring toggles for Docker / k3s / UPS (silence alerts for a subsystem you stopped on purpose)
  - health thresholds (disk %, temps, memory, CPU, load, backup staleness)
  - ping/DNS check hosts, with your default gateway auto-detected
  - changes persist to `NASSER_STATE_PATH` and apply without restarting the bot
- NAS uptime, load, CPU, memory, swap, OS info
- System extras: temperature sensors, failed systemd units, pending apt updates,
  reboot-required detection, logged-in users, listening ports, mdadm RAID status
- Filesystem usage for ext and NTFS-style mounts (container bind mounts filtered out)
- SMART health and disk temperature for configured devices
- Docker summary, container lists, details, logs, restart, and **per-container
  CPU/RAM/network stats**
- Docker Compose project/service `ps`, logs, and restarts from policy-defined projects
- k3s node summary, namespaces, deployments, pods, pod logs, events, rollout restart,
  and **per-pod / per-namespace CPU+RAM usage** via metrics-server
- **Daily/monthly traffic** per interface through vnstat, also shown in `/status`
- Backup job status, logs, and guarded run-now actions
- Network interfaces, public IP, ping checks, DNS checks, and port checks
- UPS status through NUT `upsc`
- Scheduled Telegram alerts that fire on status changes and recoveries only
  (no spam from fluctuating values), with a configurable repeat interval
- YAML policy for thresholds and action allowlists
- Journal log tails for configured systemd units
- Confirmed, one-shot restart buttons for configured systemd services

## Recommended Deployment

Run Nasser as a host-level `systemd` service. This is better than running it inside Docker for the first version because the bot needs controlled access to host-level things: `systemctl`, `journalctl`, SMART data, Docker, and the local k3s kubeconfig.

## Install On Debian

These commands assume the repo will live at `/opt/nasser`.

```sh
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip sudo smartmontools lm-sensors iputils-ping vnstat iproute2

sudo useradd --system --home /opt/nasser --shell /usr/sbin/nologin nasser
sudo mkdir -p /opt/nasser /etc/nasser
sudo chown nasser:nasser /opt/nasser

# State directory for settings changed from Telegram (Settings menu).
sudo mkdir -p /var/lib/nasser
sudo chown nasser:nasser /var/lib/nasser

sudo git clone <your-repo-url> /opt/nasser
sudo chown -R nasser:nasser /opt/nasser

cd /opt/nasser
sudo -u nasser python3 -m venv .venv
sudo -u nasser .venv/bin/pip install --upgrade pip
sudo -u nasser .venv/bin/pip install -e .
```

Enable the helpers Nasser reads from:

```sh
# Traffic accounting for the Traffic screen and /status.
sudo systemctl enable --now vnstat

# Detect temperature sensors once; answer the defaults.
sudo sensors-detect --auto
```

If you are developing locally first, replace the `git clone` line with copying this working tree to `/opt/nasser`.

## Telegram Setup

Create a bot with BotFather and put its token in `/etc/nasser/nasser.env`.

Get your numeric Telegram user ID. One common way is to message `@userinfobot` from Telegram. After your user ID is configured, Nasser's `/id` command can show the chat ID if you want to lock the bot to one private chat or group.

```sh
sudo install -m 0600 -o root -g nasser /opt/nasser/.env.example /etc/nasser/nasser.env
sudo install -m 0640 -o root -g nasser /opt/nasser/policy.example.yaml /etc/nasser/policy.yaml
sudo nano /etc/nasser/nasser.env
sudo nano /etc/nasser/policy.yaml
```

Minimum required values:

```sh
TELEGRAM_BOT_TOKEN=123456:replace_me
TELEGRAM_ALLOWED_USER_IDS=123456789
NASSER_NAME="Home NAS"
KUBECONFIG=/etc/nasser/k3s.yaml
NASSER_POLICY_PATH=/etc/nasser/policy.yaml
```

`TELEGRAM_ALLOWED_CHAT_IDS` is optional. Leave it empty to allow your configured user in any chat, or set it to a private chat/group ID for tighter access.

## Configure From Telegram

After the service is running, most day-to-day configuration happens in chat, not in files. Open `/menu` → **Settings**:

- **SMART disks** — Nasser lists every physical disk it can see (via `lsblk`) with model and size; tap to toggle monitoring. You never have to type a `/dev/disk/by-id/...` path by hand.
- **Log units** and **Restart services** — pick from the live list of systemd services on the box, selected units sort first. Restarting a unit via sudo also needs a sudoers rule (see below).
- **Alerts** — toggle on/off, set the check interval (1m–1h) and the repeat interval for unresolved problems (30m–1d).
- **Monitoring** — turn Docker/k3s/UPS health checks off when a subsystem is intentionally stopped, so it stops alerting. The menus keep working.
- **Thresholds** — −/+ buttons for every health threshold.
- **Network checks** — toggle ping/DNS hosts; your default gateway is auto-detected and offered as an option.

Changes are stored in `NASSER_STATE_PATH` (default `/var/lib/nasser/settings.json`), survive restarts, and override the corresponding env/policy.yaml values. If the Settings screen warns that settings are not persisted, create the state directory as shown in the install steps.

## Docker Access

Add the `nasser` user to the Docker group:

```sh
sudo usermod -aG docker nasser
```

Anyone who can control Docker can effectively control the host, so keep `TELEGRAM_ALLOWED_USER_IDS` narrow and protect your bot token like a password.

By default `policy.yaml` keeps Docker container restarts permissive:

```yaml
docker:
  restart:
    allow_all: true
```

After setup, tighten it with labels:

```yaml
docker:
  restart:
    allow_all: false
    required_labels:
      nasser.restart: "true"
```

Then label containers you are willing to restart from Telegram when you create them:

```sh
docker run --label nasser.restart=true ...
```

For Compose-managed containers, add the label in the service definition and recreate the service.

## k3s Access

Copy the k3s kubeconfig into a location readable by the `nasser` group:

```sh
sudo cp /etc/rancher/k3s/k3s.yaml /etc/nasser/k3s.yaml
sudo chown root:nasser /etc/nasser/k3s.yaml
sudo chmod 0640 /etc/nasser/k3s.yaml
```

If the copied kubeconfig points at a hostname that does not resolve from the service, edit `/etc/nasser/k3s.yaml` and set the server to the local API endpoint, usually:

```yaml
server: https://127.0.0.1:6443
```

## Logs Access

For richer `journalctl` access, add `nasser` to the journal group:

```sh
sudo usermod -aG systemd-journal nasser
```

Set the units you want in the menu:

```sh
NASSER_LOG_UNITS=k3s,docker,containerd,ssh
```

## Alerts

Alerts are enabled by default and check every five minutes. An alert is sent when a problem first appears, changes status (warn ↔ crit), or recovers — fluctuating details like CPU percentages do not re-trigger. Unresolved problems are re-sent after the repeat interval (one hour by default).

Interval, repeat, and on/off are all changeable live from `/menu` → Settings → Alerts. The env values below are just the defaults:

```sh
NASSER_ALERT_CHAT_IDS=123456789
NASSER_ALERT_INTERVAL_SECONDS=300
NASSER_ALERT_REPEAT_SECONDS=3600
```

If `NASSER_ALERT_CHAT_IDS` is empty, Nasser uses `TELEGRAM_ALLOWED_CHAT_IDS`. If both are empty, it can only alert after you have opened the bot once from an allowed account.

If a subsystem is down on purpose (say k3s is stopped for the summer), silence it in `/menu` → Settings → Monitoring instead of muting alerts entirely.

## Policy And Thresholds

Most non-secret behavior lives in `/etc/nasser/policy.yaml`:

```yaml
thresholds:
  disk_usage_warning_percent: 85
  disk_usage_critical_percent: 95
  disk_temp_warning_c: 45
  disk_temp_critical_c: 55

system:
  restart_allowed:
    - k3s
    - docker
```

`system.restart_allowed` is an extra limiter on top of `NASSER_RESTARTABLE_SERVICES`.

## Disk SMART Setup

The easy way: `/menu` → Settings → **SMART disks** and tap the disks you want. Nasser stores stable `/dev/disk/by-id` paths automatically.

If you prefer env config, list stable disk IDs and set them yourself:

```sh
ls -l /dev/disk/by-id/
```

```sh
NASSER_DISK_DEVICES=/dev/disk/by-id/ata-disk-one,/dev/disk/by-id/ata-disk-two
```

Nasser runs `smartctl -a -j <device>` (via sudo; the sudoers template already allows it). Disk temperature feeds the health dashboard and alerts with the `disk_temp_*` thresholds.

Note: `smartctl -a` wakes disks that are in standby. If you rely on aggressive disk spindown, raise the alert check interval in Settings → Alerts.

Some USB/SATA bridges need extra `smartctl -d ...` flags. That is not wired in yet.

## Docker Compose

Add Compose projects to `/etc/nasser/policy.yaml`:

```yaml
docker:
  compose_projects:
    - name: media
      path: /opt/stacks/media
      files:
        - compose.yaml
      services:
        - plex
        - jellyfin
      sudo: false
```

Nasser runs fixed `docker compose` commands against those project paths only. If `services` is empty, project-level logs/restart are available. If services are listed, service-level log/restart buttons are shown too.

## k3s Events

The k3s screen includes an Events view. It can show warning events only or recent events across all namespaces.

The health dashboard also detects pods with non-running phases, waiting reasons such as `CrashLoopBackOff`, and containers with three or more restarts.

## Backups

Backup jobs are declared in `policy.yaml`. Nasser never accepts a backup command from Telegram; it only runs the configured command for a named job.

```yaml
backups:
  jobs:
    - name: restic
      run_command:
        - /usr/local/bin/run-restic-backup
      status_file: /var/lib/nasser/backups/restic-last-success
      stale_after_hours: 30
      log_unit: restic-backup.service
      sudo: true
```

If `sudo: true`, add the exact command to `/etc/sudoers.d/nasser`:

```text
nasser ALL=(root) NOPASSWD: /usr/local/bin/run-restic-backup
```

For stale checks, make your backup script update the status file after a successful run:

```sh
install -D -m 0644 /dev/null /var/lib/nasser/backups/restic-last-success
touch /var/lib/nasser/backups/restic-last-success
```

## Network Checks

Network checks live in `policy.yaml`:

```yaml
network:
  public_ip_url: https://api.ipify.org
  ping_hosts:
    - 1.1.1.1
  dns_hosts:
    - debian.org
  port_checks:
    - name: k3s api
      host: 127.0.0.1
      port: 6443
```

## Traffic With vnstat

The Network → Traffic screen and the `/status` summary read from vnstat. One-time setup:

```sh
sudo apt install -y vnstat
sudo systemctl enable --now vnstat
```

vnstat needs a few minutes after first start before it has data, and roughly a day before daily totals are meaningful. It tracks all physical interfaces by default; use `vnstat --iflist` to check what it sees.

## Sensors, Updates, Users, Ports, RAID

The NAS screen has buttons for:

- **Sensors** — every temperature sensor lm-sensors exposes (run `sudo sensors-detect --auto` once). The hottest CPU sensor also feeds the health dashboard with the `cpu_temp_*` thresholds.
- **Updates** — pending apt upgrades and whether a reboot is required. Counts refresh from apt's package lists, which Debian updates daily via the `apt-daily` timers. Read-only: Nasser never installs packages.
- **Users** — active login sessions and where they are from.
- **Ports** — listening TCP/UDP ports (`ss -tuln`).
- **Failed units** — systemd units in a failed state (also a health/alert item).
- **RAID** — `/proc/mdstat`, when mdadm arrays exist.

## UPS Through NUT

Install a NUT client if you use a UPS:

```sh
sudo apt install -y nut-client
```

Enable it in `policy.yaml`:

```yaml
ups:
  enabled: true
  target: ups@localhost
```

## Privileged Restart And SMART Permissions

Install the sudoers template:

```sh
sudo install -m 0440 -o root -g root /opt/nasser/deploy/sudoers.d/nasser /etc/sudoers.d/nasser
sudo visudo -cf /etc/sudoers.d/nasser
```

Edit `/etc/sudoers.d/nasser` if your service names differ. Keep it narrow.

The default template allows only:

- `systemctl restart k3s`
- `systemctl restart docker`
- `smartctl -a -j /dev/*`
- `smartctl -a -j /dev/disk/by-id/*`

When you enable extra services under Telegram → Settings → Restart services, add a matching `systemctl restart <unit>` pair to the sudoers file (the template has a commented example), or uncomment the broad `systemctl restart *` line if you accept that trade-off. Without a rule, the restart button runs but reports a sudo failure.

Backup commands with `sudo: true` must be added as exact commands. Do not add broad wildcards for backup scripts.

## Install The systemd Service

```sh
sudo install -m 0644 -o root -g root /opt/nasser/deploy/systemd/nasser.service /etc/systemd/system/nasser.service
sudo systemctl daemon-reload
sudo systemctl enable --now nasser.service
```

Check status and logs:

```sh
sudo systemctl status nasser.service
sudo journalctl -u nasser.service -f
```

Then open Telegram and send:

```text
/menu
/status
```

## Updating Nasser

When new commits land in the repo (e.g. after you `git push` from your dev machine), pull them on the NAS **inside `/opt/nasser`** and restart the service:

```sh
cd /opt/nasser
sudo -u nasser git pull
sudo -u nasser .venv/bin/pip install -e .
sudo systemctl restart nasser.service
```

Then check it came back up:

```sh
sudo systemctl status nasser.service
sudo journalctl -u nasser.service -n 50 --no-pager
```

Notes:

- The `pip install -e .` step matters when dependencies or entry points changed; it is instant when they have not, so just always run it.
- If the update added new env vars, compare your `/etc/nasser/nasser.env` against `/opt/nasser/.env.example` (`diff <(sort /etc/nasser/nasser.env) <(sort /opt/nasser/.env.example)` gives a quick view). Same idea for `policy.yaml` vs `policy.example.yaml`.
- If the update changed the systemd unit or sudoers template, re-install them:

```sh
sudo install -m 0644 -o root -g root /opt/nasser/deploy/systemd/nasser.service /etc/systemd/system/nasser.service
sudo install -m 0440 -o root -g root /opt/nasser/deploy/sudoers.d/nasser /etc/sudoers.d/nasser
sudo visudo -cf /etc/sudoers.d/nasser
sudo systemctl daemon-reload
sudo systemctl restart nasser.service
```

### One-time migration for this update

This release added the Telegram Settings menu, vnstat traffic, sensors, and system extras. It also fixes the systemd unit: the old unit's `ProtectKernelTunables`/`ProtectKernelModules` options silently implied `NoNewPrivileges=yes` for the non-root service user, which made every sudo action (SMART, service restarts) fail with:

```text
sudo: The "no new privileges" flag is set, which prevents sudo from running as root.
```

Re-install the unit and confirm the flag is gone:

```sh
sudo install -m 0644 -o root -g root /opt/nasser/deploy/systemd/nasser.service /etc/systemd/system/nasser.service
sudo systemctl daemon-reload
sudo systemctl restart nasser.service

# Must print "NoNewPrivs: 0".
grep NoNewPrivs /proc/$(systemctl show -p MainPID --value nasser)/status
```

On an existing install, also run once:

```sh
# New packages.
sudo apt install -y vnstat iproute2
sudo systemctl enable --now vnstat
sudo sensors-detect --auto

# State directory for Telegram-managed settings.
sudo mkdir -p /var/lib/nasser
sudo chown nasser:nasser /var/lib/nasser

# New env vars (defaults are fine; add if you want to override).
echo 'NASSER_STATE_PATH=/var/lib/nasser/settings.json' | sudo tee -a /etc/nasser/nasser.env

sudo install -m 0440 -o root -g root /opt/nasser/deploy/sudoers.d/nasser /etc/sudoers.d/nasser
sudo visudo -cf /etc/sudoers.d/nasser
/etc/sudoers.d/nasser: parsed OK

# Fix the placeholder disk if you still have it configured:
# either clear NASSER_DISK_DEVICES in /etc/nasser/nasser.env and pick disks
# from Telegram -> Settings -> SMART disks, or set real /dev/disk/by-id paths.
```

## Local Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
cp policy.example.yaml policy.yaml
set -a
. ./.env
set +a
export NASSER_POLICY_PATH="$PWD/policy.yaml"
export NASSER_STATE_PATH="$PWD/settings.json"
nasser-bot
```

## Security Notes

- The bot token is equivalent to remote access to these menus.
- Docker access is highly privileged.
- k3s kubeconfig access is highly privileged.
- Keep restartable services in `NASSER_RESTARTABLE_SERVICES` limited to services you actually want in Telegram.
- Keep `policy.yaml` action entries narrow, especially backup commands and Compose projects.
- Keep `NASSER_LOG_UNITS` limited to logs you are comfortable viewing through Telegram.
- All restart actions require confirmation, but Telegram account compromise would still be serious.
