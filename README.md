# FortiGate Traffic Test Runner

This project contains a Python script, `main.py`, that runs FortiGate `diagnose traffictest` speed tests for multiple spokes from a CSV or Excel file. It discovers the firewall name over SSH, coordinates hub and spoke traffic-test commands, and writes an HTML report.

It is available in three forms:

- **CLI / interactive GUI** — `python main.py ...` (see [Basic Usage](#basic-usage)).
- **Web app** — FastAPI front-end at `http://localhost:8800` with a persistent device catalog, scheduled runs, FTP-backed result archive, and on-demand HTML / XLSX / PDF rendering (see [Web Application](#web-application)).
- **Docker container** — `docker compose up --build` brings up the web app and the FTP archive together (see [Docker](#docker)).

The web app reuses the CLI engine under the hood — every flag still maps to a form field — and adds a UI layer with devices, schedules, an archive, and a throughput history chart.

## What The Script Does

1. Reads all rows from the input file and collects every unique hub IP.
2. Runs the hub setup commands on all hubs **in parallel**.
3. Starts the hub traffictest server on every hub **in parallel** (background process, one per hub).
4. Waits for the hub servers to be ready (default 60 seconds).
5. Groups spokes by their hub IP into per-hub queues.
6. Runs all hub queues **in parallel** — within each queue, spokes are tested one at a time so only one spoke is active against its hub server at any moment.
7. After all queues finish, stops every hub server.
8. Captures spoke-side results only and generates an HTML report.

## Built-In Speed Test Commands

When you do not pass `--command` or `--command-file`, the script uses the built-in FortiGate flow below.

Hub commands run once per hub before any spoke tests, all within a single SSH session. `config global` is entered once at login before any commands run:

```text
config global
diagnose traffictest server-intf {hub_server_intf}
diagnose traffictest port {traffictest_port}
diagnose traffictest run -s
```

Spoke commands run for each spoke in its hub queue, all within a single SSH session per spoke:

```text
diagnose traffictest client-intf {spoke_client_intf}
diagnose traffictest port {traffictest_port}
diagnose traffictest run -b {speed_with_margin} -c {hub_ip}
```

Each placeholder is filled in per row: `{hub_server_intf}`, `{spoke_client_intf}`, and `{traffictest_port}` come from the input file (see [Input File](#input-file)) or fall back to `--hub-server-intf` / `--spoke-client-intf` / `--traffictest-port`. `{speed_with_margin}` is the row's speed plus 15%, and `{hub_ip}` is the row's hub IP (or `--hub-ip` when set).

All hub commands and all spoke commands each run in a single SSH shell session per device, so per-session settings such as `server-intf` and `client-intf` are preserved when the `run` command executes.

The hub server command is started in the background because `diagnose traffictest run -s` stays running while it waits for spoke clients. After all spoke queues finish, the script stops and discards the hub server output — only spoke-side results appear in the report.

## Requirements

- Python 3.10 or newer.
- SSH access to the hub and spoke firewalls.
- SSH must work without interactive prompts during the script run.
- FortiGate `diagnose traffictest` must be available on the firewalls.

No external Python packages are required unless you use `--paramiko` (which is the default on Windows).

## Input File

The input file can be `.csv` or `.xlsx`. The default file name is `devices.csv` — running `python main.py` with no arguments will look for `devices.csv` in the current directory.

The script recognizes these column names, case-insensitively after normalizing spaces and symbols:

| Purpose | Accepted Column Names |
| --- | --- |
| Spoke IP | `ip`, `host`, `address`, `spoke_ip`, `branch_ip`, `wan_ip` |
| Hub IP (traffictest target) | `hub_ip`, `hub`, `hub_host`, `hub_address`, `hub_wan_ip` |
| Hub Management IP (SSH) | `hub_mgmt_ip`, `hub_management_ip`, `hub_ssh_ip`, `hub_admin_ip`, `hub_mgmt` |
| Speed | `speed`, `rate`, `bandwidth`, `expected_speed`, `speed_mbps`, `bandwidth_mbps` |
| Hub server interface | `server_intf`, `hub_server_intf`, `hub_intf`, `hub_interface`, `server_interface` |
| Spoke client interface | `client_intf`, `spoke_client_intf`, `spoke_intf`, `spoke_interface`, `client_interface`, `wan_intf`, `wan_interface` |
| Traffic-test port | `traffictest_port`, `traffic_port`, `iperf_port`, `test_port` |

Name columns such as `name`, `site`, or `spoke_name` are not used as the final firewall name. The script uses the firewall name discovered from SSH.

Example CSV:

```csv
spoke_ip,hub_ip,speed
10.10.10.1,10.255.0.1,100M
10.10.20.1,10.255.0.1,200M
10.10.30.1,10.255.1.1,100M
```

In this example the script finds two unique hubs (`10.255.0.1` and `10.255.1.1`), sets up both in parallel, then tests the first two spokes against hub 1 and the third spoke against hub 2 simultaneously.

You can also provide one hub IP for all rows with `--hub-ip`.

To override the hub interface, spoke interface, or traffic-test port per row, add `server_intf`, `client_intf`, and `traffictest_port` columns. Rows that leave them blank fall back to `--hub-server-intf`, `--spoke-client-intf`, and `--traffictest-port` (defaults `Mobily`, `wan1`, and `5201`):

```csv
spoke_ip,hub_ip,hub_mgmt_ip,speed,server_intf,client_intf,traffictest_port
10.10.10.1,10.255.0.1,10.1.0.1,100M,STC,wan2,5300
10.10.20.1,10.255.0.1,10.1.0.1,200M,,,
```

## Firewall Name Discovery

Before running the speed test for each spoke, the script connects over SSH and runs `get system status`. It looks for output like:

```text
Hostname: FW-Riyadh-01
```

The discovered name is used as the display name in the report. No output from this step appears in the report itself.

If your login needs a username, pass it via `--sshuser` or customize the command:

```bash
--firewall-name-command 'ssh admin@{spoke_ip} "get system status"'
```

## Basic Usage

Run with no arguments to open the interactive GUI:

```bash
python main.py
```

A dialog appears asking for the input file (defaults to `devices.csv`), SSH username, and SSH password. After you click **OK**, a progress window opens and streams all test output in real time. A **Close** button appears when the run finishes.

Or pass arguments directly to skip the GUI entirely:

```bash
python main.py --input spokes.csv --sshuser admin --sshpw mypassword
```

Use one hub IP for all spokes:

```bash
python main.py --input spokes.csv --hub-ip 10.255.0.1
```

Use an Excel file:

```bash
python main.py --input spokes.xlsx --sheet Sheet1 --hub-ip 10.255.0.1
```

## SSH Credentials

Use `--sshuser` and `--sshpw` to supply credentials on the command line:

```bash
# Password as argument
python main.py --input spokes.csv --sshuser admin --sshpw mypassword

# Interactive prompt (characters hidden)
python main.py --input spokes.csv --sshuser admin --sshpw
SSH password:
```

When running with no arguments, the GUI dialog collects the username and password. The password field is masked.

## Pure-Python SSH (Paramiko)

Paramiko is used by default on all platforms — no external `ssh` or `sshpass` executables are needed.

Install Paramiko in the same Python environment as the script:

```bash
pip install paramiko
```

### Post-Logon Banner

Some FortiGate devices display a disclaimer banner immediately after login. When Paramiko is used, the script detects the banner automatically and sends `a` to accept it before running any commands. No manual intervention is needed.

## Skipping Hub Setup

If you have already started the hub traffictest server manually, use `--skip-hub-setup` to skip all hub SSH commands and run only the spoke-side test:

```bash
python main.py --input spokes.csv --sshuser admin --sshpw --skip-hub-setup
```

## SSH Username Or Options

The built-in FortiGate speed-test commands use this SSH wrapper by default (on Linux/macOS):

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {target} "{remote_command}"
```

To customize:

```bash
python main.py \
  --input spokes.csv \
  --ssh-template 'ssh admin@{target} "{remote_command}"'
```

## Interfaces And Port

Defaults:

| Setting | Default |
| --- | --- |
| Hub server interface | `Mobily` |
| Spoke client interface | `wan1` |
| Traffic-test port | `5201` |

The hub interface, spoke interface, and traffic-test port can also come from the input file using the `server_intf`, `client_intf`, and `traffictest_port` columns. The CLI flags below act as a fallback for any row that leaves those columns empty.

Override them like this:

```bash
python main.py \
  --input spokes.csv \
  --hub-ip 10.255.0.1 \
  --hub-server-intf Mobily \
  --spoke-client-intf wan1 \
  --traffictest-port 5201
```

## Speed Value

The script reads `speed` from the input file, converts it to Mbps, adds 15%, and uses that value in the spoke command:

```text
diagnose traffictest run -b {speed_with_margin} -c {hub_ip}
```

Example:

| Input Speed | Command Bandwidth |
| --- | --- |
| `100M` | `115M` |
| `200M` | `230M` |
| `1G` | `1150M` |

## Placeholders

Command templates can use these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{spoke_ip}` or `{ip}` | Spoke firewall IP address |
| `{hub_ip}` or `{hub}` | Hub firewall IP address (traffictest target) |
| `{firewall_name}` | Firewall name discovered over SSH |
| `{hostname}` | Same as discovered firewall name |
| `{device_name}` | Same as discovered firewall name |
| `{spoke_name}` | Same as discovered firewall name |
| `{site_name}` | Same as discovered firewall name |
| `{name}` | Same as discovered firewall name |
| `{speed}` or `{expected_speed}` | Speed from the input file |
| `{speed_mbps}` | Parsed speed in Mbps |
| `{speed_with_margin}` or `{bandwidth_with_margin}` | Speed plus 15%, formatted for FortiGate, for example `115M` |
| `{speed_with_margin_mbps}` | Speed plus 15% as a number |
| `{hub_server_intf}` | Hub server interface |
| `{spoke_client_intf}` | Spoke client interface |
| `{traffictest_port}` or `{traffic_port}` | Traffic-test port |
| `{traffictest_duration}` | Test duration in seconds (for custom commands) |
| `{site_index}` | Row number, starting from 1 |

## Custom Commands

If you pass `--command` or `--command-file`, the script runs your custom commands instead of the built-in FortiGate speed-test flow. Custom command mode runs sites sequentially.

Example:

```bash
python main.py \
  --input spokes.csv \
  --command 'ssh admin@{spoke_ip} "get system status"'
```

You can pass `--command` more than once, and commands run in the order provided.

To keep a long list of templates in a file, use `--command-file`:

```bash
python main.py \
  --input spokes.csv \
  --command-file commands.txt
```

Inside the file, blank lines and lines starting with `#` are ignored:

```text
# health check
ssh admin@{spoke_ip} "get system status"
# routing snapshot
ssh admin@{spoke_ip} "get router info routing-table all"
```

## Useful Options

| Option | Description |
| --- | --- |
| `--input` | CSV or XLSX input file (default: `devices.csv`) |
| `--sheet` | Worksheet name when using XLSX |
| `--hub-ip` | Hub IP for traffictest (spoke target). If omitted, each row must have a `hub_ip` column |
| `--hub-mgmt-ip` | Hub management IP for SSH (setup commands). Falls back to `hub_ip` when omitted |
| `--sshuser` | SSH username prepended to every target |
| `--sshpw [PASSWORD]` | SSH password as a value, or omit the value to be prompted invisibly |
| `--paramiko` | Use Paramiko (pure-Python SSH) instead of external `ssh`/`sshpass` executables. Default on Windows |
| `--skip-hub-setup` | Skip all hub SSH commands; assumes the hub traffictest server is already running |
| `--ssh-template` | SSH wrapper for built-in hub/spoke traffictest commands (Linux/macOS only) |
| `--hub-server-intf` | Hub interface for `server-intf`, default `Mobily` |
| `--spoke-client-intf` | Spoke interface for `client-intf`, default `wan1` |
| `--traffictest-port` | FortiGate traffictest port, default `5201` |
| `--hub-server-start-delay` | Seconds to wait after starting all hub servers before running spokes, default `60.0` |
| `--firewall-name-command` | SSH command template used to discover spoke firewall name |
| `--firewall-name-timeout` | Timeout for firewall name discovery, default `30` seconds |
| `--delay-seconds` | Delay between spokes within the same hub queue, default `0` seconds |
| `--timeout` | Timeout for each foreground SSH command |
| `--output` | HTML report path, default `traffic_test_report.html` |
| `--dry-run` | Render commands and report without executing commands |

## Dry Run

Use `--dry-run` to verify command rendering before running real tests:

```bash
python main.py \
  --input spokes.csv \
  --hub-ip 10.255.0.1 \
  --dry-run
```

In dry-run mode, commands are not executed, so firewall names are not discovered. The report uses the spoke IP as the fallback display name.

## Report

The HTML report includes:

- Total, successful, and failed sites.
- Peak detected throughput across all sites.
- Per-site: discovered firewall name, IP, hub IP, configured speed, test bandwidth, status badge, peak throughput, retransmissions, duration.
- Spoke command output, return codes, errors, and durations.
- Hub setup and server command results (when hub setup is not skipped).

## Troubleshooting

If the firewall name is not discovered:

- Confirm SSH works manually for the spoke.
- Make sure SSH does not require an interactive password prompt.
- Try a custom name command with `--firewall-name-command`.
- Make sure the output contains `Hostname: firewall-name` or only the firewall name.

If the built-in speed test does not run:

- Confirm every row has `hub_ip`, or pass `--hub-ip`.
- Confirm every row has a valid `speed`.
- Confirm the hub interface is correct (default `Mobily`).
- Confirm the spoke interface is correct (default `wan1`).
- Confirm TCP port `5201` is allowed between hub and spoke.

If Paramiko is not available on Windows:

```bash
pip install paramiko
```

If the report shows failed commands:

- Open the HTML report and check each command block.
- Review stdout, stderr, return code, and timeout messages.

## Exit Code

The script exits with `0` when every spoke run succeeds and with `1` when at least one spoke has any failed or template-error command. The HTML report is written either way.

## Web Application

A FastAPI front-end in [`webapp/`](webapp/) wraps the CLI engine with a browser UI, a persistent device catalog, a FTP-backed archive of historic results, and a built-in scheduler. The recommended deployment is `docker compose up` ([Docker](#docker) below); a local-only mode is available for development.

### Modes of running a test

| Mode | Path | When to use |
| --- | --- | --- |
| **Quick Run** | `/` | One-off CSV / XLSX upload for an ad-hoc test. |
| **Run on selected devices** | `/devices` | Pick from saved devices and start a run immediately. |
| **Schedule** | `/schedules` | Recurring or one-shot fire at a future date/time. |

All three modes go through the same engine, so the live log, archive, and reports look identical regardless of how the run was started.

### Pages

| Path | Purpose |
| --- | --- |
| `/` | Quick Run from a CSV / XLSX file |
| `/devices` | Persistent device catalog (add / edit / delete / import / run on selected) |
| `/devices/{id}/edit` | Edit one device |
| `/schedules` | List, toggle, edit, delete, manually fire scheduled runs |
| `/schedules/new`, `/schedules/{id}/edit` | Schedule form (once / daily / weekly / monthly / yearly) |
| `/archive` | Device-centric list of all devices that have run history |
| `/archive/device/{id}` | All runs for a device, with the throughput timeline chart |
| `/archive/run/{id}/render/{fmt}` | Render an archived run as `html` / `xlsx` / `pdf` on demand |
| `/run/{id}` | Live log view (SSE) for an in-flight or recent run |
| `/run/{id}/stream`, `/run/{id}/status` | SSE stream and JSON status, used by the run page |
| `/healthz` | Liveness probe — also reports FTP reachability |

### Devices catalog

Devices are stored in SQLite at `/data/app.db`. Each row mirrors the CLI input file columns (spoke IP, hub IP, hub mgmt IP, speed, server intf, client intf, traffictest port). Add devices manually from the **Add Device** modal, or bulk-import a CSV / XLSX — existing rows (matched by `spoke_ip + hub_ip`) are updated in place. CSV runs from the Quick Run page also get linked to their matching device by IP, so historic results show up in both flows.

### Schedules

Scheduled tasks fire through the same code path as the manual "Run on selected" button. Supported patterns:

| Pattern | Example | Notes |
| --- | --- | --- |
| `once` | `2026-12-31T10:00` | Auto-disables itself after firing. |
| `daily` | every day at `09:00` | Rolls to tomorrow's HH:MM after firing. |
| `weekly` | Mon, Wed, Fri at `08:30` | Multiple checked days; picks the next selected weekday. |
| `monthly` | day `15` at `09:00` | Day-of-month clamps to the last day in shorter months (e.g. `31` → Feb 28). |
| `yearly` | `December 31` at `00:00` | Month + day-of-month with the same clamping rule. |

A background poller (every 30 s) queries the schedules table for rows with `next_run_at <= now`. If a run is already active when a schedule fires, the attempt is recorded as `skipped_busy` and the next fire is still advanced.

**SSH credentials are stored in plaintext** in `/data/app.db` because the scheduler needs to fire unattended. Protect the data volume at the filesystem level. The schedule form shows a banner reminding you.

### Archive (FTP)

Runs are archived to a separate **FTP container** (`garethflowers/ftp-server`) on an internal docker network. The web app uploads the raw `SiteRun` data as a single JSON file per run, keyed by run id. Reports are **not** pre-rendered — when you click **HTML / XLSX / PDF** on a historic run, the web app fetches the JSON from FTP and runs the report builders on the fly. The upside: re-rendering with updated templates "just works" for all past runs.

The FTP server uses fixed credentials (`archive` / `archive`) and is **not exposed to the host** by default — only the runner container can reach it over the bridge network. Change the credentials in [`docker-compose.yml`](docker-compose.yml) if you publish the FTP port.

### Throughput timeline chart

The device archive page (`/archive/device/{id}`) shows a per-device throughput timeline:

- Y-axis: measured Mbps. X-axis: run timestamps (oldest → newest).
- Dashed green line marks the device's configured target speed.
- Dots are **green if throughput ≥ target**, **red if below**. Hover for a tooltip with the timestamp and value.

The chart is inline SVG with theme-aware colors — no CDN dependency.

### Theme

A circular icon button in the top-right corner toggles between dark and light themes. The choice is persisted to `localStorage` and applied before paint so there's no flash on page load.

### Concurrency

The web app accepts **one run at a time**. New submissions to `/run`, `/devices/run`, or `/schedules/{id}/run` return HTTP 409 while a run is active; scheduled fires record `skipped_busy`. Within a run, the engine's per-hub parallelism is preserved — multiple hubs run in parallel, each hub queue runs its spokes sequentially.

### Auth

The web app does **not** authenticate. Bind it to a trusted network (`127.0.0.1` or an internal interface), or put it behind a reverse proxy that enforces auth. SSH credentials and the schedule database are unencrypted on the wire and on disk, so terminate TLS at a reverse proxy and protect the `/data` volume at the filesystem level.

### Run locally (no Docker)

The local path skips the FTP archive (and therefore the on-demand re-render flow). Useful for poking at the UI; not the recommended deployment.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn webapp.app:app --host 0.0.0.0 --port 8800
```

Then open <http://localhost:8800>. Override the data location with `IPERF_DATA_DIR=/some/path`.

## Docker

A [`Dockerfile`](Dockerfile) and [`docker-compose.yml`](docker-compose.yml) are included. Compose is the recommended way to run — it brings up both the web app and the FTP archive on a shared internal network.

### Build and run

```bash
docker compose up --build
```

Then open <http://localhost:8800>. The SQLite database, uploaded inputs, and archived run JSON all live under `./data/` on the host.

### Network reachability

The container needs network access to your hub and spoke firewalls (SSH on TCP 22 and the traffictest port, default TCP 5201). If your firewalls are only reachable from the host network, uncomment the `network_mode: host` line in [`docker-compose.yml`](docker-compose.yml) (Linux only; on macOS / Windows use a routable bridge instead, or run uvicorn directly on the host).

### Environment

| Variable | Purpose | Default |
| --- | --- | --- |
| `IPERF_DATA_DIR` | Base directory for uploads and the SQLite DB | `/data` (container), `./data` (local) |
| `FTP_HOST` | FTP archive hostname | `ftp-archive` (compose service name) |
| `FTP_PORT` | FTP archive port | `21` |
| `FTP_USER`, `FTP_PASS` | FTP credentials | `archive` / `archive` |
| `FTP_TIMEOUT` | FTP socket timeout (seconds) | `20` |
