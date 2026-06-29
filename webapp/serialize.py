"""Serialize SiteRun lists to JSON and rebuild them later for re-rendering reports."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import main as engine


def _dt(value: datetime) -> str:
    return value.isoformat()


def _dt_back(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _serialize_command(cr: engine.CommandResult) -> dict[str, Any]:
    return {
        "template": cr.template,
        "command": cr.command,
        "started_at": _dt(cr.started_at),
        "ended_at": _dt(cr.ended_at),
        "return_code": cr.return_code,
        "stdout": cr.stdout,
        "stderr": cr.stderr,
        "error": cr.error,
        "throughput_mbps": cr.throughput_mbps,
        "throughput_label": cr.throughput_label,
        "retransmissions": cr.retransmissions,
        "sender_throughput_mbps": cr.sender_throughput_mbps,
        "receiver_throughput_mbps": cr.receiver_throughput_mbps,
    }


def _deserialize_command(data: dict[str, Any]) -> engine.CommandResult:
    return engine.CommandResult(
        template=data["template"],
        command=data["command"],
        started_at=_dt_back(data["started_at"]),
        ended_at=_dt_back(data["ended_at"]),
        return_code=data["return_code"],
        stdout=data["stdout"],
        stderr=data["stderr"],
        error=data.get("error"),
        throughput_mbps=data.get("throughput_mbps"),
        throughput_label=data.get("throughput_label"),
        retransmissions=data.get("retransmissions"),
        sender_throughput_mbps=data.get("sender_throughput_mbps"),
        receiver_throughput_mbps=data.get("receiver_throughput_mbps"),
    )


def _serialize_site(site: engine.SiteDefinition) -> dict[str, Any]:
    return {
        "index": site.index,
        "raw": site.raw,
        "placeholders": site.placeholders,
        "display_name": site.display_name,
        "ip_address": site.ip_address,
        "hub_ip": site.hub_ip,
        "speed": site.speed,
        "speed_mbps": site.speed_mbps,
        "accepted_speed": site.accepted_speed,
        "accepted_speed_mbps": site.accepted_speed_mbps,
        "speed_with_margin_mbps": site.speed_with_margin_mbps,
        "speed_with_margin_label": site.speed_with_margin_label,
        "hub_mgmt_ip": site.hub_mgmt_ip,
        "hub_server_intf": site.hub_server_intf,
        "spoke_client_intf": site.spoke_client_intf,
        "traffictest_port": site.traffictest_port,
        "hub_name": site.hub_name,
        "circuit_id": site.circuit_id,
        "isp": site.isp,
    }


def _deserialize_site(data: dict[str, Any]) -> engine.SiteDefinition:
    return engine.SiteDefinition(
        index=data["index"],
        raw=data.get("raw", {}),
        placeholders=data.get("placeholders", {}),
        display_name=data["display_name"],
        ip_address=data["ip_address"],
        hub_ip=data["hub_ip"],
        speed=data["speed"],
        speed_mbps=data.get("speed_mbps"),
        accepted_speed=data.get("accepted_speed", ""),
        accepted_speed_mbps=data.get("accepted_speed_mbps"),
        speed_with_margin_mbps=data.get("speed_with_margin_mbps"),
        speed_with_margin_label=data.get("speed_with_margin_label", ""),
        hub_mgmt_ip=data.get("hub_mgmt_ip", ""),
        hub_server_intf=data.get("hub_server_intf", ""),
        spoke_client_intf=data.get("spoke_client_intf", ""),
        traffictest_port=data.get("traffictest_port", ""),
        hub_name=data.get("hub_name", ""),
        circuit_id=data.get("circuit_id", ""),
        isp=data.get("isp", ""),
    )


def serialize_runs(
    runs: list[engine.SiteRun],
    summary: dict[str, Any],
    *,
    input_path: Path,
    command_templates: list[str],
    delay_seconds: int,
) -> dict[str, Any]:
    return {
        "version": 1,
        "input_name": input_path.name,
        "command_templates": command_templates,
        "delay_seconds": delay_seconds,
        "summary": summary,
        "runs": [
            {
                "site": _serialize_site(r.site),
                "started_at": _dt(r.started_at),
                "ended_at": _dt(r.ended_at),
                "delayed_after_seconds": r.delayed_after_seconds,
                "name_discovery_result": (
                    _serialize_command(r.name_discovery_result)
                    if r.name_discovery_result else None
                ),
                "command_results": [_serialize_command(c) for c in r.command_results],
            }
            for r in runs
        ],
    }


def deserialize_runs(data: dict[str, Any]) -> tuple[list[engine.SiteRun], dict[str, Any], list[str], int]:
    runs: list[engine.SiteRun] = []
    for r in data["runs"]:
        runs.append(
            engine.SiteRun(
                site=_deserialize_site(r["site"]),
                started_at=_dt_back(r["started_at"]),
                ended_at=_dt_back(r["ended_at"]),
                command_results=[_deserialize_command(c) for c in r["command_results"]],
                name_discovery_result=(
                    _deserialize_command(r["name_discovery_result"])
                    if r.get("name_discovery_result") else None
                ),
                delayed_after_seconds=r.get("delayed_after_seconds", 0),
            )
        )
    return runs, data.get("summary", {}), data.get("command_templates", []), data.get("delay_seconds", 0)
