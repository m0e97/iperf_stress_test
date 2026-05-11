# FortiGate Traffic Test Runner

This project contains a Python script, `main.py`, that runs FortiGate `diagnose traffictest` speed tests for multiple spokes from a CSV or Excel file. It discovers the firewall name over SSH, coordinates hub and spoke traffic-test commands, and writes an HTML report.

## What The Script Does

1. Reads all rows from the input file and collects every unique hub IP.
2. Runs the hub setup commands on all hubs **in parallel**.
3. Starts the hub traffictest server on every hub **in parallel** (background process, one per hub).
4. Waits for the hub servers to be ready (default 60 seconds).
5. Groups spokes by their hub IP into per-hub queues.
6. Runs all hub queues **in parallel** — within each queue, spokes are tested one at a time so only one spoke is active against its hub server at any moment.
7. Each spoke test runs for a fixed duration (default 2 minutes).
8. After all queues finish, stops every hub server.
9. Captures spoke-side results only and generates an HTML report.

## Built-In Speed Test Commands

When you do not pass `--command` or `--command-file`, the script uses the built-in FortiGate flow below.

Hub commands run once per hub before any spoke tests:

```text
diagnose traffictest server-intf {hub_server_intf}
diagnose traffictest port {traffictest_port}
diagnose traffictest run -s
```

Spoke commands run for each spoke in its hub queue:

```text
diagnose traffictest client-intf {spoke_client_intf}
diagnose traffictest port {traffictest_port}
diagnose traffictest run -b {speed_with_margin} -c {hub_ip} -t {traffictest_duration}
```

