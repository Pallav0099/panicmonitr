# Panic Monitor

A zero-infrastructure, peer-to-peer health monitoring daemon. Each node is
sovereign — there's no central server. Nodes form a flat-peer mesh over
[Iroh](https://iroh.computer/) QUIC connections, and access control is an
append-only, cryptographically signed log per node. The dashboard, container
stats, gap-fill sync, and webhook alerting are all local-first.

If you're used to centralized uptime monitors (Pingdom, UptimeRobot, Datadog
Synthetics), think of this as the same thing turned inside out: every node
runs the monitor *and* is monitored, and consent is explicit per-permission.

What you get out of the box:

- **Heartbeat probing** of peers you trust, with configurable thresholds,
  flap suppression, and webhook notifications on UP/DOWN.
- **A live single-page dashboard** at `http://127.0.0.1:42069/` — no reloads,
  configurable poll interval, pause-on-tab-hidden.
- **btop-style process monitoring** — top-N processes by CPU/MEM/RSS with
  PID, user, threads, command.
- **Detailed Docker diagnostics** — click any container card to expand
  inline and see net I/O, block I/O, ports, mounts, health-check output,
  restart count, started-at, and the last 20 log lines.
- **Cryptographic audit trail** — every trust mutation and DOWN/UP transition
  is a signed entry in an append-only log.
- **Local-first storage** — SQLite history + logstore, no external service
  required. Pull peer dashboards over iroh QUIC when both sides consent.

---

## Quick start

Run these on **both** machines.

```fish
# 1. Install
git clone <repo-url> panic-monitr && cd panic-monitr
python3 -m venv .venv && source .venv/bin/activate.fish   # or .venv/bin/activate
pip install -e .

# 2. Init + start the daemon
panic-monitor --init              # pick a password, generates signing identity
panic-monitor --install-service   # wires up systemd, encrypts password, starts

# 3. Share your Node ID with the other machine
panic-monitor --show-identity     # prints a 64-char hex string — share it

# 4. Add the other machine's Node ID
panic-monitor --add-peer <THEIR_NODE_ID> --alias "my-other-machine" --permissions monitor

# 5. Open the dashboard
open http://127.0.0.1:42069/     # live dashboard, auto-refreshing
```

After both sides add each other, you'll see the peer show up in the fleet
panel within one probe interval (30s default). Click any peer card to see
their live CPU, memory, disk, processes, and containers.

That's it. Read on for roles, webhooks, hardening, and troubleshooting.

---

## Contents

