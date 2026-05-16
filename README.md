# FortiGate Traffic Test Runner

This project contains a Python script, `main.py`, that runs FortiGate `diagnose traffictest` speed tests for multiple spokes from a CSV or Excel file. It discovers the firewall name over SSH, coordinates hub and spoke traffic-test commands, and writes an HTML report.

It is available in three forms:

- **CLI / interactive GUI** — `python main.py ...` (see [Basic Usage](#basic-usage)).
- **Web app** — a FastAPI front-end at `http://localhost:8800` (see [Web Application](#web-application)).
- **Docker container** — image bundling the web app (see [Docker](#docker)).

The web app and Docker image are thin wrappers around the same `main.py` engine — every CLI flag is exposed as a form field.

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

A FastAPI web front-end is included in [`webapp/`](webapp/). It mirrors every CLI option as a form field, streams live test output to the browser via Server-Sent Events, and serves the generated HTML/Excel/PDF reports.

### Run Locally

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn webapp.app:app --host 0.0.0.0 --port 8800
```

Then open <http://localhost:8800>.

Uploaded input files and generated reports are stored under `./data/` by default. Override the location with the `IPERF_DATA_DIR` environment variable.

### Pages

| Path | Purpose |
| --- | --- |
| `/` | New-run form (upload CSV/XLSX, fill SSH credentials, set overrides) |
| `/run/{id}` | Live log view for an in-flight run |
| `/run/{id}/stream` | SSE stream of stdout/stderr |
| `/run/{id}/status` | JSON status (used by the run page to fetch reports) |
| `/reports` | History of generated reports |
| `/reports/{file}` | Download / view a report file |
| `/healthz` | Liveness probe (returns `{"ok": true}`) |

### Concurrency

The web app accepts **one run at a time**. While a run is active, new submissions return HTTP 409. Within a run, the existing parallel model is preserved: hubs are set up in parallel, and each hub queue runs its spokes sequentially while different hubs run in parallel.

### No Auth

The web app does not authenticate. Bind it to a trusted network (`127.0.0.1` or an internal interface), or put it behind a reverse proxy that enforces auth. Note that SSH credentials are sent in the form body — terminate TLS at a reverse proxy if you expose it beyond localhost.

## Docker

A `Dockerfile` and `docker-compose.yml` are included.

### Build and run

```bash
docker compose up --build
```

Then open <http://localhost:8800>. Uploads and reports persist on the host under `./data/`.

### Network Reachability

The container needs network access to your hub and spoke firewalls (SSH on TCP 22 and the traffictest port, default TCP 5201). If your firewalls are only reachable from the host network, uncomment the `network_mode: host` line in [`docker-compose.yml`](docker-compose.yml) (note: `network_mode: host` is Linux-only; on macOS/Windows you must use a routable bridge instead).

### Standalone Docker (no compose)

```bash
docker build -t iperf-stress-test .
docker run --rm -p 8800:8800 -v "$(pwd)/data:/data" iperf-stress-test
```

### Environment

| Variable | Purpose | Default |
| --- | --- | --- |
| `IPERF_DATA_DIR` | Base directory for uploads and reports | `/data` (container) / `./data` (local) |
