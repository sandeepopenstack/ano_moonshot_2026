"""Mock DetectiveAgent provider aligned to the /investigate output schema.

The real DetectiveAgent is an external service. This mock exists only for local
tests and converts a Reflex triage payload into the Doc2 RCA payload consumed by
EngineerAgent.
"""

from datetime import datetime, timezone


DOMAIN_DEFAULTS = {
    "RAN": {
        "change_request_id": "CR-SYN-002",
        "root_cause": "antenna_tilt_misconfiguration",
        "incident_type": "RAN_PARAMETER_PUSH",
        "risk_score": 0.4,
        "reversibility_score": 0.95,
        "estimated_ttr_minutes": 130,
        "affected_hex_bins": ["87283472bffffff", "87283472affffff"],
        "causal_parameters": {
            "parameter": "antenna_tilt_degrees",
            "previous_value": 47.1,
            "current_value": 42.1,
            "unit": "degrees",
        },
        "recovery_targets": [
            {
                "target_metric": "dl_throughput_mbps",
                "target_value": 50.0,
                "current_value": 8.0,
                "tolerance_pct": 10.0,
            },
            {
                "target_metric": "rrc_setup_success_rate",
                "target_value": 99.5,
                "current_value": 60.0,
                "tolerance_pct": 5.0,
            },
        ],
        "suggested_remediation": [
            {"option": "A", "action": "revert_antenna_tilt", "param": "antenna_tilt"},
            {"option": "B", "action": "adjust_neighbor_antenna_tilt", "param": "antenna_tilt"},
            {"option": "C", "action": "accept_degradation"},
        ],
    },
    "CORE": {
        "change_request_id": "CR-SYN-001",
        "root_cause": "hss_subscriber_db_saturation",
        "incident_type": "FAILOVER_MIGRATION",
        "risk_score": 0.6,
        "reversibility_score": 0.7,
        "estimated_ttr_minutes": 80,
        "affected_hex_bins": [],
        "causal_parameters": {
            "parameter": "session_count",
            "previous_value": 45000,
            "current_value": 98000,
            "unit": "sessions",
        },
        "recovery_targets": [
            {
                "target_metric": "attach_success_rate",
                "target_value": 99.8,
                "current_value": 72.0,
                "tolerance_pct": 5.0,
            },
            {
                "target_metric": "cpu_utilization_pct",
                "target_value": 35.0,
                "current_value": 92.0,
                "tolerance_pct": 10.0,
            },
        ],
        "suggested_remediation": [
            {"option": "A", "action": "clear_stale_hss_sessions"},
            {"option": "B", "action": "shift_traffic_to_secondary_hss"},
            {"option": "C", "action": "accept_degradation"},
        ],
    },
}


def _entity_id(value):
    if isinstance(value, dict):
        return value.get("node_id") or value.get("eid") or value.get("id")
    return value


def _infer_domain(domain_triage: str, entity_ids: list[str]) -> str:
    domain = (domain_triage or "").upper()
    if domain in DOMAIN_DEFAULTS:
        return domain
    if any(e.upper().startswith(("HSS", "MME", "AMF", "SMF", "UPF")) for e in entity_ids):
        return "CORE"
    return "RAN"


def _hypothesis_id(change_request_id: str) -> str:
    suffix = change_request_id.split("-")[-1] if change_request_id else "000"
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"RCH-{day}-{suffix}"


def _with_targets(suggestions: list[dict], entity_ids: list[str]) -> list[dict]:
    primary = entity_ids[0] if entity_ids else ""
    secondary = entity_ids[1] if len(entity_ids) > 1 else primary
    output = []
    for suggestion in suggestions:
        item = dict(suggestion)
        if item["option"] == "A":
            item.setdefault("target", primary)
            item.setdefault("note", "Primary remediation recommended by RCA")
        elif item["option"] == "B":
            item.setdefault("target", secondary)
            item.setdefault("note", "Fallback remediation if primary action is insufficient")
        else:
            item.setdefault("target", "")
            item.setdefault("note", "Accept only if the change was intentional")
        output.append(item)
    return output


def generate_detective_output(reflex_payload: dict) -> dict:
    """Return a Doc2-compatible investigation.rca.confirmed payload."""
    entity_ids = [
        eid for eid in (_entity_id(e) for e in reflex_payload.get("entity_ids", []))
        if eid
    ]
    ranked_nodes = reflex_payload.get("ranked_list") or []
    if not entity_ids:
        entity_ids = [
            eid for eid in (_entity_id(e) for e in ranked_nodes)
            if eid
        ]

    domain = _infer_domain(reflex_payload.get("domain_triage", ""), entity_ids)
    cfg = DOMAIN_DEFAULTS[domain]
    change_request_id = cfg["change_request_id"]
    now = datetime.now(timezone.utc).isoformat()
    primary = entity_ids[0] if entity_ids else ""

    return {
        "eventId": reflex_payload.get("eventId", ""),
        "hypothesis_id": _hypothesis_id(change_request_id),
        "root_cause": cfg["root_cause"],
        "root_cause_description": f"{cfg['incident_type']} on {primary or 'unknown entity'}",
        "timestamp_of_cause": reflex_payload.get("reference_time") or now,
        "domain": domain,
        "confidence": "HIGH",
        "confidence_score": 0.85 if domain == "RAN" else 0.90,
        "severity": reflex_payload.get("priority_flag", "P1"),
        "businessPriority": reflex_payload.get("priority", "CRITICAL"),
        "incident_type": cfg["incident_type"],
        "change_type_name": cfg["incident_type"],
        "change_request_id": change_request_id,
        "risk_score": cfg["risk_score"],
        "reversibility_score": cfg["reversibility_score"],
        "estimated_ttr_minutes": cfg["estimated_ttr_minutes"],
        "primary_resource": {
            "node_id": primary,
            "type": domain,
        },
        "causal_parameters": {
            **cfg["causal_parameters"],
            "change_source": f"change_request_{change_request_id}",
        },
        "affected_entities": entity_ids,
        "neighbor_entities": reflex_payload.get("affected_neighbor_enodebs", []),
        "affected_cells": entity_ids if domain == "RAN" else [],
        "alarm_ids": [],
        "affected_hex_bins": cfg["affected_hex_bins"],
        "confirmedRcaBranches": [],
        "suggested_remediation": _with_targets(cfg["suggested_remediation"], entity_ids),
        "evidence_chain": [
            f"GNN ranked entities: {entity_ids}",
            f"Domain triage confirmed: {domain}",
            f"RCA matched: {cfg['root_cause']}",
            f"Change request lineage: {change_request_id}",
        ],
        "recovery_targets": cfg["recovery_targets"],
        "kpi_impact": {
            "primary_metric": cfg["recovery_targets"][0]["target_metric"],
            "primary_metric_delta_pct": -84.0 if domain == "RAN" else -27.8,
            "secondary_impacts": {},
        },
        "impact_score": reflex_payload.get("impact_score", 0.94),
        "criticality_score": reflex_payload.get("criticality_score", 1.0),
        "criticality_label": reflex_payload.get("criticality_label", "CRITICAL"),
        "investigation_timestamp": now,
        "source": "DetectiveAgent_Mock",
    }