Each placeholder is filled in per row: `{hub_server_intf}`, `{spoke_client_intf}`, and `{traffictest_port}` come from the input file (see [Input File](#input-file)) or fall back to `--hub-server-intf` / `--spoke-client-intf` / `--traffictest-port`. `{speed_with_margin}` is the row's speed plus 15%, `{hub_ip}` is the row's hub IP (or `--hub-ip` when set), and `{traffictest_duration}` is the test duration in seconds (default `120`).

The hub server command is started in the background because `diagnose traffictest run -s` stays running while it waits for spoke clients. After all spoke queues finish, the script stops and discards the hub server output — only spoke-side results appear in the report.

## Requirements

- Python 3.10 or newer.
- SSH access to the hub and spoke firewalls.
- SSH must work without interactive prompts during the script run.
- FortiGate `diagnose traffictest` must be available on the firewalls.

No external Python packages are required.

## Input File

The input file can be `.csv` or `.xlsx`.

The script recognizes these column names, case-insensitively after normalizing spaces and symbols:

| Purpose | Accepted Column Names |
| --- | --- |
| Spoke IP | `ip`, `host`, `address`, `spoke_ip`, `branch_ip`, `wan_ip` |
| Hub IP | `hub_ip`, `hub`, `hub_host`, `hub_address`, `hub_wan_ip` |
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
spoke_ip,hub_ip,speed,server_intf,client_intf,traffictest_port
10.10.10.1,10.255.0.1,100M,STC,wan2,5300
10.10.20.1,10.255.0.1,200M,,,
```

## Firewall Name Discovery

Before running the speed test for each spoke, the script runs this SSH command by default:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {spoke_ip} "get system status"
```

It looks for output like:

```text
Hostname: FW-Riyadh-01
```

If your login needs a username, pass a custom command:

```bash
--firewall-name-command 'ssh admin@{spoke_ip} "get system status"'
```

## Basic Usage

Use a CSV file with `spoke_ip`, `hub_ip`, and `speed`:

```bash
python3 main.py --input spokes.csv
```

Use one hub IP for all spokes:

```bash
python3 main.py \
  --input spokes.csv \
  --hub-ip 10.255.0.1
```

Use an Excel file:

```bash
python3 main.py \
  --input spokes.xlsx \
  --sheet Sheet1 \
  --hub-ip 10.255.0.1
```

## SSH Credentials

Use `--sshuser` and `--sshpw` to supply credentials without customizing the SSH template manually:

```bash
python3 main.py \
  --input spokes.csv \
  --sshuser admin \
  --sshpw mypassword
```

When `--sshpw` is provided the script automatically uses `sshpass` to supply the password non-interactively, so `sshpass` must be installed on the machine running the script. When only `--sshuser` is given, standard key-based authentication is used with the username prepended.

These flags override both the built-in SSH template and the firewall name discovery command. If you need further control (custom port, identity file, etc.) use `--ssh-template` and `--firewall-name-command` directly.

## SSH Username Or Options

The built-in FortiGate speed-test commands use this SSH wrapper by default:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {target} "{remote_command}"
```

If you need a username without a password, you can use `--sshuser` (see above) or customize the template:

```bash
python3 main.py \
  --input spokes.csv \
  --hub-ip 10.255.0.1 \
  --ssh-template 'ssh admin@{target} "{remote_command}"'
```

## Interfaces And Port

Defaults:

| Setting | Default |
| --- | --- |
| Hub server interface | `Mobily` |
| Spoke client interface | `wan1` |
| Traffic-test port | `5201` |

The hub interface, spoke interface, and traffic-test port can also come from the input file using the `server_intf`, `client_intf`, and `traffictest_port` columns (see the table above for accepted aliases). The CLI flags below act as a fallback for any row that leaves those columns empty.

Override them like this:

```bash
python3 main.py \
  --input spokes.csv \
  --hub-ip 10.255.0.1 \
  --hub-server-intf Mobily \
  --spoke-client-intf wan1 \
  --traffictest-port 5201
```

## Speed Value

The script reads `speed` from the input file, converts it to Mbps, adds 15%, and uses that value in the spoke command:

```text
diagnose traffictest run -b {speed_with_margin} -c {hub_ip} -t {traffictest_duration}
```

Example:

| Input Speed | Command Bandwidth |
| --- | --- |
| `100M` | `115M` |
| `200M` | `230M` |
| `1G` | `1150M` |

## Test Duration

Each spoke test runs for a fixed duration set by `--traffictest-duration` (default `120` seconds / 2 minutes). The `-t` flag is passed directly to `diagnose traffictest run` on the spoke. After the duration elapses the spoke command exits automatically and the script moves to the next spoke in the queue.

## Placeholders

Command templates can use these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{spoke_ip}` or `{ip}` | Spoke firewall IP address |
| `{hub_ip}` or `{hub}` | Hub firewall IP address |
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
| `{traffictest_duration}` | Test duration in seconds (default `120`) |
| `{site_index}` | Row number, starting from 1 |

## Custom Commands

If you pass `--command` or `--command-file`, the script runs your custom commands instead of the built-in FortiGate speed-test flow. Custom command mode runs sites sequentially.

Example:

```bash
python3 main.py \
  --input spokes.csv \
  --command 'ssh admin@{spoke_ip} "get system status"'
```

You can pass `--command` more than once, and commands run in the order provided.

To keep a long list of templates in a file, use `--command-file`:

```bash
python3 main.py \
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
| `--input` | Required CSV or XLSX input file |
| `--sheet` | Worksheet name when using XLSX |
| `--hub-ip` | One hub IP to use for all spokes |
| `--sshuser` | SSH username prepended to every target |
| `--sshpw` | SSH password supplied via `sshpass` |
| `--ssh-template` | SSH wrapper for built-in hub/spoke traffictest commands |
| `--hub-server-intf` | Hub interface for `server-intf`, default `Mobily` |
| `--spoke-client-intf` | Spoke interface for `client-intf`, default `wan1` |
| `--traffictest-port` | FortiGate traffictest port, default `5201` |
| `--traffictest-duration` | Duration in seconds for each spoke test, default `120` |
| `--hub-server-start-delay` | Seconds to wait after starting all hub servers before running spokes, default `60.0` |
| `--firewall-name-command` | SSH command template used to discover spoke firewall name |
| `--firewall-name-timeout` | Timeout for firewall name discovery, default `30` seconds |
| `--delay-seconds` | Delay between spokes within the same hub queue, default `120` seconds |
| `--timeout` | Timeout for each foreground command |
| `--output` | HTML report path, default `traffic_test_report.html` |
| `--dry-run` | Render commands and report without executing commands |

## Dry Run

Use `--dry-run` to verify command rendering before running real tests:

```bash
python3 main.py \
  --input spokes.csv \
  --hub-ip 10.255.0.1 \
  --dry-run
```

In dry-run mode, commands are not executed, so firewall names are not discovered. The report uses the spoke IP as the fallback display name.

## Report

The HTML report includes:

- Total spokes.
- Successful and failed spokes.
- Total commands run.
- Peak detected throughput.
- Discovered firewall name.
- Spoke IP and hub IP.
- Configured speed.
- Test bandwidth with 15% margin.
- Spoke command output, return codes, errors, and durations.

Hub-side command output is not included in the report.

## Troubleshooting

If the firewall name is not discovered:

- Confirm SSH works manually for the spoke.
- Make sure SSH does not require an interactive password prompt.
- Try a custom name command with `--firewall-name-command`.
- Make sure the output contains `Hostname: firewall-name` or only the firewall name.

If the built-in speed test does not run:

- Confirm every row has `hub_ip`, or pass `--hub-ip`.
- Confirm every row has a valid `speed`.
- Confirm the hub interface is really `Mobily`.
- Confirm the spoke interface is really `wan1`.
- Confirm TCP port `5201` is allowed between hub and spoke.

If the report shows failed commands:

- Open the HTML report and check each command block.
- Review stdout, stderr, return code, and timeout messages.

## Exit Code

The script exits with `0` when every spoke run succeeds and with `1` when at least one spoke has any failed or template-error command. The HTML report is written either way.
