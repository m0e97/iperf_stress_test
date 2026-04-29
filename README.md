# IPerf Stress Test Runner

This project contains a single Python script, `main.py`, that runs SD-WAN or firewall traffic tests for multiple sites from a CSV or Excel file. It runs the sites one by one, waits between sites, captures command output, extracts detected throughput, and writes an HTML report.

The script now gets the firewall name from the SSH connection instead of using the name from the Excel sheet.

## What The Script Does

For each row in the input file, `main.py`:

1. Reads the site IP address and expected speed.
2. Connects to the firewall over SSH to discover the real firewall name.
3. Uses the discovered firewall name in logs, command placeholders, and the HTML report.
4. Runs one or more traffic-test commands.
5. Parses command output for throughput values such as `Mbits/sec` or `Gbits/sec`.
6. Waits before moving to the next site.
7. Generates an HTML report with summary and per-site command details.

## Requirements

- Python 3.10 or newer.
- SSH access to the firewalls.
- Passwordless SSH, SSH keys, or an SSH setup that works without interactive prompts.
- Any traffic-test tool used by your command templates, such as `iperf3`.

No external Python packages are required.

## Input File

The input file can be `.csv` or `.xlsx`.

The script recognizes these column names, case-insensitively after normalizing spaces and symbols:

| Purpose | Accepted Column Names |
| --- | --- |
| Firewall IP | `ip`, `host`, `address`, `spoke_ip`, `branch_ip`, `wan_ip` |
| Speed | `speed`, `rate`, `bandwidth`, `expected_speed`, `speed_mbps`, `bandwidth_mbps` |

Name columns such as `name`, `site`, or `spoke_name` are no longer used as the final firewall name. The script uses the firewall name discovered through SSH.

Example CSV:

```csv
spoke_ip,speed
10.10.10.1,100M
10.10.20.1,200M
```

## Firewall Name Discovery

Before running the traffic test for each site, the script runs this SSH command by default:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {spoke_ip} "get system status"
```

It looks for output like:

```text
Hostname: FW-Riyadh-01
```

It also accepts a command that returns only the firewall name:

```text
FW-Riyadh-01
```

If your firewall uses a different command, pass your own command with `--firewall-name-command`.

Example:

```bash
python3 main.py \
  --input spokes.csv \
  --firewall-name-command 'ssh admin@{spoke_ip} "get system status"' \
  --command 'iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30'
```

## Placeholders

Command templates can use values from the input file and calculated values from the script.

Common placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{spoke_ip}` or `{ip}` | Firewall IP address from the input file |
| `{firewall_name}` | Firewall name discovered over SSH |
| `{hostname}` | Same as discovered firewall name |
| `{spoke_name}` | Same as discovered firewall name |
| `{site_name}` | Same as discovered firewall name |
| `{name}` | Same as discovered firewall name |
| `{speed}` | Speed from the input file |
| `{speed_mbps}` | Parsed speed in Mbps |
| `{speed_with_margin}` | Speed plus 15%, formatted for traffic commands, for example `115M` |
| `{speed_with_margin_mbps}` | Speed plus 15% as a number |
| `{site_index}` | Row number, starting from 1 |

## Basic Usage

Run one command for every firewall:

```bash
python3 main.py \
  --input spokes.csv \
  --command 'iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30'
```

Use an Excel file:

```bash
python3 main.py \
  --input spokes.xlsx \
  --sheet Sheet1 \
  --command 'iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30'
```

Write the report to a custom path:

```bash
python3 main.py \
  --input spokes.csv \
  --output reports/traffic_test_report.html \
  --command 'iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30'
```

## Multiple Commands

You can pass `--command` more than once:

```bash
python3 main.py \
  --input spokes.csv \
  --command 'iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30' \
  --command 'ping -c 5 {spoke_ip}'
```

Or use a command file:

```bash
python3 main.py \
  --input spokes.csv \
  --command-file commands.txt
```

Example `commands.txt`:

```text
# Blank lines and comments are ignored
iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30
ping -c 5 {spoke_ip}
```

## Useful Options

| Option | Description |
| --- | --- |
| `--input` | Required CSV or XLSX input file |
| `--sheet` | Worksheet name when using XLSX |
| `--command` | Command template to run for each site |
| `--command-file` | File containing command templates |
| `--firewall-name-command` | SSH command template used to discover firewall name |
| `--firewall-name-timeout` | Timeout for firewall name discovery, default `30` seconds |
| `--delay-seconds` | Delay between sites, default `120` seconds |
| `--timeout` | Timeout for each traffic-test command |
| `--output` | HTML report path, default `traffic_test_report.html` |
| `--dry-run` | Render commands and report without executing commands |

## Dry Run

Use `--dry-run` to verify command rendering before running real tests:

```bash
python3 main.py \
  --input spokes.csv \
  --dry-run \
  --command 'iperf3 -c {spoke_ip} -b {speed_with_margin} -t 30'
```

In dry-run mode, commands are not executed, so firewall names are not discovered. The report will use the IP address as the fallback display name.

## Report

The script writes an HTML report that includes:

- Total sites.
- Successful and failed sites.
- Total commands run.
- Peak detected throughput.
- Firewall name discovered over SSH.
- IP address.
- Configured speed.
- Test bandwidth with 15% margin.
- Command output, errors, return codes, and durations.

## Troubleshooting

If the firewall name is not discovered:

- Confirm SSH works manually for the same target.
- Make sure the SSH command does not require an interactive password prompt.
- Try a custom name command with `--firewall-name-command`.
- Make sure the command output contains `Hostname: firewall-name` or only the firewall name.

If a command placeholder fails:

- Check that the placeholder exists in the input file or in the supported placeholder list.
- Run with `--dry-run` to see rendered commands without executing them.

If the report shows failed commands:

- Open the HTML report and check each command block.
- Review stdout, stderr, return code, and timeout messages.
