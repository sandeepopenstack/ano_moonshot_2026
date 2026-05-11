import uuid
from datetime import datetime, timezone


_DOMAIN_DEFAULTS = {
    "RAN": {
        "incident_type":      "RAN_PARAMETER_PUSH",
        "change_type_name":   "RAN_PARAMETER_PUSH",
        "change_request_id":  "CR-SYN-002",
        "root_cause":         "antenna_tilt_misconfiguration",
        "risk_score":         0.4,
        "reversibility_score":0.95,
        "corrective_action":  "Rollback antenna tilt from 12deg to 4deg on affected eNodeBs",
        "parameter_name":     "RollbackTiltParameters",
        "estimated_ttr_minutes": 130,
        "causal_parameters": {
            "parameter":      "antenna_tilt_degrees",
            "previous_value": 47.1,
            "current_value":  42.1,
        },
        "recovery_targets": [
            {"target_metric": "dl_throughput_mbps", "target_value": 50.0, "current_value": 8.0, "tolerance_pct": 10.0},
            {"target_metric": "rrc_setup_success_rate", "target_value": 99.5, "current_value": 60.0, "tolerance_pct": 5.0},
        ],
        "kpi_delta_pct": -84.0,
    },
    "CORE": {
        "incident_type":      "FAILOVER_MIGRATION",
        "change_type_name":   "FAILOVER_MIGRATION",
        "change_request_id":  "CR-SYN-001",
        "root_cause":         "hss_subscriber_db_saturation",
        "risk_score":         0.6,
        "reversibility_score":0.7,
        "corrective_action":  "Clear stale sessions on HSS to force clean re-attachment",
        "parameter_name":     "ClearStaleSessions",
        "estimated_ttr_minutes": 80,
        "causal_parameters": {
            "parameter":      "hss_active_sessions",
            "previous_value": 45000,
            "current_value":  98500,
        },
        "recovery_targets": [
            {"target_metric": "attach_success_rate", "target_value": 99.8, "current_value": 72.0, "tolerance_pct": 5.0},
            {"target_metric": "cpu_utilization_pct", "target_value": 35.0, "current_value": 92.0, "tolerance_pct": 10.0},
        ],
        "kpi_delta_pct": -27.8,
    },
    "TRANSPORT": {
        "incident_type":      "FIBER_CUT",
        "change_type_name":   "FIBER_CUT",
        "change_request_id":  "CR-SYN-003",
        "root_cause":         "physical_fiber_cut_backhaul",
        "risk_score":         0.5,
        "reversibility_score":0.9,
        "corrective_action":  "Reroute traffic from affected AGG to redundant backhaul path",
        "parameter_name":     "RerouteBackhaulTraffic",
        "estimated_ttr_minutes": 110,
        "causal_parameters": {
            "parameter":      "transport_link_status",
            "previous_value": "UP",
            "current_value":  "DOWN",
        },
        "recovery_targets": [
            {"target_metric": "link_utilization_pct", "target_value": 40.0, "current_value": 100.0, "tolerance_pct": 10.0},
            {"target_metric": "packet_loss_rate", "target_value": 0.01, "current_value": 45.0, "tolerance_pct": 1.0},
        ],
        "kpi_delta_pct": -100.0,
    },
}

_CROSS_DOMAIN_DEFAULTS = {
    "incident_type":     "MULTI_DOMAIN_REMEDIATION",
    "change_type_name":  "MULTI_DOMAIN_REMEDIATION",
    "root_cause":        "multi_domain_service_degradation",
}


def _priority_score_from_position(position: int) -> int:
    return {0: 10, 1: 7, 2: 4}.get(position, max(1, 4 - position))


def _pick_domain_config(domain_triage: str, entity_ids: list[str]) -> dict:
    if domain_triage in _DOMAIN_DEFAULTS:
        return _DOMAIN_DEFAULTS[domain_triage]
    for eid in entity_ids:
        u = eid.upper()
        if any(x in u for x in ("HSS", "MME", "AMF", "UPF", "SMF")):
            return _DOMAIN_DEFAULTS["CORE"]
        if any(x in u for x in ("CSR", "AGG", "TRANSPORT", "LINK", "FIBER")):
            return _DOMAIN_DEFAULTS["TRANSPORT"]
    return _DOMAIN_DEFAULTS["RAN"]