- [Quick start](#quick-start)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [First-time setup](#first-time-setup)
- [Adding devices (peers)](#adding-devices-peers)
- [Permissions model](#permissions-model)
- [Daemon permissions and hardening](#daemon-permissions-and-hardening)
- [Web dashboard](#web-dashboard)
- [Day-to-day operations](#day-to-day-operations)
- [Roles](#roles)
- [Webhooks](#webhooks)
- [Where state lives](#where-state-lives)
- [System (root) mode](#system-root-mode)
- [Troubleshooting](#troubleshooting)
- [CLI reference](#cli-reference)

---

## Prerequisites

- Linux with **systemd ≥ 250** for system-wide service installation, or **systemd ≥ 256** for user-mode service installation using `systemd-creds`.
- **Python 3.12+**
- A working **TPM2** chip is nice-to-have but not required; `systemd-creds`
  falls back to the per-user host key automatically.
- **Docker** (optional — needed only for container stats collection).

**Note:** If you are running a stable Linux distribution (like Ubuntu 24.04) with **systemd < 256**, you cannot use `systemd-creds` for user-mode installation. Instead, use the **keyring** backend:
`panic-monitor --install-service --password-from keyring`

---

## Install

```fish
git clone <repository_url> panic-monitr
cd panic-monitr
python3 -m venv .venv
source .venv/bin/activate.fish    # or .venv/bin/activate for bash/zsh
pip install -e .
```

The `panic-monitor` console script lands in the venv's `bin/`. Either keep
that venv active for the install commands below or call it by absolute path.

---

## First-time setup

Two commands. The first generates a signing identity; the second wires up the
systemd unit, encrypts your password, and starts the daemon.

```fish
panic-monitor --init             # generate identity; prompts twice for a new password
panic-monitor --install-service  # renders the unit, encrypts the password, enable + start
```

After this, every administrative command (`--add-peer`, `--list-peers`,
`--set-maintenance`, …) talks to the running daemon over its local control
socket. No further password prompts, no daemon restarts.

### What `--init` does

- Generates a fresh Curve25519 keypair. The public key is your **Node ID** — a
  64-character lowercase hex string. Share it with peers who should be able
  to monitor or view you.
- Seals the secret key under your password via argon2id → SecretBox, writes
  the ciphertext to `secret.key` (mode 0600), and the public Node ID + salt
  to `secret.meta`.
- Writes a genesis entry to `log.jsonl`, the append-only signed trust log.
- Pick a password you can type unattended — you'll need it once more during
  `--install-service`, after which systemd-creds holds the only copy.

### What `--install-service` does

- **User mode by default** (no sudo). Writes
  `~/.config/systemd/user/panic-monitor.service` and runs
  `systemctl --user enable --now panic-monitor`.
- **System mode when run as root**. Writes `/etc/systemd/system/panic-monitor.service`
  and uses the host's systemd instance.
- Prompts once for the identity password, encrypts it via `systemd-creds`
  (with `--user` scope in user mode — important, see [Troubleshooting](#troubleshooting)),
  and stores the ciphertext at `<config-dir>/password.cred`. The plaintext
  never lands on disk; systemd decrypts it into a private tmpfs at unit start.
- Bakes the resolved `panic-monitor` entrypoint into the unit via
  `shutil.which`, so a venv install doesn't break on `$PATH` differences.

Useful flags:

| Flag | Effect |
|---|---|
| `--user` / `--system` | Override the auto-detected mode |
| `--force` | Overwrite an existing unit |
| `--rotate-password` | Re-encrypt the credential (after a host migration, password change, etc.) |
| `--password-from {systemd-creds,keyring,stdin,env,pinentry}` | Pick a different backend (default: `systemd-creds`) |

### Verify it's running

```fish
systemctl --user status panic-monitor.service
journalctl --user -u panic-monitor.service -n 50
```

You should see `Engine ready  role=both  interval=30s` toward the end of the
journal. Open `http://127.0.0.1:42069/` for the dashboard.

---

## Adding devices (peers)

The trust model is **default-deny**: no peer can monitor, push to, or query
this node until you explicitly add their Node ID to your trust log.

### Step 1 — get the peer's Node ID

On the other device:

```fish
panic-monitor --show-identity
```

It prints a 64-character hex string. Share it however you'd share any public
key (Signal, paste, QR — doesn't matter, it's not secret).

### Step 2 — add them on your side

```fish
panic-monitor --add-peer <NODE_ID> \
    --alias "api-server" \
    --permissions monitor \
    --tags "prod,critical"
```

The default `monitor` permission covers everything — heartbeat probes,
dashboard stats pull, container log fetch, and historical sync. You
don't need separate `view_dashboard` unless you want a peer to see your
stats without being probed.

The change takes effect immediately — the daemon reloads its monitor target
list via the control socket. No restart.

### Step 3 — they add you on their side

Trust is per-direction. For your node to be able to *pull* their dashboard
or probe them, they must also `--add-peer` your Node ID. With default
`monitor` permission on both sides, full bidirectional monitoring and stats
sharing work out of the box.

### Listing, filtering, revoking

```fish
panic-monitor --list-peers                       # all peers
panic-monitor --list-peers --filter-tag prod     # filter by tag
panic-monitor --revoke-peer <NODE_ID_OR_ALIAS>   # signed revoke op in the log
```

Revocation is logged, not deleted — the chain stays auditable.

### Tags and maintenance

```fish
panic-monitor --add-tag api-server staging
panic-monitor --remove-tag api-server staging
panic-monitor --set-tags api-server "prod,db,critical"

# Suppress alerts during a planned outage. END can be ISO time or '+2h'/'+30m'/'+1d'.
panic-monitor --set-maintenance api-server +0 +2h
panic-monitor --clear-maintenance api-server
panic-monitor --list-maintenance
```

While a peer is in maintenance: probes still run (so history is unbroken)
but DOWN/UP transitions don't fire webhooks or signed log events.

---

## Permissions model

Permissions are **per-peer, per-protocol**, granted by you on your trust log.
Each peer can hold any subset of these:

| Permission | What it grants the peer |
|---|---|
| `monitor` | **Everything** — probing, dashboard stats, container logs, push, and
historical sync. This is the default and the only permission most setups need. |
| `view_dashboard` | Optional. Explicit dashboard + container-logs access,
without requiring `monitor`. Useful when you want a peer to see your stats
but not appear on your probe target list. Ignored when `monitor` is also
present (already covered). |
| `chat` | Reserved — currently a no-op slot for future protocols |
| `split` / `call` / `drop` | Same — protocol slots reserved for the wider PanicLab ecosystem |
| `*` | All current and future permissions |

**Permission fallback.** All ALPN handlers that need `view_dashboard` also
accept `monitor` as a fallback. This means adding a peer with the default
`["monitor"]` is enough for the full feature set — live stats sharing,
container log fetching, and heartbeat probes all work without explicitly
adding `view_dashboard`.

```fish
# The default — covers everything (probe + stats + logs + sync)
panic-monitor --add-peer <NID> --permissions monitor

# Explicit dashboard-only access (won't appear on your probe target list)
panic-monitor --add-peer <NID> --permissions view_dashboard

# Upgrade an existing peer
panic-monitor --update-permissions <NID> "monitor,view_dashboard"
```

(Use `panic-monitor --help` if `--update-permissions` isn't on your build —
older builds use `--add-peer` with `--force` instead.)

---

## Daemon permissions and hardening

The unit rendered by `--install-service` is sandboxed by systemd. Defaults
chosen for safety on a workstation; tighten or loosen as you like.

| Directive | Value | Why |
|---|---|---|
| `MemoryHigh` / `MemoryMax` | 180M / 256M | Cap a leak before it pages the host. ~110M is typical resident-set. |
| `CPUQuota` | 50% | One half-core, even on a single-core VM |
| `Nice` | 10 | Yield to interactive work |
| `LimitNOFILE` | 512 | File descriptor cap (SQLite + iroh sockets fit easily) |
| `TasksMax` | 64 | Cgroup-scoped task cap — does **not** confuse argon2/Python thread pools |
| `NoNewPrivileges` | yes | No setuid/setgid escalation possible |
| `ProtectSystem` | strict | Whole FS read-only except `ReadWritePaths` |
| `PrivateTmp` | yes | Service has its own /tmp tmpfs |
| `ReadWritePaths` | config-dir, data-dir | The only writable paths |

In **system mode** the unit also gets `ProtectHome=yes`, `ProtectKernelTunables`,
`ProtectKernelModules`, `ProtectControlGroups`, `RestrictSUIDSGID`,
`RestrictAddressFamilies`, and a `SystemCallFilter` allowlist. Those directives
require `CAP_SETPCAP`, which the per-user systemd instance doesn't have, so
they're omitted in user mode. You can still inspect the rendered unit at
`~/.config/systemd/user/panic-monitor.service` to confirm what's active.

### To rotate the encrypted password

```fish
panic-monitor --install-service --rotate-password --force
```

Re-prompts for the password, re-encrypts under the same backend, rewrites the
unit. Useful after a host migration (the `systemd-creds` host key is host-
specific) or a password change.

### To uninstall

```fish
panic-monitor --uninstall-service
```

Disables and removes the unit. State files (identity, history, logs) are left
in place; delete `~/.config/panic-monitor` and `~/.local/share/panic-monitor`
manually if you want a clean wipe.

---

## Web dashboard

Two HTTP surfaces, both bound to `127.0.0.1` by default. Neither has auth —
keep them on loopback or put them behind a reverse proxy that adds auth.

| URL | Source | Default port | Notes |
|---|---|---|---|
| `http://127.0.0.1:42069/` | `src/webapp.py` (Flask + Plotly) | 42069 | Single-page live dashboard. JS polls `/api/dashboard`; the page itself never reloads. |
| `http://127.0.0.1:8080/` | `src/statuspage.py` (stdlib `http.server`) | 8080 | Lightweight peer list + sparklines, also exposes `/status.json`. |

The Flask dashboard at port 42069 renders once and then streams values via a
single JSON endpoint. Open it once, leave the tab open all day, never see a
page reload. Scroll position, hover state, and expanded container detail
panels survive every refresh.

The status page on port 8080 is a separate, lighter UI built on the stdlib
HTTP server — useful for headless servers or as a status endpoint for an
external monitor. Both share the same `build_dashboard_snapshot` builder,
which is cached for 1 second so concurrent polls don't multiply SQLite load.

### Live dashboard at a glance

```
┌──────────────────────────────────────────────────────┐
│  ███ ███ ██ █ █ ██ █▄ █▄█ ███  PANICMONITR (banner)  │
│  • live · updated 14:35:04  role: both  node: 0b01… │
│  refresh: [5s ▾]  [Refresh]                          │
│ ┌──[Fleet]─────────────────────────────────────────┐ │
│ │  Targets  Alive  Dead  Maint  AvgUp  Probes24h   │ │
│ └──────────────────────────────────────────────────┘ │
│ ┌──[System]──────┐  ┌──[CPU · MEM — last hour]────┐ │
│ │ CPU ███▌  14%  │  │      ╱╲    Plotly area      │ │
│ │ MEM ████  42%  │  │  ___╱  ╲___╱╲___            │ │
│ │ DISK ▍    63%  │  └─────────────────────────────┘ │
│ │ Host / Load …  │                                   │
│ └────────────────┘                                   │
│ ┌──[Processes]───────────────────────────────────┐  │
│ │ sort: [CPU%▾]  show: [top 20▾]  σ CPU  σ MEM   │  │
│ │  PID   USER    CPU%  MEM%  RSS    Thr  ST  CMD │  │
│ │  6243  pallav   4.1  3.6   598MB  28   slp claude│
│ │  …                                              │  │
│ └─────────────────────────────────────────────────┘  │
│ ┌──[Containers]──────────────────────────────────┐  │
│ │  ▸ searxng-crawler  running · healthy   CPU…  │  │
│ │  ▸ redis            running             CPU…  │  │
│ │  ▾ web-app          running · unhealthy  ←──┐  │  │
│ │    image / id / command / created / started   │  │
│ │    net ↓ 2.1 MB ↑ 0.8 MB     blk r 14 MB …    │  │
│ │    ports 80→8080 · mounts /data → /srv …      │  │
│ │    health unhealthy · failing streak 3        │  │
│ │    ─── recent logs ─── [Refresh]               │  │
│ │    2026-05-19T07:48:32  httpx.ConnectError…    │  │
│ └─────────────────────────────────────────────────┘  │
│ ┌──[Peers]───────────────────────────────────────┐  │
│ │  alias  status  sync  rtt  24h  last  tags     │  │
│ └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### Refresh control

Top-right of the meta bar:

- **`refresh` dropdown** — choose 2 s / 5 s (default) / 10 s / 30 s / 1 m / **paused**.
  Choice persists in `localStorage` under `panic-monitor.poll-interval`.
- **`[Refresh]` button** — fires one immediate poll without waiting for the
  interval. Useful when paused.
- **Tab-visibility aware** — switching to another tab pauses polling
  automatically; coming back triggers an immediate refresh and resumes the
  schedule. Saves SQLite contention if you leave the tab open all day.
- The live-indicator dot is **teal pulsing** when fresh, **red** when a poll
  fails, **gray** when paused.

### `[Processes]` — btop-style top-N list

20 most CPU-busy processes by default. Per-row: PID, user, CPU %, MEM %,
RSS (adaptive bytes), thread count, state, command.

- **`sort`** dropdown: CPU% (default), MEM%, RSS, or PID.
- **`show`** dropdown: top 10 / 20 / 50 (top 20 default).
- Sorting and limit changes re-paint locally without refetching.
- σ-CPU / σ-MEM totals on the right show the aggregate of all 20 returned
  processes.
- The collector seeds a CPU-percent baseline on first call (psutil's `cpu_percent`
  is delta-based and returns 0 cold). Subsequent ticks return real values.
- Sort and limit choices persist (`panic-monitor.proc-sort`, `panic-monitor.proc-limit`).
- Process data is **live-only** — never written to SQLite. Each poll is a
  fresh `psutil.process_iter()` scan, ~30 ms on a typical workstation.

### `[Containers]` — click to expand for deep diagnostics

The grid shows summary cards (image, name, status, health, CPU, MEM) just
like before, but every card is now a `<details>` element. Click → expands
inline (full-width) and reveals:

| Field | Source |
|---|---|
| Image, ID, command | docker inspect |
| Created · Started | RFC3339 + relative |
| Restart count | `HostConfig.RestartCount` |
| Network ↓ / ↑ | sum across all attached interfaces |
| Block I/O r / w | `blkio_stats.io_service_bytes_recursive` (cgroup v1; v2 may report 0) |
| Memory usage / limit | `memory_stats.usage` / `.limit` |
| Ports | "`hostPort→containerPort/proto`" chips |
| Mounts | "`/host → /container[:ro]`" chips |
| Health | status + failing streak + last health-check output |
| Recent logs | tail 20 (timestamps included), lazy-fetched on first expand |

The `recent logs` block hits a dedicated endpoint `/api/container/<id>/logs?tail=N`
only when you expand the card. Cached per container for 5 seconds; click
`[Refresh]` inside the detail to force a re-fetch.

The `<details>` open state persists across polls — the diff-by-name DOM
update strategy mutates existing nodes rather than recreating them, so
expanding a card and watching it for a minute is fine.

### Disabling / exposing

```fish
panic-monitor --daemon --dashboard-port 0    # disable Flask dashboard
panic-monitor --daemon --status-bind ""      # disable stdlib status page
```

To expose on the LAN (no built-in auth!):

```fish
# edit the unit's ExecStart or run manually:
panic-monitor --daemon --status-bind "0.0.0.0:8080"
```

The daemon **logs a warning** if you bind to a non-loopback address.

---

## Day-to-day operations

```fish
# state
systemctl --user status panic-monitor.service
journalctl --user -u panic-monitor.service -f

# restart after editing the unit
systemctl --user daemon-reload
systemctl --user restart panic-monitor.service

# identity ops (no password needed)
panic-monitor --show-identity
panic-monitor --list-peers

# password change — re-seals the same Node ID under a new password
panic-monitor --reset-password
panic-monitor --install-service --rotate-password --force   # then re-encrypt the cred
```

### Querying history

```fish
panic-monitor --uptime api-server --window 24h   # 0–100% over the window
panic-monitor --history api-server --hours 6     # raw probe stream
panic-monitor --fetch-dashboard api-server       # pull their live dashboard once
```

### Test the webhook

```fish
panic-monitor --test-webhook --webhook-url https://ntfy.sh/your-topic
```

---

## Roles

`--role {monitored,monitoring,both}` (default: `both`):

- **monitored** — collects local system + container stats, writes to the
  local logstore, serves them when peers pull. Doesn't pull peers itself.
  Right choice for headless servers.
- **monitoring** — pulls peers, renders the TUI / Web UI, schedules gap-fill
  syncs when a peer reconnects after being offline. Doesn't collect its own
  stats.
- **both** — runs both. Default; most desktops and "watch each other" pairs
  want this.

---

## Webhooks

```fish
panic-monitor --daemon --webhook-url "https://ntfy.sh/your-topic"
```

Fires on every monitor_down / monitor_up transition. Suppressed during
maintenance windows. Flap protection (`--flap-min-dwell SECS`, default 60)
prevents a bouncing peer from paging you 60 times an hour — the signed log
still records every transition for audit.

---

## Where state lives

User mode (default):

```
$XDG_CONFIG_HOME/panic-monitor/
    secret.key       sealed signing key (mode 0600)
    secret.meta      Node ID + argon2 salt (mode 0600)
    peers.json       materialized peer cache (rebuilt from log)
    log.jsonl        append-only signed trust log (the authority)
    password.cred    systemd-creds-encrypted password ciphertext

$XDG_DATA_HOME/panic-monitor/
    history.db       SQLite: probe latency + status timeseries
    logstore.db      SQLite: system/container stats + rollups

$XDG_RUNTIME_DIR/panic-monitor/
    control.sock     UNIX socket — daemon ↔ CLI IPC
```

When run as root, these become `/etc/panic-monitor`, `/var/lib/panic-monitor`,
`/run/panic-monitor`.

Override the config or data root explicitly via `PANIC_MONITOR_CONFIG_DIR` /
`PANIC_MONITOR_DATA_DIR`.

### Migrating legacy state

If you previously ran `--init` in a checkout directory and copied state
around manually:

```fish
panic-monitor --migrate
```

Copies state files from legacy locations (CWD, `/etc`, `/var/lib`) into the
resolved XDG/system paths. Source files are left in place; verify the daemon
comes up cleanly, then remove the originals.

---

## System (root) mode

Install as a system service instead of a user service when you want the daemon
running at boot regardless of who's logged in:

```fish
sudo panic-monitor --init                    # state lives in /etc/panic-monitor
sudo panic-monitor --install-service         # auto-detected as system mode
sudo systemctl status panic-monitor.service
```

System mode applies the full sandbox (kernel-protect, address-family
restriction, syscall filter — see [hardening](#daemon-permissions-and-hardening)),
and the password is encrypted with the host key so only this machine's
system-instance systemd can decrypt it.

---

## Troubleshooting

### `Failed to set up credentials: Wrong medium type` (status=243/CREDENTIALS)

The encrypted password was made with the wrong scope. User-mode systemd can't
decrypt a host-key credential. Rotate it:

```fish
systemctl --user reset-failed panic-monitor.service
panic-monitor --install-service --rotate-password --force
```

### `Failed to drop capabilities: Operation not permitted` (status=218/CAPABILITIES)

Hardening directive in the unit needs `CAP_SETPCAP` which user-mode systemd
lacks. Reinstall — recent builds emit the privileged hardening block only in
system mode:

```fish
panic-monitor --install-service --force
```

### `Failed to set up mount namespacing: ... No such file or directory` (status=226/NAMESPACE)

The unit's mount-sandbox can't find the state dir to bind-mount. Either the
state dir really is missing (run `--init` again) or you have a stale
`ProtectHome=tmpfs` in user mode. Reinstall:

```fish
panic-monitor --install-service --force
```

### `argon2.exceptions.HashingError: Threading failure` at startup

`pthread_create` returned EAGAIN. Almost always caused by `LimitNPROC=` in
the unit — `RLIMIT_NPROC` is **per-user** (counts *all* your processes),
not per-service. Newer builds use `TasksMax=` (cgroup-scoped) instead.
Reinstall:

```fish
panic-monitor --install-service --force
```

### Service crash-loops with `start-limit-hit`

Five failures in 30 seconds and systemd gives up. After fixing the underlying
issue:

```fish
systemctl --user reset-failed panic-monitor.service
systemctl --user start panic-monitor.service
```

### Dashboard returns `Connection refused` on 42069

The daemon isn't actually running, or the webapp failed to bind. Check both:

```fish
systemctl --user is-active panic-monitor.service
journalctl --user -u panic-monitor.service | grep webapp
```

### Peer shows alive but clicking shows no system stats

Both sides are running but the dashboard shows only the peer's liveness
status with no CPU/mem/disk/container data. This is almost always a
permissions gap — the remote peer hasn't granted you `view_dashboard` or
`monitor` on their trust log.

**Fix:** On every peer that should share stats, ensure the other node has
at least `monitor` permission (or `view_dashboard`). Restarting the
daemon is not required — the control-socket reload picks up new
permissions immediately:

```fish
panic-monitor --add-peer <OTHER_NODE_ID> --permissions monitor --force
```

After both sides have each other with `monitor`, stats should appear
within one `--stats-interval` (default 10 seconds).

### `daemon error: Invalid Node ID`

Node IDs are Curve25519 public keys, not just any 64-char hex. `--add-peer`
validates by attempting to construct an iroh `PublicKey` — use the value
your peer prints from `--show-identity`, not a hand-typed one.

### "Cache entry deserialization failed, entry ignored" during `pip install`

Harmless `pip` cache warning, unrelated to panic-monitor. Continues normally.

### Wipe everything and start fresh

```fish
panic-monitor --uninstall-service
systemctl --user reset-failed panic-monitor.service 2>/dev/null
rm -rf ~/.config/panic-monitor ~/.local/share/panic-monitor
panic-monitor --init
panic-monitor --install-service
```

---

## CLI reference

The authoritative list lives in `panic-monitor --help`. The most common flags:

| Flag | Purpose |
|---|---|
| `--init` | Generate identity (first time only) |
| `--show-identity` | Print this node's Node ID — no password |
| `--reset-password` | Re-seal the identity under a new password |
| `--install-service [--user\|--system] [--force] [--rotate-password]` | Render + enable the systemd unit |
| `--uninstall-service` | Remove the systemd unit |
| `--add-peer NID [--alias X] [--permissions C,S,V] [--tags X,Y]` | Trust a peer |
| `--revoke-peer NID_OR_ALIAS` | Revoke a peer (logged, not deleted) |
| `--list-peers [--filter-tag X]` | Show trusted peers |
| `--set-tags TARGET CSV` / `--add-tag` / `--remove-tag` | Tag ops |
| `--set-maintenance TARGET START END` / `--clear-maintenance` | Maintenance window |
| `--uptime TARGET [--window 24h]` | Uptime % over window |
| `--history TARGET [--hours 24]` | Raw probe stream |
| `--fetch-dashboard TARGET` | Pull a peer's dashboard once |
| `--daemon` | Run headless (for non-systemd installs) |
| `--tui` | Interactive textual UI |
| `--test-webhook --webhook-url …` | Smoke-test the webhook |
| `--role {monitored,monitoring,both}` | Node behavior (default: both) |
| `--dashboard-port PORT` | Flask dashboard port (0 disables; default 42069) |
| `--status-bind HOST:PORT` | Lightweight status page bind (empty disables; default 127.0.0.1:8080) |
| `--stats-interval SECS` | System stats + cross-device pull interval (default 10) |
| `--interval SECS` | Heartbeat probe interval (default 30) |
| `--down-after N` / `--up-after N` | Transition thresholds (default 3 / 1) |
| `--flap-min-dwell SECS` | Min seconds between webhook firings for the same peer (default 60) |
| `--password-from {systemd-creds,keyring,stdin,env,pinentry}` | Password backend |
| `--push-to NID` | Push heartbeat to this peer (repeatable; for behind-NAT setups) |
| `--no-docker` | Skip container stats collection |

### Non-systemd / Docker

Set `--password-from stdin` and pipe the password at container start:

```fish
echo "$PANIC_MONITOR_PASSWORD" | panic-monitor --daemon --password-from stdin
```

Set `PANIC_MONITOR_CONFIG_DIR` and `PANIC_MONITOR_DATA_DIR` to point at the
mounted volumes.

---

## Architecture notes

- **Log is the authority, peers.json is a cache.** Every mutation goes through
  a signed log entry. `peers.json` is re-materialized after each append.
- **Iroh handles NAT traversal.** No public IPs or port forwarding required.
- **Identity rotation = social.** Your Node ID is your Curve25519 public key;
  rotating it means a new identity + re-establishing trust with every peer.
- **History retention.** Probe history is pruned after `--retain-days` (default
  30). Container/stats raw snapshots are kept 2 hours; 5-minute buckets 30
  days; hourly + daily summaries indefinitely.
- **Concurrency model.** One asyncio loop owns iroh + the heartbeat scheduler;
  the control socket is a thread; the dashboards are threads. The trust log,
  peer trust manager, and SQLite stores are each guarded by their own lock.
  See `src/log.py:TrustLog._lock` and `src/trust.py:PeerTrustManager._lock`.
- **Dashboard is a single-page app.** `src/webapp.py` renders one static
  HTML shell at `GET /` and exposes `GET /api/dashboard` (live state) +
  `GET /api/container/<id>/logs` (lazy log tail). The page never reloads;
  all updates happen via diff-by-key DOM mutation so scroll position,
  hover state, and expanded container detail rows survive every refresh.
- **Stats collection.** `src/stats.py:StatsCollector.collect_all()` runs once
  per `--stats-interval` in a thread (off the asyncio loop). It returns
  system + containers (with block I/O, ports, mounts, health log) + top-N
  processes. Processes are live-only; nothing per-process touches SQLite.
