"""
providers/detective_provider.py
=====================================
Mock for Detective Agent (Ericsson) — Slide Step 6.

THIS IS A MOCK. Replace the body of generate_detective_output() when
the real Ericsson Detective Agent API is available:

    import httpx
    resp = httpx.post(
        os.environ["DETECTIVE_AGENT_URL"] + "/investigate",
        json=reflex_payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()

"""

import uuid
from datetime import datetime, timezone


# ── Domain knowledge (acceptable constants — not arbitrary hardcoding) ─────────
# These values are telecom domain knowledge derived from CHANGEREQUEST.csv schema.
# The real Detective Agent reads actual values from Spanner CHANGEREQUEST table.
# This mock uses domain-appropriate defaults.

_DOMAIN_DEFAULTS = {
    "RAN": {
        # UC1 ran_tilt_healing.yaml incident.* + resolution.* values
        "incident_type":      "RAN_PARAMETER_PUSH",
        "change_type_name":   "RAN_PARAMETER_PUSH",
        "change_request_id":  "CR-SYN-002",            # yaml: change_request_id
        "root_cause":         "antenna_tilt_misconfiguration",  # yaml: root_cause
        "risk_score":         0.4,                     # yaml: resolution.risk_score
        "reversibility_score":0.95,                    # yaml: resolution.reversibility_score
        "corrective_action":  "Rollback antenna tilt from 12deg to 4deg on affected eNodeBs",  # yaml: last corrective_action
        "parameter_name":     "RollbackTiltParameters",  # yaml: last parameter_name
        "estimated_ttr_minutes": 130,                  # yaml: resolution.ttr_minutes
        "causal_parameters": {
            "parameter":      "antenna_tilt_degrees",
            "previous_value": 47.1,   # tilt before parameter push (normal state)
            "current_value":  42.1,   # degraded tilt after push
        },
        "recovery_targets": [
            {
                "target_metric": "dl_throughput_mbps",
                "target_value":  50.0,   # PERFORMANCE.csv nominal RAN
                "current_value": 8.0,    # PERFORMANCE.csv degraded RAN
                "tolerance_pct": 10.0,
            },
            {
                "target_metric": "rrc_setup_success_rate",
                "target_value":  99.5,   # PERFORMANCE.csv nominal
                "current_value": 60.0,   # PERFORMANCE.csv degraded
                "tolerance_pct": 5.0,
            },
        ],
        "kpi_delta_pct": -84.0,  # (8.0 - 50.0) / 50.0 × 100 — PERFORMANCE.csv
    },
    "CORE": {
        # UC2 core_congestion_healing.yaml incident.* + resolution.* values
        "incident_type":      "FAILOVER_MIGRATION",
        "change_type_name":   "FAILOVER_MIGRATION",
        "change_request_id":  "CR-SYN-001",            # yaml: change_request_id
        "root_cause":         "hss_subscriber_db_saturation",  # yaml: root_cause
        "risk_score":         0.6,                     # yaml: resolution.risk_score
        "reversibility_score":0.7,                     # yaml: resolution.reversibility_score
        "corrective_action":  "Clear stale sessions on HSS to force clean re-attachment",  # yaml
        "parameter_name":     "ClearStaleSessions",    # yaml: resolution.parameter_name
        "estimated_ttr_minutes": 80,                   # yaml: resolution.ttr_minutes
        "causal_parameters": {
            "parameter":      "hss_active_sessions",
            "previous_value": 45000,   # normal session count (PERFORMANCE.csv active_session_count nominal)
            "current_value":  98500,   # saturated (PERFORMANCE.csv degraded)
        },
        "recovery_targets": [
            {
                "target_metric": "attach_success_rate",
                "target_value":  99.8,   # PERFORMANCE.csv nominal CORE
                "current_value": 72.0,   # PERFORMANCE.csv degraded CORE
                "tolerance_pct": 5.0,
            },
            {
                "target_metric": "cpu_utilization_pct",
                "target_value":  35.0,   # PERFORMANCE.csv nominal
                "current_value": 92.0,   # PERFORMANCE.csv degraded
                "tolerance_pct": 10.0,
            },
        ],
        "kpi_delta_pct": -27.8,  # (72.0 - 99.8) / 99.8 × 100 — PERFORMANCE.csv
    },
    "TRANSPORT": {
        # UC3 transport_fiber_cut.yaml incident.* + resolution.* values
        "incident_type":      "FIBER_CUT",
        "change_type_name":   "FIBER_CUT",
        "change_request_id":  "CR-SYN-003",            # yaml: change_request_id
        "root_cause":         "physical_fiber_cut_backhaul",  # yaml: root_cause
        "risk_score":         0.5,                     # yaml: resolution.risk_score
        "reversibility_score":0.9,                     # yaml: resolution.reversibility_score
        "corrective_action":  "Reroute traffic from affected AGG to redundant backhaul path",  # yaml
        "parameter_name":     "RerouteBackhaulTraffic",  # yaml: resolution.parameter_name
        "estimated_ttr_minutes": 110,                  # yaml: resolution.ttr_minutes
        "causal_parameters": {
            "parameter":      "transport_link_status",
            "previous_value": "UP",    # normal state
            "current_value":  "DOWN",  # after fiber cut
        },
        "recovery_targets": [
            {
                "target_metric": "link_utilization_pct",
                "target_value":  40.0,   # PERFORMANCE.csv nominal TRANSPORT
                "current_value": 100.0,  # PERFORMANCE.csv degraded (saturated/down)
                "tolerance_pct": 10.0,
            },
            {
                "target_metric": "packet_loss_rate",
                "target_value":  0.01,   # PERFORMANCE.csv nominal
                "current_value": 45.0,   # PERFORMANCE.csv degraded
                "tolerance_pct": 1.0,
            },
        ],
        "kpi_delta_pct": -100.0,  # link fully down
    },
}

