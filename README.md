# FortiGate Traffic Test Runner

This project contains a Python script, `main.py`, that runs FortiGate `diagnose traffictest` speed tests for multiple spokes from a CSV or Excel file. It discovers the firewall name over SSH, starts the hub as the traffic-test server, runs the spoke as the client, captures output, and writes an HTML report.

## What The Script Does

For each spoke row, the script:

1. Reads the spoke IP, hub IP, and expected speed.
2. Connects to the spoke over SSH to discover the firewall name.
3. Runs the hub-side `diagnose traffictest` server commands.
4. Runs the spoke-side `diagnose traffictest` client commands.
5. Extracts detected throughput from command output.
6. Waits before moving to the next spoke.
7. Generates an HTML report.

## Built-In Speed Test Commands

When you do not pass `--command` or `--command-file`, the script uses the built-in FortiGate flow below.

Hub commands run first:

```text
diagnose traffictest server-intf Mobily
diagnose traffictest port 5201
diagnose traffictest run -s
```

Spoke commands run after the hub server starts:

```text
diagnose traffictest client-intf wan1
diagnose traffictest port 5201
diagnose traffictest run -b {speed_with_margin} -c {hub_ip}
```

The hub server command is started in the background because `diagnose traffictest run -s` can stay running while it waits for the spoke client. After the spoke commands finish, the script stops and collects the hub server output.

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

Name columns such as `name`, `site`, or `spoke_name` are not used as the final firewall name. The script uses the firewall name discovered from SSH.

Example CSV:

```csv
spoke_ip,hub_ip,speed
10.10.10.1,10.255.0.1,100M
10.10.20.1,10.255.0.1,200M
```

You can also provide one hub IP for all rows with `--hub-ip`.

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

## SSH Username Or Options

The built-in FortiGate speed-test commands use this SSH wrapper by default:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {target} "{remote_command}"
```

If you need a username, customize it:

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
| `{hub_ip}` or `{hub}` | Hub firewall IP address |
| `{firewall_name}` | Firewall name discovered over SSH |
| `{hostname}` | Same as discovered firewall name |
| `{spoke_name}` | Same as discovered firewall name |
| `{site_name}` | Same as discovered firewall name |
| `{name}` | Same as discovered firewall name |
| `{speed}` | Speed from the input file |
| `{speed_mbps}` | Parsed speed in Mbps |
| `{speed_with_margin}` | Speed plus 15%, formatted for FortiGate, for example `115M` |
| `{speed_with_margin_mbps}` | Speed plus 15% as a number |
| `{hub_server_intf}` | Hub server interface |
| `{spoke_client_intf}` | Spoke client interface |
| `{traffictest_port}` | Traffic-test port |
| `{site_index}` | Row number, starting from 1 |

## Custom Commands

If you pass `--command` or `--command-file`, the script runs your custom commands instead of the built-in FortiGate speed-test flow.

Example:

```bash
python3 main.py \
  --input spokes.csv \
  --command 'ssh admin@{spoke_ip} "get system status"'
```

You can pass `--command` more than once, and commands run in the order provided.

## Useful Options

| Option | Description |
| --- | --- |
| `--input` | Required CSV or XLSX input file |
| `--sheet` | Worksheet name when using XLSX |
| `--hub-ip` | One hub IP to use for all spokes |
| `--ssh-template` | SSH wrapper for built-in hub/spoke traffictest commands |
| `--hub-server-intf` | Hub interface for `server-intf`, default `Mobily` |
| `--spoke-client-intf` | Spoke interface for `client-intf`, default `wan1` |
| `--traffictest-port` | FortiGate traffictest port, default `5201` |
| `--hub-server-start-delay` | Seconds to wait after starting hub server, default `2.0` |
| `--firewall-name-command` | SSH command template used to discover spoke firewall name |
| `--firewall-name-timeout` | Timeout for firewall name discovery, default `30` seconds |
| `--delay-seconds` | Delay between spokes, default `120` seconds |
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
- Hub and spoke command output.
- Return codes, errors, and durations.

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