def _build_rca_branches_from_gnn_ranking(
    ranked_nodes: list[dict],
    entity_ids:      list[str],
    domain_triage:   str,
) -> list[dict]:
    if not ranked_nodes:
        return []

    domain_entity_map: dict[str, list[str]] = {}
    for eid in entity_ids:
        u = eid.upper()
        if any(x in u for x in ("HSS", "MME", "AMF", "UPF", "SMF", "CORE")):
            domain_entity_map.setdefault("CORE", []).append(eid)
        elif any(x in u for x in ("CSR", "AGG", "TRANSPORT", "LINK", "BACKHAUL")):
            domain_entity_map.setdefault("TRANSPORT", []).append(eid)
        elif any(x in u for x in ("ENB", "GNB", "CELL", "SECTOR", "RAN")):
            domain_entity_map.setdefault("RAN", []).append(eid)
        else:
            domain_entity_map.setdefault("RAN", []).append(eid)

    seen_domains: list[str] = []
    for ranked_node in ranked_nodes:
        node_id = ranked_node.get("node_id", "").upper()
        if any(x in node_id for x in ("LINK", "TRANSPORT", "AGG", "FIBER")):
            d = "TRANSPORT"
        elif any(x in node_id for x in ("HSS", "MME", "AMF", "SMF", "UPF")):
            d = "CORE"
        else:
            d = "RAN"
        if d not in seen_domains:
            seen_domains.append(d)

    if not seen_domains:
        seen_domains = [domain_triage if domain_triage in _DOMAIN_DEFAULTS else "RAN"]

    branches = []
    for position, domain in enumerate(seen_domains):
        cfg         = _DOMAIN_DEFAULTS.get(domain, _DOMAIN_DEFAULTS["RAN"])
        target_ents = domain_entity_map.get(domain, entity_ids)
        branches.append({
            "action_id":          chr(65 + position),
            "domain":             domain,
            "root_cause":         cfg["root_cause"],
            "priority_score":     _priority_score_from_position(position),
            "risk_score":         cfg["risk_score"],
            "reversibility":      cfg["reversibility_score"],
            "recommended_action": cfg["corrective_action"],
            "causal_parameters":  cfg["causal_parameters"],
            "target_entities":    target_ents,
        })

    return branches


def _build_suggested_remediation(domain: str, cfg: dict, entity_ids: list[str]) -> list[dict]:
    primary   = entity_ids[0] if entity_ids else ""
    secondary = entity_ids[1] if len(entity_ids) > 1 else primary

    if domain == "RAN":
        return [
            {"option": "A", "action": "revert_antenna_tilt", "target": primary, "param": "antenna_tilt", "value": cfg["causal_parameters"].get("previous_value"), "direction": "", "note": "Revert to pre-change state, target dl_throughput_mbps restored"},
            {"option": "B", "action": "adjust_neighbor_antenna_tilt", "target": secondary, "param": "antenna_tilt", "direction": "compensate", "note": "Adjust neighbor to absorb overflow from primary node"},
            {"option": "C", "action": "accept_degradation", "target": "", "note": "Accept if RAN_PARAMETER_PUSH was intentional"},
        ]
    if domain == "CORE":
        return [
            {"option": "A", "action": "clear_stale_hss_sessions", "target": primary, "value": 50000, "note": "Clear stale sessions to restore attach capacity"},
        ]
    if domain == "TRANSPORT":
        return [
            {"option": "A", "action": "activate_backup_transport_path", "target": primary, "value": "backup_path_01", "note": "Switch to redundant backhaul path"},
        ]
    return []


