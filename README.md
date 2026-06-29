# FortiGate Traffic Test Runner

This project contains a Python script, `main.py`, that runs FortiGate `diagnose traffictest` speed tests for multiple spokes from a CSV or Excel file. It discovers the firewall name over SSH, coordinates hub and spoke traffic-test commands, and writes HTML, XLSX, and PDF reports.

It is available in three forms:

- **CLI / interactive GUI** — `python main.py ...` (see [Basic Usage](#basic-usage)).
- **Web app** — FastAPI front-end at `http://localhost:8800` with a dashboard, persistent device catalog, scheduled runs, a filesystem-backed result archive, and on-demand report rendering (see [Web Application](#web-application)).
- **Docker container** — `docker compose up --build` runs the web app with the data directory bind-mounted from the host. In Kubernetes the same path is mounted as a PersistentVolume (see [Docker](#docker)).

The web app reuses the CLI engine under the hood — every flag still maps to a form field — and adds a UI layer with devices, schedules, an archive, and a throughput history chart.

## What The Script Does

1. Reads all rows from the input file and collects every unique hub IP.
2. Runs the hub setup commands on all hubs **in parallel**.
3. Starts the hub traffictest server on every hub **in parallel** (background process, one per hub).
4. Waits for the hub servers to be ready (default 60 seconds).
5. Groups spokes by their hub IP into per-hub queues.
6. Runs all hub queues **in parallel** — within each queue, spokes are tested one at a time so only one spoke is active against its hub server at any moment.
7. After all queues finish, stops every hub server.
8. Captures spoke-side results only and generates HTML, XLSX, and PDF reports.

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
diagnose traffictest run -b {speed_with_margin} -c {hub_ip}{duration_flag}
```

Each placeholder is filled in per row: `{hub_server_intf}`, `{spoke_client_intf}`, and `{traffictest_port}` come from the input file (see [Input File](#input-file)) or fall back to `--hub-server-intf` / `--spoke-client-intf` / `--traffictest-port`. `{speed_with_margin}` is the row's speed plus 15%, and `{hub_ip}` is the row's hub IP (or `--hub-ip` when set).

`{duration_flag}` controls the test length: when a row sets `traffictest_duration` (or you pass `--traffictest-duration`), it expands to ` -t <seconds>`, appended at the end of the run command. When neither is set it is empty, and FortiGate falls back to its built-in **10-second** default. A per-device value always wins over the run-wide flag.

All hub commands and all spoke commands each run in a single SSH shell session per device, so per-session settings such as `server-intf` and `client-intf` are preserved when the `run` command executes.

The hub server command is started in the background because `diagnose traffictest run -s` stays running while it waits for spoke clients. After all spoke queues finish, the script stops and discards the hub server output — only spoke-side results appear in the report.

## Requirements

- Python 3.10 or newer.
- SSH access to the hub and spoke firewalls.
- SSH must work without interactive prompts during the script run.
- FortiGate `diagnose traffictest` must be available on the firewalls.

No external Python packages are required for the core test. Install optional packages for extra output formats:

```bash
pip install paramiko     # pure-Python SSH (default on Windows)
pip install openpyxl     # Excel (.xlsx) report output
pip install reportlab    # PDF report output
```

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
| Test duration (seconds) | `traffictest_duration`, `test_duration`, `duration`, `duration_seconds`, `traffic_duration` |

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

To override the hub interface, spoke interface, traffic-test port, or test duration per row, add `server_intf`, `client_intf`, `traffictest_port`, and `traffictest_duration` columns. Rows that leave them blank fall back to `--hub-server-intf`, `--spoke-client-intf`, and `--traffictest-port` (defaults `Mobily`, `wan1`, and `5201`); a blank `traffictest_duration` means no `-t` is sent and FortiGate uses its built-in 10s default:

```csv
spoke_ip,hub_ip,hub_mgmt_ip,speed,server_intf,client_intf,traffictest_port,traffictest_duration
10.10.10.1,10.255.0.1,10.1.0.1,100M,STC,wan2,5300,30
10.10.20.1,10.255.0.1,10.1.0.1,200M,,,,
```

## Firewall Name Discovery

Before running the speed test for each spoke, the script connects over SSH and runs `get system status`. It looks for output like:

```text
Hostname: FW-Riyadh-01
```

The discovered name is used as the display name in the report. No output from this step appears in the report itself.

The same discovery runs for each hub firewall so the report can label the hub column as **"Hub Name (Hub IP)"** instead of a bare IP.

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

### Saved Credentials File

The GUI can save credentials to `credentials.json` next to `main.py` so you do not have to retype them on every run. Click **Save credentials** in the dialog to write the file and **Load credentials** to fill the fields from it. Pass `--sshuser` / `--sshpw` on the command line to override the saved file for a single run.

The **password field is encrypted at rest** using a Fernet key — see [Credential encryption](#credential-encryption) for how the key is resolved. `credentials.json` is also listed in `.gitignore` and is never committed.

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
diagnose traffictest run -b {speed_with_margin} -c {hub_ip}{duration_flag}
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
| `{traffictest_duration}` | Test duration in seconds (effective value: per-device, else `--traffictest-duration`, else empty) |
| `{duration_flag}` | ` -t <seconds>` when a duration is set, else empty — appended to the built-in run command |
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
| `--output` | Base report path (default: `Reports/traffic_test_report_YYYYMMDD_HHMMSS`) |
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

Each test run generates three report files saved to the `Reports/` folder, each with a timestamp in the filename (e.g. `traffic_test_report_20260615_143022`):

| Format | File | Requires |
| --- | --- | --- |
| HTML | `.html` | built-in |
| Excel | `.xlsx` | `pip install openpyxl` |
| PDF | `.pdf` | `pip install reportlab` |

If `openpyxl` or `reportlab` are not installed, the HTML report is still written and the missing-format error message includes the exact `pip install` command for the active Python interpreter.

### Summary table columns

| Column | Description |
| --- | --- |
| `#` | Row index |
| `Site name` | Firewall name discovered over SSH (falls back to spoke IP) |
| `Speed` | Configured speed from the input file |
| `IP` | Spoke IP address |
| `Hub` | Hub firewall name and IP as **"Hub Name (Hub IP)"** |
| `Generated traffic` | Bandwidth value sent in the traffictest command |
| `Actual bandwidth` | Peak measured throughput from the spoke-side output |
| `Started` | Timestamp when this spoke's test began |
| `Result` | **Pass** or **Fail (not reachable)** / **Fail (insufficient speed)** |

### Pass / Fail logic

A spoke **passes** when its measured **sender** throughput meets its acceptance threshold. The threshold is the device's `accepted_speed` when set, otherwise **90 % of the configured speed**. Otherwise it fails with a parenthetical reason:

- **Fail (not reachable)** — no throughput was captured (SSH or traffictest error).
- **Fail (insufficient speed)** — throughput was captured but fell below the threshold.

The same rule drives both the HTML report and the web dashboard's pass-rate card, and both compare the spoke's **sender-side** throughput, so the two never disagree.

The summary cards at the top of the HTML report are interactive: clicking **Successful sites** or **Failed sites** filters the table to show only that group.

### Per-site detail cards

Below the summary table, each spoke gets a detail card. The report header itself is intentionally minimal — just the title and the generation timestamp. Each card shows:

- **Reachable** — whether the SSH connection to the spoke succeeded (`Yes` / `No`).
- **Sender / Receiver Throughput** and **Total Retransmissions** from the traffictest run.
- The **traffictest run** command block, whose **Status** badge reflects the speed **Pass / Fail** result (the acceptance threshold above), not the raw command exit code.

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
- Review stdout, stderr, and timeout messages.

## Exit Code

The script exits with `0` when every spoke run succeeds and with `1` when at least one spoke has any failed or template-error command. The HTML report is written either way.

## Web Application

A FastAPI front-end in [`webapp/`](webapp/) wraps the CLI engine with a browser UI, a persistent device catalog, a filesystem-backed archive of historic results, and a built-in scheduler. The recommended deployment is `docker compose up` ([Docker](#docker) below) or a Kubernetes Deployment with a PersistentVolume; a local-only mode is available for development.

### Modes of running a test

| Mode | Path | When to use |
| --- | --- | --- |
| **Quick Run** | `/` (collapsed form) | One-off CSV / XLSX upload for an ad-hoc test. |
| **Run on selected devices** | `/devices` | Pick from saved devices and start a run immediately. |
| **Schedule** | `/schedules` | Recurring or one-shot fire at a future date/time. |

All three modes go through the same engine, so the live log, archive, and reports look identical regardless of how the run was started.

### Pages

| Path | Purpose |
| --- | --- |
| `/` | Dashboard — stat cards (devices, runs, schedules, device pass rate, last run) + recent runs table + collapsed Quick Run form |
| `/devices` | Persistent device catalog (add / edit / delete / import / run on selected) |
| `/devices/{id}/edit` | Edit one device |
| `/schedules` | List, toggle, edit, delete, manually fire scheduled runs |
| `/schedules/new`, `/schedules/{id}/edit` | Schedule form (once / daily / weekly / monthly / yearly) |
| `/archive` | Run-centric list of historic runs with start/finish time, devices tested, green/red pass-fail counts, and HTML/XLSX/PDF download links |
| `/isp-report` | **ISP Compliance Report** — pick an ISP + time window + SLA target and see, per circuit, how often each link met or missed its contracted speed (see [ISP Compliance Report](#isp-compliance-report)) |
| `/isp-report/export/{fmt}` | Download the current ISP report as `html` / `xlsx` / `pdf` |
| `/archive/device/{id}` | Per-device view: all runs for that device plus the throughput timeline chart (linked from the **History** column on the Devices page) |
| `/archive/run/{id}/render/{fmt}` | Render an archived run as `html` / `xlsx` / `pdf` on demand |
| `/run/{id}` | Live log view (SSE) for an in-flight or recent run, with a **Stop run** button while it is active |
| `/run/{id}/stream`, `/run/{id}/status` | SSE stream and JSON status, used by the run page |
| `/run/{id}/stop` | `POST` — request cooperative cancellation of the active run |
| `/jobs/active` | JSON snapshot of the active job (or the most recent finished run when idle), plus the last 6 runs as history. Powers the bottom run bar. |
| `/devices/template?format=csv\|xlsx` | Downloads a starter import file pre-filled with the canonical column names and one example row. |
| `/healthz` | Liveness probe — also reports the report archive directory path |

### Dashboard

The home page (`/`) shows five stat cards at a glance:

| Card | What it shows |
| --- | --- |
| **Devices** | Total count with passing / failing / untested pills based on each device's most recent result |
| **Total Runs** | Cumulative run count across all time |
| **Active Schedules** | Enabled schedules out of the total |
| **Device Pass Rate** | Fleet-wide health — the share of devices whose **latest result** (across all runs) meets its threshold. Recomputed against current thresholds, so editing a device's `accepted_speed`/`speed` is reflected immediately. |
| **Last Run** | Pass rate of the **single most recent run only** — i.e. just the devices included in that run. Uses the verdict the engine recorded at run time, so it always matches that run's report. |

> The two pass-rate cards answer different questions: **Device Pass Rate** is "how healthy is my whole fleet right now?", while **Last Run** is "how did the test I just kicked off do?". After a run that targets one device, Last Run reflects only that device, whereas Device Pass Rate still aggregates every device's latest known result.

Below the cards, a **Recent Runs** table lists the last 8 runs with status badge, pass and fail counts, and a link to the live log. The **Quick Run** form sits below in a collapsible `<details>` element.

### Devices catalog

Devices are stored in SQLite at `/data/app.db`. Each row mirrors the CLI input file columns plus two web-only fields:

| Field | Purpose |
| --- | --- |
| `spoke_ip` | Spoke firewall IP (required) |
| `hub_ip` | Hub IP for traffictest (required) |
| `hub_mgmt_ip` | Hub SSH management IP |
| `speed` | Configured link speed (e.g. `100M`) |
| `accepted_speed` | Pass/fail threshold override. Leave blank to auto-compute as **90 % of `speed`**. Shown as **auto** in the devices table when not set. |
| `circuit_id`, `isp` | Informational labels shown in the table and device picker |
| `server_intf`, `client_intf`, `traffictest_port` | Per-device interface / port overrides |
| `traffictest_duration` | Per-device test length in seconds (`-t`). Blank = FortiGate's built-in 10s default. |
| `notes` | Free-text notes |

Add devices manually from the **Add Device** modal, or bulk-import a CSV / XLSX — existing rows (matched by `spoke_ip + hub_ip`) are updated in place. CSV runs from the Quick Run page also get linked to their matching device by IP, so historic results show up in both flows.

If you don't have a file yet, the **Import** modal exposes two **↓ CSV template** / **↓ XLSX template** links (served from `/devices/template?format=…`). Both downloads ship with the same column headers the importer recognizes — `name`, `spoke_ip`, `hub_wan_ip`, `hub_mgmt_ip`, `speed`, `accepted_speed`, `server_intf`, `client_intf`, `traffictest_port`, `traffictest_duration`, `circuit_id`, `isp` — plus one example row you can edit or delete. The XLSX template needs `openpyxl` installed in the runner environment; without it the link redirects back with a banner pointing at the `pip install` command.

The devices page includes a live search bar and a multi-select flow (select → run modal). The device picker on the schedule form shows searchable cards with ISP / BW / circuit ID badges.

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

### ISP Compliance Report

The **Compliance** tab (`/isp-report`) turns the run history into a per-ISP SLA report for contract reviews and renewal negotiations. You pick:

- **ISP** — the report covers every device whose `isp` matches.
- **Time window** — presets (last 7 / 30 / 90 / 180 days, last 12 months) or a custom `From`/`To` date range.
- **SLA target %** — a test "meets" the contract when its **sender throughput ≥ SLA % of the device's contracted speed** (`speed`). The percentage is chosen per report, so you can model different negotiating positions (e.g. 80 % vs 95 %).

For every device/circuit it reports, over the window: number of tests scored, how many **met** vs **missed** the SLA, the **compliance %**, and throughput stats (min / avg / max and average % of contract achieved). Devices are ordered worst-compliance first, with the lowest performers highlighted. Aggregate cards show overall compliance, devices with data, and total met / not-met.

> A device can sit well below 100 % compliance even with a healthy-looking average — e.g. averaging 81 % of contract but never crossing a 90 % SLA line. That gap is exactly what the report surfaces for negotiation.

Devices with no `speed` configured are listed but left **unscored** (compliance shown as N/A), and devices with no runs in the window show 0 tests. The report can be downloaded as **HTML**, **Excel**, or **PDF** to attach to a contract discussion.

The report also includes:

- **Compliance trend** — the window is split into up to six contiguous sub-periods and each is scored, so you can see at a glance whether the link is **improving or declining** over the window (rendered as a small bar chart in the app and HTML/PDF, and as rows in Excel).
- **Per-device drill-down** — each device row links to that device's run history (`/archive/device/{id}`), where you can see every individual run and the throughput timeline behind the compliance number.
- **All-ISPs comparison** — choosing **★ All ISPs (comparison)** ranks every ISP against each other (overall compliance, devices with data, met / not-met) so you can compare providers at a glance, with a **details →** link to drill into each one. The comparison is itself exportable to HTML / Excel / PDF.

SSH passwords for schedules are **encrypted at rest** in `app.db` using a Fernet key — see [Credential encryption](#credential-encryption). The DB still holds the encrypted tokens, so back up the key file separately if you back up the data volume.

### Archive

`/archive` is the run-centric history page. Each row is one run with:

| Column | Description |
| --- | --- |
| `Run` | Run id, linked to the live log view (`/run/{id}`) |
| `Source` | How the run was kicked off (`csv`, `devices`, `schedule`) |
| `Started` / `Finished` | Timestamps |
| `Devices` | Number of devices tested |
| `Result` | **N pass** in green and **N fail** in red, from the run's stored summary |
| `Status` | Status badge (`done`, `error`, `cancelled`, …) |
| `Report` | Per-row **HTML / XLSX / PDF** download links rendered on demand from the report archive |

Per-device history (with the throughput timeline chart) is still reachable at `/archive/device/{id}` — the **History** column on the Devices page links to it.

Downloaded report files are named `Report_YYYYMMDD_HHMMSS.{ext}` using the run's start time. Runs that were fired from a schedule are prefixed with the (sanitized) schedule name — e.g. `Daily_Smoke_Report_20260517_123000.xlsx`.

#### Storage layout

Runs are archived to the local filesystem at **`$IPERF_DATA_DIR/reports/{run_id}.json`** (defaults to `<repo>/data/reports/` for local dev, `/data/reports/` in the container, and is meant to be a **PersistentVolume mount** in Kubernetes). The web app writes the raw `SiteRun` payload as a single JSON file per run, keyed by run id.

Reports are **not** pre-rendered — when you click **HTML / XLSX / PDF** on a historic run, the web app reads the JSON back from the archive directory and runs the report builders on the fly. Re-rendering with updated templates "just works" for all past runs.

The same volume also holds `app.db` (devices, runs, schedules) and `.secret.key` (the credential-encryption key, see [Credential encryption](#credential-encryption)). Back the volume up like any other stateful workload.

### Throughput timeline chart

The device archive page (`/archive/device/{id}`) shows a per-device throughput timeline:

- Y-axis: measured Mbps. X-axis: run timestamps (oldest → newest).
- Dashed green line marks the device's acceptance threshold (`accepted_speed`, or 90 % of the configured speed).
- Dots are **green if throughput ≥ threshold**, **red if below**. Hover for a tooltip with the timestamp and value.

The chart is inline SVG with theme-aware colors — no CDN dependency.

### App chrome

Every page shares the same chrome:

- **Sidebar** — Fortinet logomark (inline SVG, tinted via CSS so it follows the active theme) next to the "SD-WAN iPerf" wordmark, then a nav with line-icon links for **Dashboard**, **Devices**, **Archive**, and **Schedules**.
- **Top-right cluster** — a clock chip showing the current date, time, and `GMT+3` (rendered in `Asia/Riyadh` so it stays consistent regardless of the viewer's locale) sits next to a circular theme-toggle button. The chosen theme is persisted to `localStorage` and applied before paint so there's no flash on load.
- **Bottom run bar** — a fixed-position bar that polls `/jobs/active` every 2 s while a run is active (15 s when idle). It collapses to a single line by default and expands via a chevron on the right:
  - **Running** — pulsing dot, `N / total` progress, indeterminate slide while totals are unknown, the current site message, and a "View run" link. Expanded view adds the trailing 8 log lines and source/start metadata.
  - **Idle** — status pill for the last finished run, pass · fail counts, relative timestamp ("12m ago"), and a "View" link. Expanded view lists the last 6 runs with status, counts, and a per-run link.
  - **Empty** — bar stays hidden until the first run is recorded.

  The expanded/collapsed state is remembered in `localStorage`. Polling pauses while the tab is hidden.

### Concurrency

The web app accepts **one run at a time**. New submissions to `/run`, `/devices/run`, or `/schedules/{id}/run` return HTTP 409 while a run is active; scheduled fires record `skipped_busy`. Within a run, the engine's per-hub parallelism is preserved — multiple hubs run in parallel, each hub queue runs its spokes sequentially.

### Stopping a run

The run page shows a **Stop run** button while a run is active (`POST /run/{id}/stop`). Cancellation is **cooperative**: the engine checks between devices, so the device currently being tested finishes its iperf run, then no further devices start. The run ends with status **`cancelled`**, still archives a report for the devices that completed, and frees the slot so a new run can start.

### Auth

The web app does **not** authenticate. Bind it to a trusted network (`127.0.0.1` or an internal interface), or put it behind a reverse proxy that enforces auth. Terminate TLS at the reverse proxy and protect the `/data` volume at the filesystem level — even though credentials at rest are encrypted (see below), traffic over HTTP would carry them in cleartext.

### Credential encryption

Every SSH password that the app persists — both `credentials.json` (CLI/GUI saved creds) and the `sshpw` column on the `schedules` table — is encrypted with a per-deployment **Fernet key** before being written to disk. Public usernames stay readable.

A stored schedule password is also **never sent back to the browser**. When you edit a schedule the password field is rendered empty (not pre-filled), so the secret never appears in the page source / DevTools "Inspect". Leave it blank to keep the existing credential, or type a new value to replace it; the username (not secret) is still pre-filled for convenience.

The key is resolved on first use, in this order:

1. **`IPERF_SECRET_KEY`** env var (a urlsafe-base64 Fernet key, 44 chars). Recommended for Docker.
2. **`IPERF_SECRET_KEY_FILE`** env var (path to a file containing the key).
3. **`${IPERF_DATA_DIR}/.secret.key`** — auto-created on first boot with `0600` perms.
4. **`<repo>/data/.secret.key`** for CLI use when no `IPERF_DATA_DIR` is set.

Generate a key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Existing rows that were written before this version was deployed are migrated to ciphertext the first time the DB is opened, so no manual migration is needed.

The key file is gitignored. **Back it up separately from the data volume** — losing the key means losing every encrypted credential. Rotating the key is a manual exercise: pin the old value, decrypt, switch keys, re-save.

### Run locally (no Docker)

The local path is the same code as the container — it just writes the report archive under `<repo>/data/reports/` instead of the container/PV mount. Useful for poking at the UI without Docker.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn webapp.app:app --host 0.0.0.0 --port 8800
```

Then open <http://localhost:8800>. Override the data location with `IPERF_DATA_DIR=/some/path`.

## Docker

A [`Dockerfile`](Dockerfile) and [`docker-compose.yml`](docker-compose.yml) are included. Compose runs the single `iperf-runner` service with `./data` bind-mounted at `/data` — that directory holds the SQLite DB, uploaded inputs, the `reports/` archive, and the credential encryption key.

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
| `IPERF_DATA_DIR` | Base directory for uploads, the SQLite DB, and the `reports/` archive | `/data` (container), `./data` (local) |
| `IPERF_SECRET_KEY` | Fernet key used to encrypt SSH credentials at rest. Set this in production so the key isn't co-located with the encrypted data. | _(auto-generated key file)_ |
| `IPERF_SECRET_KEY_FILE` | Path to a file containing the Fernet key (alternative to `IPERF_SECRET_KEY`). | _(unset)_ |
| `IPERF_TZ_OFFSET` | App-wide timezone offset in hours used for every timestamp (run ids, run started/finished, report times, schedule next/last run, and the UI clock). Lets a UTC server show local time. e.g. `0` for UTC, `5.5` for IST, `-4` for EDT. | `3` (GMT+3, Riyadh) |

## Tests

A `pytest` regression suite lives in [`tests/`](tests/). Run it after **any** code change to confirm nothing that already worked has broken.

### Install test dependencies (once)

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
```

### Run

```bash
.venv/Scripts/python -m pytest
```

The tests are hermetic: `tests/conftest.py` points `IPERF_DATA_DIR` at a throwaway temp directory before importing the app, so your real `data/` (DB, reports, credentials) is never touched, and no network or SSH access is required.

### What is covered

| File | Area | Why it matters |
| --- | --- | --- |
| `test_scheduler.py` | `compute_next_run` for once/daily/weekly/monthly/yearly + the fire path | Wrong next-run math means schedules fire at the wrong time or never; includes a guard against the startup-callable signature bug |
| `test_progress.py` | `JobState` `[n/m]` progress parsing | Keeps the run-bar percentage in sync with console progress (incl. the Windows `\r` regression) |
| `test_engine.py` | `parse_speed_to_mbps`, `_compute_result`, `summarize`, format helpers | These drive every pass/fail decision and report number |
| `test_report.py` | `build_html_report` output | Valid HTML + report theme colors don't silently regress |
| `test_db.py` | Devices / runs / schedules persistence | CRUD + credential encryption at rest |
| `test_app_api.py` | `/healthz`, `/jobs/active`, run-ID format | Endpoints respond; run names stay `YYYYMMDD-HHMMSS` in Riyadh time |