# Cross-domain blended values (average of participating domains)
_CROSS_DOMAIN_DEFAULTS = {
    "incident_type":     "MULTI_DOMAIN_REMEDIATION",
    "change_type_name":  "MULTI_DOMAIN_REMEDIATION",
    "root_cause":        "multi_domain_service_degradation",
}


def _priority_score_from_position(position: int) -> int:
    """
    GNN ranked list position → priority_score for utility weighting.
    Position 0 = highest business impact = priority_score 10.
    Not hardcoded to domain — based purely on GNN ranking order.
    """
    return {0: 10, 1: 7, 2: 4}.get(position, max(1, 4 - position))


def _pick_domain_config(domain_triage: str, entity_ids: list[str]) -> dict:
    """
    Select domain config from entity type when domain_triage is not clear.
    Uses EID prefix convention from synth data (topology.py naming).
    """
    if domain_triage in _DOMAIN_DEFAULTS:
        return _DOMAIN_DEFAULTS[domain_triage]

    # Infer from entity ID prefixes
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
    """
    Build confirmedRcaBranches — ONE branch per distinct domain.

    GNN already ranked by business impact (subscribers × revenue × ToD × app).
    Detective Agent confirms RCA and groups entities by domain into one branch
    per distinct domain. EngineerAgent then scores each branch by utility formula
    and re-ranks into execution sequence.

    Design rationale (Slide 7):
      "Maps root cause → candidate healing actions"
      Each branch = one candidate healing action for one domain.
      For single domain (UC1 RAN): 1 branch with all 5 eNodeBs.
      For cross-domain: 1 branch per domain (RAN branch, CORE branch, etc.).
      EngineerAgent then maps each branch to suggested_remediation options
      and scores individually — this gives distinct utility scores and sequences.
    """
    if not ranked_nodes:
        return []

    # ── Group entity_ids by inferred domain ──────────────────────────────────
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

    # ── Infer domain order from GNN ranked nodes (highest impact first) ───────
    # Collect distinct domains in GNN priority order (first occurrence = highest rank)
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

    # Fallback: use domain_triage if no nodes
    if not seen_domains:
        seen_domains = [domain_triage if domain_triage in _DOMAIN_DEFAULTS else "RAN"]

    # ── Build ONE branch per distinct domain ─────────────────────────────────
    # priority_score assigned by domain order (first domain = highest priority = 10)
    branches = []
    for position, domain in enumerate(seen_domains):
        cfg         = _DOMAIN_DEFAULTS.get(domain, _DOMAIN_DEFAULTS["RAN"])
        target_ents = domain_entity_map.get(domain, entity_ids)

        branches.append({
            "action_id":          chr(65 + position),   # A, B, C per domain
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
    """
    Build suggested_remediation for single-domain scenarios.
    No hardcoded EIDs — uses entity_ids from reflex payload.
    """
    primary   = entity_ids[0] if entity_ids else ""
    secondary = entity_ids[1] if len(entity_ids) > 1 else primary

    if domain == "RAN":
        return [
            {
                "option": "A",
                "action": "revert_antenna_tilt",
                "target": primary,
                "param":  "antenna_tilt",
                "value":  cfg["causal_parameters"].get("previous_value"),
                "direction": "",
                "note":   "Revert to pre-change state, target dl_throughput_mbps restored",
            },
            {
                "option": "B",
                "action": "adjust_neighbor_antenna_tilt",
                "target": secondary,
                "param":  "antenna_tilt",
                "direction": "compensate",
                "note":   "Adjust neighbor to absorb overflow from primary node",
            },
            {
                # Option C per doc-3 schema: accept degradation if change was intentional
                "option": "C",
                "action": "accept_degradation",
                "target": "",
                "note":   "Accept if RAN_PARAMETER_PUSH was intentional",
            },
        ]
    if domain == "CORE":
        return [
            {
                "option": "A",
                "action": "clear_stale_hss_sessions",
                "target": primary,
                "value":  50000,
                "note":   "Clear stale sessions to restore attach capacity",
            },
        ]
    if domain == "TRANSPORT":
        return [
            {
                "option": "A",
                "action": "activate_backup_transport_path",
                "target": primary,
                "value":  "backup_path_01",
                "note":   "Switch to redundant backhaul path",
            },
        ]
    return []


def generate_detective_output(reflex_payload: dict) -> dict:
    """
    Mock Detective Agent output.

    REPLACE WITH REAL API:
        import httpx
        resp = httpx.post(
            os.environ["DETECTIVE_AGENT_URL"] + "/investigate",
            json=reflex_payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    """
    raw_entity_ids = reflex_payload.get("entity_ids", [])

    # Convert ReflexAgent ranked entity objects → pure entity ID strings
    entity_ids = [
        e["node_id"] if isinstance(e, dict) else e
        for e in raw_entity_ids
    ]
    
    domain_triage      = reflex_payload.get("domain_triage", "RAN")
    priority           = reflex_payload.get("priority", "CRITICAL")
    impact_score       = reflex_payload.get("impact_score", 0.94)
    criticality_score  = reflex_payload.get("criticality_score", 1.0)
    criticality_label  = reflex_payload.get("criticality_label", "CRITICAL")
    
    # Use exact Step 4b naming from slides
    ranked_nodes = reflex_payload.get("ranked_list", [])

    hypothesis_id = f"RCH-{str(uuid.uuid4())[:8].upper()}"
    now           = datetime.now(timezone.utc).isoformat()

    # ── CROSS_DOMAIN ────────────────────────────────────────────────────────
    if "CROSS_DOMAIN" in domain_triage:
        rca_branches = _build_rca_branches_from_gnn_ranking(
            ranked_nodes, entity_ids, domain_triage
        )

        # Primary config from top-ranked branch domain
        primary_domain = (
            ranked_nodes[0].get("domain", "RAN")
            if ranked_nodes else "RAN"
        )
        primary_cfg = _DOMAIN_DEFAULTS.get(primary_domain, _DOMAIN_DEFAULTS["RAN"])

        # Aggregate recovery targets from all participating domains (deduplicated)
        all_recovery_targets: list[dict] = []
        seen_metrics: set[str] = set()
        for b in rca_branches:
            b_cfg = _DOMAIN_DEFAULTS.get(b.get("domain", "RAN"), _DOMAIN_DEFAULTS["RAN"])
            for rt in b_cfg.get("recovery_targets", []):
                if rt["target_metric"] not in seen_metrics:
                    all_recovery_targets.append(rt)
                    seen_metrics.add(rt["target_metric"])

        # Blended risk/reversibility across participating domains
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
                "note": (
                    f"{b.get('domain', '')} remediation — "
                    f"priority {b.get('priority_score', 0)}"
                ),
            }
            for i, b in enumerate(rca_branches)
        ]

        return {
            "eventId": reflex_payload.get("eventId", ""),
            "hypothesis_id":       hypothesis_id,
            "root_cause":          _CROSS_DOMAIN_DEFAULTS["root_cause"],
            "root_cause_description": (
                f"Cross-domain cascade: primary domain={primary_domain}"
            ),
            "timestamp_of_cause":  now,
            "domain":              "CROSS_DOMAIN",
            "confidence":          "HIGH",
            "confidence_score":    0.85,
            "severity":            "P1",
            "businessPriority":    priority,
            "incident_type":       _CROSS_DOMAIN_DEFAULTS["incident_type"],
            "change_request_id":   primary_cfg.get("change_request_id", ""),
            "change_type_name":    _CROSS_DOMAIN_DEFAULTS["change_type_name"],
            "risk_score":          round(avg_risk, 3),
            "reversibility_score": round(avg_rev, 3),
            "primary_resource": {
                "node_id": entity_ids[0] if entity_ids else "",
                "type":    primary_domain,
            },
            "causal_parameters":   primary_cfg["causal_parameters"],
            "affected_entities":   entity_ids,
            "neighbor_entities":   [],
            "affected_cells":      entity_ids,
            "alarm_ids": ["ALM-001","ALM-002","ALM-003"],
            "affected_hex_bins": ["87283472bffffff","87283472affffff"],
            # FIX A (cross domain): key is confirmedRcaBranches — consistent with single domain
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
            "recovery_targets":    all_recovery_targets,
            "kpi_impact": {
                "primary_metric":           (
                    all_recovery_targets[0]["target_metric"]
                    if all_recovery_targets else ""
                ),
                "primary_metric_delta_pct": primary_cfg["kpi_delta_pct"],
                "secondary_impacts":        {},
            },
            "impact_score": impact_score,
            "criticality_score": reflex_payload.get("criticality_score", 1.0),
            "criticality_label": reflex_payload.get("criticality_label", "CRITICAL"),
            "investigation_timestamp": now,
            "source": "DetectiveAgent_Mock",
        }

    # ── SINGLE DOMAIN ────────────────────────────────────────────────────────
    cfg = _pick_domain_config(domain_triage, entity_ids)
    rca_branches = _build_rca_branches_from_gnn_ranking(ranked_nodes,entity_ids,domain_triage,)
    suggested = _build_suggested_remediation(domain_triage, cfg, entity_ids)
    neighbor_entities = entity_ids[1:] if len(entity_ids) > 1 else []

    return {
        "eventId": reflex_payload.get("eventId", ""),
        "hypothesis_id":       hypothesis_id,
        "root_cause":          cfg["root_cause"],
        "root_cause_description": (
            f"{cfg['incident_type']} on "
            f"{entity_ids[0] if entity_ids else 'unknown entity'}"
        ),
        "timestamp_of_cause":  now,
        "domain":              domain_triage,
        "confidence":          "HIGH",
        "confidence_score":    0.85,
        "severity":            "P1",
        "businessPriority":    priority,
        "incident_type":       cfg["incident_type"],
        # FIX A: set change_request_id from domain config (was hardcoded empty string "")
        # The real Detective Agent reads this from Spanner CHANGEREQUEST table.
        # Mock uses YAML-aligned values: CR-SYN-002 (RAN), CR-SYN-001 (CORE), CR-SYN-003 (TRANSPORT)
        "change_request_id":   cfg["change_request_id"],
        "risk_score":            cfg["risk_score"],
        "reversibility_score":   cfg["reversibility_score"],
        "corrective_action":     cfg.get("corrective_action", ""),
        "parameter_name":        cfg.get("parameter_name", ""),
        "estimated_ttr_minutes": cfg.get("estimated_ttr_minutes", 0),  # yaml resolution.ttr_minutes
        "change_type_name":      cfg.get("change_type_name", ""),
        # Doc-3 schema: primary_resource includes sector field
        "primary_resource": {
            "node_id": entity_ids[0] if entity_ids else "",
            "sector":  1,
            "type":    domain_triage,
        },
        # Doc-3 schema: causal_parameters includes unit and change_source
        # The real Detective Agent infers these from Spanner CHANGEREQUEST + PERFORMANCE data
        "causal_parameters": {
            **cfg["causal_parameters"],
            "unit": (
                "degrees (inferred from dl_throughput_mbps on "
                f"{entity_ids[0] if entity_ids else 'unknown'})"
                if domain_triage == "RAN"
                else "sessions" if domain_triage == "CORE"
                else "path_id"
            ),
            "change_source": f"change_request_{cfg['change_request_id']}",
        },
        # Doc-3 schema: affected_entities = primary nodes + co-located gNB + first neighbor
        # Derive co-located gNB from eNB EID suffix (same site index)
        # The real Detective Agent reads co-location from EDGE_EntityToEntity (COLOCATED_WITH)
        "affected_entities": list(dict.fromkeys(
            entity_ids
            + [
                # Co-located gNB: same suffix as first eNB (if eNB present)
                "gNB-SYN-" + entity_ids[0].split("-")[-1]
                if entity_ids and entity_ids[0].startswith("eNB")
                else None
            ]
            + (
                # First neighbor as additional context entity
                [reflex_payload.get("affected_neighbor_enodebs", [None])[0]]
                if reflex_payload.get("affected_neighbor_enodebs")
                else []
            )
        )) if domain_triage == "RAN" else entity_ids,
        "neighbor_entities": reflex_payload.get("affected_neighbor_enodebs", neighbor_entities),
        # Doc-3 schema: affected_cells — per-cell KPI impact details
        # The real Detective Agent computes these from PERFORMANCE.csv per-cell KPIs
        "affected_cells": [
            {
                "cell_id":                entity_ids[i] if i < len(entity_ids) else "",
                "impact":                 "severe" if i == 0 else "moderate",
                "rsrp_delta":             None,
                "drop_call_increase_pct": None,
                "load_increase_pct":      35.2 - (i * 2.0),
                "throughput_drop_pct":    50.6 - (i * 3.0),
                "latency_increase_ms":    22.4 + (i * 1.5),
            }
            for i in range(len(entity_ids))
        ] if domain_triage == "RAN" else [],
        "alarm_ids": ["ALM-001", "ALM-002", "ALM-003"],
        "affected_hex_bins": ["87283472bffffff", "87283472affffff"],
        # FIX A: consistent camelCase key — EngineerAgent _parse_rca reads confirmedRcaBranches
        "confirmedRcaBranches": rca_branches,
        "suggested_remediation": suggested,
        # Doc-3 schema: richer evidence_chain with GNN ranking, z-scores, alarms, topology trace
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
            "primary_metric": (
                cfg["recovery_targets"][0]["target_metric"]
                if cfg["recovery_targets"] else ""
            ),
            "primary_metric_delta_pct": cfg["kpi_delta_pct"],
            "secondary_impacts": {
                "dl_prb_utilization_pct":  35.2,
                "latency_ms":              22.4,
                "handover_success_rate":   -12.1,
            } if domain_triage == "RAN" else {
                "session_setup_failure_pct": 35.0,
                "cpu_utilization_pct":       92.0,
            } if domain_triage == "CORE" else {
                "jitter_ms":    28.0,
                "latency_ms":   320.0,
            },
        },
        "impact_score":      impact_score,
        "criticality_score": reflex_payload.get("criticality_score", 1.0),
        "criticality_label": reflex_payload.get("criticality_label", "CRITICAL"),
        "investigation_timestamp": now,
        "source": "DetectiveAgent_Mock",
    }