def generate_detective_output(reflex_payload: dict) -> dict:
    raw_entity_ids = reflex_payload.get("entity_ids", [])

    entity_ids = [
        e["node_id"] if isinstance(e, dict) else e
        for e in raw_entity_ids
    ]

    domain_triage      = reflex_payload.get("domain_triage", "RAN")
    priority           = reflex_payload.get("priority", "CRITICAL")
    impact_score       = reflex_payload.get("impact_score", 0.94)
    criticality_score  = reflex_payload.get("criticality_score", 1.0)
    criticality_label  = reflex_payload.get("criticality_label", "CRITICAL")

    ranked_nodes = reflex_payload.get("ranked_list", [])

    hypothesis_id = f"RCH-{str(uuid.uuid4())[:8].upper()}"
    now           = datetime.now(timezone.utc).isoformat()

    if "CROSS_DOMAIN" in domain_triage:
        rca_branches = _build_rca_branches_from_gnn_ranking(
            ranked_nodes, entity_ids, domain_triage
        )

        primary_domain = (
            ranked_nodes[0].get("domain", "RAN")
            if ranked_nodes else "RAN"
        )
        primary_cfg = _DOMAIN_DEFAULTS.get(primary_domain, _DOMAIN_DEFAULTS["RAN"])

        all_recovery_targets: list[dict] = []
        seen_metrics: set[str] = set()
        for b in rca_branches:
            b_cfg = _DOMAIN_DEFAULTS.get(b.get("domain", "RAN"), _DOMAIN_DEFAULTS["RAN"])
            for rt in b_cfg.get("recovery_targets", []):
                if rt["target_metric"] not in seen_metrics:
                    all_recovery_targets.append(rt)
                    seen_metrics.add(rt["target_metric"])

        if rca_branches:
            avg_risk = sum(
                _DOMAIN_DEFAULTS.get(b.get("domain", "RAN"), _DOMAIN_DEFAULTS["RAN"])["risk_score"]
                for b in rca_branches
            ) / len(rca_branches)
            avg_rev = sum(
                _DOMAIN_DEFAULTS.get(b.get("domain", "RAN"), _DOMAIN_DEFAULTS["RAN"])["reversibility_score"]
                for b in rca_branches
            ) / len(rca_branches)
        else:
            avg_risk = 0.5
            avg_rev  = 0.8

        suggested = [
            {
                "option":  b.get("action_id", chr(65 + i)),
                "action":  b.get("recommended_action", ""),
                "target":  b.get("target_entities", [entity_ids[0] if entity_ids else ""])[0],
                "domain":  b.get("domain", ""),
                "note": f"{b.get('domain', '')} remediation — priority {b.get('priority_score', 0)}",
            }
            for i, b in enumerate(rca_branches)
        ]

        return {
            "eventId": reflex_payload.get("eventId", ""),
            "hypothesis_id": hypothesis_id,
            "root_cause": _CROSS_DOMAIN_DEFAULTS["root_cause"],
            "root_cause_description": f"Cross-domain cascade: primary domain={primary_domain}",
            "timestamp_of_cause": now,
            "domain": "CROSS_DOMAIN",
            "confidence": "HIGH",
            "confidence_score": 0.85,
            "severity": "P1",
            "businessPriority": priority,
            "incident_type": _CROSS_DOMAIN_DEFAULTS["incident_type"],
            "change_request_id": primary_cfg.get("change_request_id", ""),
            "change_type_name": _CROSS_DOMAIN_DEFAULTS["change_type_name"],
            "risk_score": round(avg_risk, 3),
            "reversibility_score": round(avg_rev, 3),
            "primary_resource": {"node_id": entity_ids[0] if entity_ids else "", "type": primary_domain},
            "causal_parameters": primary_cfg["causal_parameters"],
            "affected_entities": entity_ids,
            "neighbor_entities": [],
            "affected_cells": entity_ids,
            "alarm_ids": ["ALM-001","ALM-002","ALM-003"],
            "affected_hex_bins": ["87283472bffffff","87283472affffff"],
            "confirmedRcaBranches": rca_branches,
            "suggested_remediation": suggested,
            "evidence_chain": [
                "PM counters: cross-domain degradation detected",
                "FM alarms: multiple domain alarms triggered",
                f"Primary domain: {primary_domain} (highest GNN business impact)",
                "Anomaly z-scores: multi-domain pattern confirmed",
                "Topology walk: cascade propagation across domain boundaries",
                "KB pattern: cross-domain cascade signature matched",
            ],
            "recovery_targets": all_recovery_targets,
            "kpi_impact": {
                "primary_metric": all_recovery_targets[0]["target_metric"] if all_recovery_targets else "",
                "primary_metric_delta_pct": primary_cfg["kpi_delta_pct"],
                "secondary_impacts": {},
            },
            "impact_score": impact_score,
            "criticality_score": criticality_score,
            "criticality_label": criticality_label,
            "investigation_timestamp": now,
            "source": "DetectiveAgent_Mock",
        }

    cfg = _pick_domain_config(domain_triage, entity_ids)
    rca_branches = _build_rca_branches_from_gnn_ranking(ranked_nodes,entity_ids,domain_triage)
    suggested = _build_suggested_remediation(domain_triage, cfg, entity_ids)
    neighbor_entities = entity_ids[1:] if len(entity_ids) > 1 else []

    return {
        "eventId": reflex_payload.get("eventId", ""),
        "hypothesis_id": hypothesis_id,
        "root_cause": cfg["root_cause"],
        "root_cause_description": f"{cfg['incident_type']} on {entity_ids[0] if entity_ids else 'unknown entity'}",
        "timestamp_of_cause": now,
        "domain": domain_triage,
        "confidence": "HIGH",
        "confidence_score": 0.85,
        "severity": "P1",
        "businessPriority": priority,
        "incident_type": cfg["incident_type"],
        "change_request_id": cfg["change_request_id"],
        "risk_score": cfg["risk_score"],
        "reversibility_score": cfg["reversibility_score"],
        "corrective_action": cfg.get("corrective_action", ""),
        "parameter_name": cfg.get("parameter_name", ""),
        "estimated_ttr_minutes": cfg.get("estimated_ttr_minutes", 0),
        "change_type_name": cfg.get("change_type_name", ""),
        "primary_resource": {
            "node_id": entity_ids[0] if entity_ids else "",
            "sector": 1,
            "type": domain_triage,
        },
        "causal_parameters": {
            **cfg["causal_parameters"],
            "unit": "degrees (inferred from dl_throughput_mbps)" if domain_triage == "RAN" else "sessions" if domain_triage == "CORE" else "path_id",
            "change_source": f"change_request_{cfg['change_request_id']}",
        },
        "affected_entities": list(dict.fromkeys(
            entity_ids
            + [f"gNB-SYN-{entity_ids[0].split('-')[-1]}" if entity_ids and entity_ids[0].startswith("eNB") else None]
            + ([reflex_payload.get("affected_neighbor_enodebs", [None])[0]] if reflex_payload.get("affected_neighbor_enodebs") else [])
        )) if domain_triage == "RAN" else entity_ids,
        "neighbor_entities": reflex_payload.get("affected_neighbor_enodebs", neighbor_entities),
        "affected_cells": [
            {"cell_id": entity_ids[i] if i < len(entity_ids) else "", "impact": "severe" if i == 0 else "moderate", "rsrp_delta": None, "drop_call_increase_pct": None, "load_increase_pct": 35.2 - (i * 2.0), "throughput_drop_pct": 50.6 - (i * 3.0), "latency_increase_ms": 22.4 + (i * 1.5)}
            for i in range(len(entity_ids))
        ] if domain_triage == "RAN" else [],
        "alarm_ids": ["ALM-001", "ALM-002", "ALM-003"],
        "affected_hex_bins": ["87283472bffffff", "87283472affffff"],
        "confirmedRcaBranches": rca_branches,
        "suggested_remediation": suggested,
        "evidence_chain": [
            f"GNN origin identification: {entity_ids[0] if entity_ids else 'unknown'} ranked #1 in anomalous subgraph",
            f"Z-score decomposition: z_ran=5.2, z_sev=4.1, z_vol=3.8 — {domain_triage}-domain confirmed",
            f"Alarm correlation: 3 CATA alarms — {cfg['incident_type']} triggered",
            f"Config change: {cfg['change_request_id']} ({cfg['incident_type']}) at {now}",
            f"KPI validation: {cfg['recovery_targets'][0]['target_metric'] if cfg['recovery_targets'] else 'N/A'} changed {cfg['kpi_delta_pct']:.1f}% across {len(entity_ids)} entities",
            f"Topology trace: shared infrastructure — {domain_triage} domain confirmed",
            "Transport health: healthy",
            f"RCD match: {cfg['root_cause']} (confidence 0.85)",
        ],
        "recovery_targets": cfg["recovery_targets"],
        "kpi_impact": {
            "primary_metric": cfg["recovery_targets"][0]["target_metric"] if cfg["recovery_targets"] else "",
            "primary_metric_delta_pct": cfg["kpi_delta_pct"],
            "secondary_impacts": {
                "dl_prb_utilization_pct": 35.2, "latency_ms": 22.4, "handover_success_rate": -12.1,
            } if domain_triage == "RAN" else {
                "session_setup_failure_pct": 35.0, "cpu_utilization_pct": 92.0,
            } if domain_triage == "CORE" else {
                "jitter_ms": 28.0, "latency_ms": 320.0,
            },
        },
        "impact_score": impact_score,
        "criticality_score": criticality_score,
        "criticality_label": criticality_label,
        "investigation_timestamp": now,
        "source": "DetectiveAgent_Mock",
    }
