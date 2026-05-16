# Panic Monitor

A zero-infrastructure, peer-to-peer health monitoring daemon. It replaces centralized uptime monitors with a flat-peer mesh, using Iroh for direct QUIC connections and append-only, cryptographically signed trust logs for access control.

With v2, it features comprehensive system and container resource monitoring, local SQLite log stores, P2P gossip-based metric streaming, and offline gap handling.

## Prerequisites

- Linux (tested on modern distributions)
- Python 3.12+
- Systemd (for daemon management)
- Optional but recommended: Docker (for container stats)

## Installation

Clone the repository and install it locally:

```bash
git clone <repository_url> panic-monitr
cd panic-monitr
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Initialization

Before the daemon can run, you must initialize the node's cryptographic identity. This generates an Ed25519 keypair sealed with an Argon2d password.

```bash
panic-monitor --init
```
You will be prompted to set a password. Store this password securely.

To view your newly generated Node ID:
```bash
panic-monitor --show-identity
```

## Trusting Peers

Panic Monitor uses a default-deny model. To monitor a peer, you must add their Node ID to your trust log and grant them the `monitor` permission.

```bash
panic-monitor --add-peer <NODE_ID> --alias "api-server" --permissions "monitor"
```

To allow a peer to pull your dashboard data:
```bash
panic-monitor --add-peer <NODE_ID> --alias "ops-laptop" --permissions "view_dashboard"
```

## Roles

A node can operate in one of three roles, controlled by the `--role` flag (default: `both`):
- `monitored`: Collects local system/container metrics and serves as the source of truth, storing data in a local log store.
- `monitoring`: Consumes metrics from monitored peers to render the TUI and Web UI dashboards. Requests syncs to fill gaps if it goes offline.
- `both`: Acts as both a monitored agent and a monitoring dashboard.

## Running the Daemon

The standard deployment method is via systemd. 

1. Create the configuration directory:
```bash
sudo mkdir -p /etc/panic-monitor /var/lib/panic-monitor
```

2. Copy your local state files to the system paths (assuming you ran `--init` and `--add-peer` in the current directory):
```bash
sudo cp secret.key secret.meta peers.json log.jsonl /etc/panic-monitor/
sudo chown -R root:root /etc/panic-monitor
sudo chmod 600 /etc/panic-monitor/secret.key
```

3. Provide your password to the daemon securely:
```bash
echo "PANIC_MONITOR_PASSWORD=your-password-here" | sudo tee /etc/panic-monitor/env > /dev/null
sudo chmod 600 /etc/panic-monitor/env
```

4. Install and start the service:
```bash
sudo cp panic-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now panic-monitor
```

## Webhooks

To receive notifications on state transitions (UP/DOWN), append the `--webhook-url` flag to the `ExecStart` line in the systemd service, or pass it when running manually:

```bash
panic-monitor --daemon --webhook-url "https://ntfy.sh/your-topic"
```

## Maintenance Windows

If you need to take a node down without triggering alerts, schedule a maintenance window:

```bash
# Silence alerts for the next 2 hours
panic-monitor --set-maintenance "api-server" +0 +2h
```

## TUI and Dashboard

To interactively view the status of all monitored peers, use the btop-inspired Terminal UI:
```bash
panic-monitor --tui
```

The daemon exposes a rich Flask + Plotly dashboard for interactive visualizations of CPU, Memory, Disk, Network, and Uptime over time. It handles network disconnects by distinguishing between offline gaps vs. true outages.
By default, it binds to port `42069`.
```bash
http://127.0.0.1:42069/
```

To fetch a remote peer's dashboard over the P2P mesh (requires `view_dashboard` permission):
```bash
panic-monitor --fetch-dashboard <NODE_ID>
```

### Additional Configuration Flags

- `--role {monitored,monitoring,both}`: Defines node behavior (default: `both`).
- `--dashboard-port PORT`: Port for the Flask web dashboard (default: `42069`).
- `--stats-interval SECS`: System stats collection interval in seconds (default: `10`).
- `--logstore-db PATH`: Path to the server-side SQLite logstore DB.
- `--no-docker`: Disables Docker container stats collection.
