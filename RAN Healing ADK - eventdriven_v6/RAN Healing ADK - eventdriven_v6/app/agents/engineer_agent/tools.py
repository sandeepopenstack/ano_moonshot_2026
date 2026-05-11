"""
app/agents/engineer_agent/tools.py
====================================
EngineerAgent — single tool: generate_healing_plan.

Slide Step 7:
  In : Structured Root Cause Hypotheses + Risk score + Reversibility score
       (from Detective Agent Step 6)
  Out: RCD Confirmed Cause (TMF921)


Branch/sequence logic:
  Single domain: 1 branch (A), optional 2nd branch (B) for neighbor_entities
  Cross-domain:  one branch per domain from Detective confirmedRcaBranches
  Branches sorted by utility score descending → sequence assigned AFTER sort
  Sequence 1 = highest utility = Executor runs this first
"""

import uuid
import os
import json
import logging
from datetime import datetime, timezone
from google.adk.tools import ToolContext

from app.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,
    NETWORK_STATUS_KEY,
    consume_latest,
    make_engineer_event,
    publish_event,
)
from app.config.remediation_config import (
    get_healing_actions,
    get_tilt_correction,
    normalize_root_cause,
    UTILITY_SCORING,
)


# ── Utility scoring ────────────────────────────────────────────────────────────

def _compute_utility(
        impact_score: float,
        criticality_score: float,
        risk_score: float,
        reversibility: float,
    ) -> float:
    
        return round(
            impact_score *
            criticality_score *
            (1.0 - risk_score) *
            reversibility,
            4
        )   


def _priority_from_utility(utility: float) -> str:
    t = UTILITY_SCORING["priority_thresholds"]
    if utility >= t["CRITICAL"]: return "CRITICAL"
    if utility >= t["HIGH"]:     return "HIGH"
    if utility >= t["MEDIUM"]:   return "MEDIUM"
    return "LOW"


# ── GNN values from state ─────────────────────────────────────────────────────
def _get_gnn_criticality_label(state: dict) -> str:
    """
    Get criticality label from GNN output.
    Used for display/routing only.
    """

    gnn = state.get("latest_gnn_result", {})

    return gnn.get("criticality_label", "CRITICAL")




# ── Business metadata ──────────────────────────────────────────────────────────

def _fetch_business_metadata(entity_ids: list[str]) -> dict[str, dict]:
    """
    Fetch per-entity traffic density and criticality tier.
    BIZ_METADATA_SOURCE=bigquery → aw_base_hex07_base.csv (Stage 0 topology.py)
    BIZ_METADATA_SOURCE=mock     → structural fallback from EID prefix
    """
    source = os.environ.get("BIZ_METADATA_SOURCE", "mock").lower()
    if source == "bigquery":
        try:
            return _fetch_from_bigquery(entity_ids)
        except Exception as e:
            logging.error(f"[EngineerAgent] BigQuery failed: {e} — structural fallback")
    return _fetch_structural_fallback(entity_ids)


def _fetch_structural_fallback(entity_ids: list[str]) -> dict[str, dict]:
    """
    Structural fallback — NO hardcoded EIDs.
    Infers tier from synth EID prefix (topology.py naming convention):
      HSS, SMF         → Tier1 (core anchor)
      MME, AMF, UPF, AGG → Tier2 (aggregation)
      eNodeB, gNodeB, CSR, unknown → Tier3 (edge)
    """
    fallback = UTILITY_SCORING["fallback_metadata"]

    def _tier(eid: str) -> str:
        u = eid.upper()
        if any(x in u for x in ("HSS", "SMF")):
            return "Tier1"
        if any(x in u for x in ("MME", "AMF", "UPF", "AGG")):
            return "Tier2"
        return "Tier3"

    _density_map = {"Tier1": 0.85, "Tier2": 0.65, "Tier3": 0.50}
    result = {}
    for eid in entity_ids:
        t = _tier(eid)
        result[eid] = {
            "biz_traffic_density":          _density_map.get(t, fallback["biz_traffic_density"]),
            "biz_service_criticality_tier": t,
            "source": "structural_fallback",
        }
    return result


def _fetch_from_bigquery(entity_ids: list[str]) -> dict[str, dict]:
    """
    Real BigQuery query against aw_base_hex07_base.csv (Stage 0 topology.py).
    Active when BIZ_METADATA_SOURCE=bigquery + BQ_PROJECT + BQ_DATASET.
    network_segment: NR→Tier1 (5G premium), LTE→Tier2.
    biz_cell_profile: TRANSIT_HUB→0.90, COMMERCIAL→0.75, RESIDENTIAL→0.55.
    """
    from google.cloud import bigquery
    project = os.environ["BQ_PROJECT"]
    dataset = os.environ["BQ_DATASET"]
    client  = bigquery.Client(project=project)
    query   = f"""
        SELECT DISTINCT enodeb AS entity_id, network_segment, biz_cell_profile
        FROM `{project}.{dataset}.aw_base_hex07_base`
        WHERE enodeb IN UNNEST(@entity_ids)
    """
    cfg  = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("entity_ids", "STRING", entity_ids)]
    )
    rows     = list(client.query(query, job_config=cfg).result())
    result   = {}
    fallback = UTILITY_SCORING["fallback_metadata"]
    _profile_density = {"TRANSIT_HUB": 0.90, "COMMERCIAL": 0.75, "RESIDENTIAL": 0.55}
    for r in rows:
        tier    = "Tier1" if r["network_segment"] == "NR" else "Tier2"
        profile = (r.get("biz_cell_profile") or "").upper()
        result[r["entity_id"]] = {
            "biz_traffic_density":          _profile_density.get(profile, fallback["biz_traffic_density"]),
            "biz_service_criticality_tier": tier,
            "biz_cell_profile":             profile,
            "source": "bigquery",
        }
    for eid in entity_ids:
        if eid not in result:
            result[eid] = _fetch_structural_fallback([eid])[eid]
    return result


def _get_traffic_density(entity_ids: list[str], metadata: dict) -> float:
    fallback = UTILITY_SCORING["fallback_metadata"]["biz_traffic_density"]
    return max(
        (metadata.get(eid, {}).get("biz_traffic_density", fallback) for eid in entity_ids),
        default=fallback,
    )


def _fallback_impact_score(entity_ids: list[str], metadata: dict) -> float:
    """Fallback when GNN result not in state. Maps Tier1→1.0, Tier2→0.7, Tier3→0.4."""
    tier_weight = UTILITY_SCORING["tier_weight"]
    tier_rank   = {"Tier1": 3, "Tier2": 2, "Tier3": 1}
    best        = UTILITY_SCORING["fallback_metadata"]["biz_service_criticality_tier"]
    for eid in entity_ids:
        t = metadata.get(eid, {}).get("biz_service_criticality_tier", "Tier3")
        if tier_rank.get(t, 1) > tier_rank.get(best, 1):
            best = t
    return tier_weight.get(best, 0.7)


# ── Parse Detective Agent RCA output ──────────────────────────────────────────

def _parse_rca(raw: dict) -> dict:
    """
    Parse Detective Agent RCA payload (detective.rca.confirmed event payload).

    kpi_delta_pct resolution (PERFORMANCE.csv values via Detective Agent):
      1. raw['kpi_delta_pct']                        — mock sends at root level
      2. raw['kpi_impact']['primary_metric_delta_pct'] — real Detective Agent (doc 30 schema)
      3. UTILITY_SCORING['default_kpi_delta_pct']    — fallback (30.0)

    risk_score + reversibility_score (CHANGEREQUEST.csv via Detective Agent):
      UC1: 0.4 / 0.95  (ran_tilt_healing.yaml resolution.*)
      UC2: 0.6 / 0.7   (core_congestion_healing.yaml resolution.*)
      UC3: 0.5 / 0.9   (transport_fiber_cut.yaml resolution.*)

    confirmedRcaBranches key:
      Detective Agent returns camelCase "confirmedRcaBranches".
      Fallback to snake_case "confirmed_rca_branches" for forward compatibility.
    """
    kpi_impact = raw.get("kpi_impact", {})
    raw_delta  = (
        raw.get("kpi_delta_pct")
        or kpi_impact.get("primary_metric_delta_pct")
        or UTILITY_SCORING["default_kpi_delta_pct"]
    )
    kpi_delta_pct = abs(float(raw_delta))

    return {
        "original_root_cause":    raw.get("root_cause", ""),
        "normalized_root_cause":  normalize_root_cause(raw.get("root_cause", "")),
        "domain":                 raw.get("domain", "UNKNOWN"),
        "confidence_score":       float(raw.get("confidence_score", 0.0)),
        "confidence_label":       raw.get("confidence", ""),
        "affected_entities":      raw.get("affected_entities", []),
        "affected_cells":         raw.get("affected_cells", []),
        "change_request_id":      raw.get("change_request_id", ""),
        "hypothesis_id":          raw.get("hypothesis_id", ""),
        "eventId": raw.get("eventId", ""),
        "alarm_ids":              raw.get("alarm_ids", []),
        "incident_type":          raw.get("incident_type", ""),
        "causal_parameters":      raw.get("causal_parameters", {}),
        "suggested_remediation":  raw.get("suggested_remediation", []),
        "recovery_targets":       raw.get("recovery_targets", []),
        "kpi_impact":             kpi_impact,
        "kpi_delta_pct":          kpi_delta_pct,
        "primary_resource":       raw.get("primary_resource", {}),
        "evidence_chain":         raw.get("evidence_chain", []),
        # FIX B: Detective Agent returns camelCase "confirmedRcaBranches".
        # Fallback to snake_case for forward compatibility with any future naming.
        "confirmed_rca_branches": raw.get("confirmedRcaBranches", raw.get("confirmed_rca_branches", [])),
        "business_priority":      raw.get("businessPriority", "CRITICAL"),
        "severity":               raw.get("severity", "P1"),
        "root_cause_description": raw.get("root_cause_description", ""),
        "timestamp_of_cause": raw.get("timestamp_of_cause", ""),
        "affected_hex_bins": raw.get("affected_hex_bins", []),
        # CHANGEREQUEST.csv + YAML resolution.* values via Detective Agent
        "risk_score":             float(raw.get("risk_score", 0.5)),
        "reversibility_score":    float(raw.get("reversibility_score", 0.8)),
        "impact_score": float(raw.get("impact_score", 0.94)),
        "criticality_score": float(raw.get("criticality_score", 1.0)),
        "criticality_label": raw.get("criticality_label", "CRITICAL"),
        "change_type_name":       raw.get("change_type_name", "UNKNOWN"),
        # YAML resolution.ttr_minutes: total fault→resolution time
        # UC1=130min, UC2=80min, UC3=110min — via Detective Agent payload
        "estimated_ttr_minutes":  int(raw.get("estimated_ttr_minutes", 0)),
    }


# ── Build action command ───────────────────────────────────────────────────────

def _build_action_command(rca: dict, actions_def: dict) -> dict:
    """
    Build action command from RCA causal_parameters + HEALING_ACTIONS definition.

    action_type = actions_def['yaml_action'] which matches ACTIVATIONS.csv action field:
      UC1: RAN_PARAM_ROLLBACK   (YAML: resolution.action)
      UC2: MZ_SESSION_CLEAR     (YAML: resolution.action)
      UC3: BACKHAUL_REROUTE     (YAML: resolution.action)

    Additional params come from causal_parameters (Detective Agent evidence chain)
    and tmf915_parameter_bounds (remediation_config domain knowledge).
    """
    root_cause  = rca["normalized_root_cause"].lower()
    causal      = rca["causal_parameters"]
    bounds      = actions_def.get("tmf915_parameter_bounds", {})
    yaml_action = actions_def.get("yaml_action", "UNKNOWN_ACTION")
    suggestions = rca.get("suggested_remediation") or []
    best        = suggestions[0] if suggestions else None
    alternatives = suggestions[1:] if len(suggestions) > 1 else []

    # ── RAN: antenna tilt rollback ────────────────────────────────────────────
    if root_cause in ("bad_antenna_tilt_push", "antenna_tilt_misconfiguration"):
        current_tilt = causal.get("current_value")
        target_tilt  = causal.get("previous_value")
        if target_tilt is None and best:
            target_tilt = best.get("value")
        if current_tilt is not None and target_tilt is not None:
            correction = get_tilt_correction(float(current_tilt), float(target_tilt))
            return {
                "type":               yaml_action,    # RAN_PARAM_ROLLBACK (ACTIVATIONS.csv)
                "action_detail":      "ANTENNA_TILT_ADJUST",
                "current_degrees":    correction["current_tilt_degrees"],
                "target_degrees":     correction["target_tilt_degrees"],
                "delta_degrees":      correction["correction_delta"],
                "within_safe_bounds": correction["within_safe_bounds"],
                "clamped":            correction["clamped"],
                "description": (
                    f"Rollback tilt: {current_tilt}° → "
                    f"{correction['target_tilt_degrees']}° "
                    f"(delta {correction['correction_delta']:+.2f}°"
                    + (", CLAMPED to safe bound ±5°" if correction["clamped"] else "") + ")"
                ),
                "parameter_name":   "RollbackTiltParameters",
                "safe_profile":     bounds.get("safe_profile"),
                "rollback_action":  bounds.get("rollback_action"),
                "source_recommendation": (
                    {"option": best.get("option"), "action": best.get("action")}
                    if best else None
                ),
                "alternative_actions": [
                    {"option": s.get("option"), "action": s.get("action")}
                    for s in alternatives
                ],
            }
        return {
            "type":           yaml_action,
            "action_detail":  "ANTENNA_TILT_ROLLBACK_DEFAULT",
            "target_degrees": bounds.get("baseline_value"),
            "parameter_name": "RollbackTiltParameters",
            "description":    "Rollback to nominal tilt (causal_parameters not available)",
        }

    # ── CORE: HSS session clear ───────────────────────────────────────────────
    if root_cause in (
        "hss_stale_session_loop", "hss_saturation",
        "hss_session_table_overflow", "hss_subscriber_db_saturation",
    ):
        return {
            "type":                yaml_action,    # MZ_SESSION_CLEAR (ACTIVATIONS.csv)
            "action_detail":       "HSS_SESSION_CLEAR",
            "sessions_to_clear":   bounds.get("max_clear"),
            "target_capacity_pct": bounds.get("target_capacity_pct"),
            "parameter_name":      "ClearStaleSessions",
            "safe_profile":        bounds.get("safe_profile"),
            "rollback_action":     bounds.get("rollback_action"),
            "description": (
                f"Clear up to {bounds.get('max_clear', 0):,} stale HSS sessions "
                f"to restore capacity to {bounds.get('target_capacity_pct')}%"
            ),
        }

    # ── TRANSPORT: backhaul reroute / failover ────────────────────────────────
    if root_cause in (
        "fiber_cut", "path_degradation", "physical_fiber_cut_backhaul",
        "transport_path_failure", "transport_path_degradation",
    ):
        return {
            "type":           yaml_action,     # BACKHAUL_REROUTE (ACTIVATIONS.csv)
            "action_detail":  "TRANSPORT_FAILOVER",
            "backup_path":    bounds.get("backup_path", bounds.get("safe_profile")),
            "primary_path":   bounds.get("primary_path"),
            "parameter_name": "RerouteBackhaulTraffic",
            "rollback_action": bounds.get("rollback_action"),
            "description":    (
                f"Reroute traffic from primary path to "
                f"{bounds.get('backup_path', 'AGG_REDUNDANT')}"
            ),
        }

    # ── CROSS_DOMAIN ──────────────────────────────────────────────────────────
    if root_cause == "multi_domain_service_degradation":
        return {
            "type":         yaml_action,
            "action_detail": "MULTI_DOMAIN_SEQUENCE",
            "safe_profile": bounds.get("safe_profile"),
            "rollback_action": bounds.get("rollback_action"),
            "description":  "Execute multi-domain remediation sequence",
        }

    return {
        "type":        "MANUAL_INVESTIGATION_REQUIRED",
        "action_detail": "UNKNOWN",
        "description": f"Unknown root cause: {rca['original_root_cause']}",
    }


def _build_action_command_for_branch(
    branch_domain:     str,
    branch_root_cause: str,
    branch_causal:     dict,
    actions_def:       dict,
) -> dict:
    """Build action command for a cross-domain scenario branch."""
    rc_norm     = normalize_root_cause(branch_root_cause)
    bounds      = actions_def.get("tmf915_parameter_bounds", {})
    yaml_action = actions_def.get("yaml_action", "UNKNOWN_ACTION")

    if branch_domain == "RAN" or rc_norm in (
        "bad_antenna_tilt_push", "antenna_tilt_misconfiguration"
    ):
        current = branch_causal.get("current_value")
        target  = branch_causal.get("previous_value")
        if current is not None and target is not None:
            correction = get_tilt_correction(float(current), float(target))
            return {
                "type":            yaml_action,
                "action_detail":   "ANTENNA_TILT_ADJUST",
                "current_degrees": correction["current_tilt_degrees"],
                "target_degrees":  correction["target_tilt_degrees"],
                "delta_degrees":   correction["correction_delta"],
                "description": (
                    f"Rollback tilt: {current}° → {correction['target_tilt_degrees']}°"
                ),
            }
        return {
            "type":           yaml_action,
            "action_detail":  "ANTENNA_TILT_ROLLBACK_DEFAULT",
            "target_degrees": bounds.get("baseline_value"),
            "description":    "Rollback antenna tilt to baseline",
        }

    if branch_domain == "CORE" or rc_norm in (
        "hss_stale_session_loop", "hss_session_table_overflow",
        "hss_saturation", "hss_subscriber_db_saturation",
    ):
        return {
            "type":              yaml_action,
            "action_detail":     "HSS_SESSION_CLEAR",
            "sessions_to_clear": bounds.get("max_clear", 10000),
            "description":       "Clear stale HSS sessions to restore capacity",
        }

    if branch_domain == "TRANSPORT" or rc_norm in (
        "fiber_cut", "physical_fiber_cut_backhaul",
        "transport_path_failure", "transport_path_degradation",
    ):
        return {
            "type":        yaml_action,
            "action_detail": "TRANSPORT_FAILOVER",
            "backup_path": bounds.get("backup_path", "AGG_REDUNDANT"),
            "description": "Failover backhaul to redundant path",
        }

    return {
        "type":          "MANUAL_INVESTIGATION_REQUIRED",
        "action_detail": "UNKNOWN",
        "description":   f"Unknown branch root cause: {branch_root_cause}",
    }


# ── Build utility-scored branches ──────────────────────────────────────────────
def _build_branches(
    rca,
    actions_def,
    impact_score,
    criticality_score,
    risk_score,
    reversibility,
)-> list[dict]:
    """
    Build healing branches — one per candidate healing action — and rank by utility.

    Slide 7:
      "Maps root cause → candidate healing actions. Scores each candidate using
       utility formula: Utility = impact × criticality × (1-risk) × reversibility.
       Ranks and sequences by utility score. Sequence 1 = highest utility = Executor first."

    Branch sources:
      Single domain  → one branch per suggested_remediation option from Detective Agent
                        (Option A = primary action, Option B = secondary/neighbor, ...)
                        Fallback: one branch per ranked_healing_actions from remediation_config
      Cross-domain   → one branch per domain from confirmedRcaBranches
      CROSS_DOMAIN   → one branch per domain inferred from entity EID prefixes

    Utility differentiation across branches:
      Option A (primary):   base risk_score         → highest utility → Sequence 1
      Option B (secondary): risk_score + 0.1        → lower utility  → Sequence 2
      Option C (tertiary):  risk_score + 0.2        → lowest utility → Sequence 3

    Action types align with ACTIVATIONS.csv:
      UC1 RAN:       RAN_PARAM_ROLLBACK   / ANTENNA_TILT_ADJUST
      UC2 CORE:      MZ_SESSION_CLEAR     / HSS_SESSION_CLEAR
      UC3 TRANSPORT: BACKHAUL_REROUTE     / TRANSPORT_FAILOVER

    ranked_healing_actions: ordered sub-steps within the action (from remediation_config):
      UC1: [ROLLBACK_TILT_TO_BASELINE, REDUCE_TILT_BY_2_DEGREES, REBUILD_NEIGHBOR_RELATIONS]
      UC2: [CLEAR_STALE_HSS_SESSIONS, SHIFT_TRAFFIC_TO_SECONDARY_HSS, REDUCE_REATTACH_RATE_LIMIT]
      UC3: [FAILOVER_TO_REDUNDANT_FIBER_PATH, REROUTE_TRAFFIC_VIA_BACKUP_AGG, ISOLATE_FAILED_AGG_NODE]

    Sequence assigned AFTER sort: sort descending by utility → sequence 1, 2, 3...
    """
    branches: list[dict] = []

    if rca["confirmed_rca_branches"] and (
        rca["domain"] == "CROSS_DOMAIN"
        or len({b.get("domain") for b in rca["confirmed_rca_branches"]}) > 1
    ):
        # ── Cross-domain: one branch per domain from Detective Agent ──────────
        branches = []
        for branch in rca["confirmed_rca_branches"]:
            b_domain     = branch.get("domain", rca["domain"])
            b_root_cause = branch.get("root_cause", "")
            b_rc_norm    = normalize_root_cause(b_root_cause)
            act_def      = get_healing_actions(b_rc_norm)

            # Per-branch risk + reversibility from CHANGEREQUEST.csv
            b_risk       = float(branch.get("risk_score", risk_score))
            b_reversible = float(branch.get("reversibility", reversibility))
            b_priority   = float(branch.get("priority_score", 10))

            b_kpi   = rca["kpi_delta_pct"] * (b_priority / 10.0)
            utility = _compute_utility(impact_score, criticality_score, b_risk, b_reversible,)

            action_cmd  = _build_action_command_for_branch(
                b_domain, b_root_cause,
                branch.get("causal_parameters", rca["causal_parameters"]),
                act_def,
            )
            target_ents = branch.get("target_entities", rca["affected_entities"])

            branches.append({
                "domain":                 b_domain,
                "root_cause":             b_root_cause,
                "root_cause_normalized":  b_rc_norm,
                "priority_score":         b_priority,
                "utility_score":          utility,
                "utility_priority":       _priority_from_utility(utility),
                "ranked_healing_actions": act_def.get("ranked_healing_actions", []),
                "action_command":         action_cmd,
                "target_entities":        target_ents,
                "risk_score":             b_risk,
                "reversibility_score":    b_reversible,
                "impact_score":          impact_score,
                "action_source":          "investigation",
            })
    elif rca["domain"] == "CROSS_DOMAIN":
        # ── CROSS_DOMAIN fallback: when confirmed_rca_branches is empty ────────
        #
        # Builds one branch per DISTINCT domain found in affected_entities.
        # Each domain gets its own:
        #   - root_cause: from _DOMAIN_DEFAULTS (not hardcoded UC values)
        #   - risk_score + reversibility: domain-specific from _DOMAIN_DEFAULTS
        #   - utility score: computed independently → enables meaningful ranking
        #   - target_entities: only the entities belonging to that domain
        #
        # EID prefix matching uses the same patterns as topology.py:
        #   ENB/GNB → RAN | HSS/MME/AMF/UPF/SMF → CORE | AGG/CSR → TRANSPORT

        from app.providers.detective_provider import _DOMAIN_DEFAULTS as _DD

        branches = []

        # ── Group affected_entities by domain (one branch per domain) ─────────
        domain_entity_map: dict[str, list[str]] = {}
        for eid in rca["affected_entities"]:
            u = eid.upper()
            if any(x in u for x in ("ENB", "GNB", "CELL", "SECTOR", "NR")):
                domain_entity_map.setdefault("RAN", []).append(eid)
            elif any(x in u for x in ("HSS", "MME", "AMF", "UPF", "SMF", "CORE")):
                domain_entity_map.setdefault("CORE", []).append(eid)
            elif any(x in u for x in ("AGG", "CSR", "TRANSPORT", "BACKHAUL", "FIBER", "LINK")):
                domain_entity_map.setdefault("TRANSPORT", []).append(eid)
            else:
                # Unknown prefix — fall to RAN as default (most common)
                domain_entity_map.setdefault("RAN", []).append(eid)

        # ── Build one branch per domain — utility scored independently ─────────
        for branch_domain, domain_eids in domain_entity_map.items():

            # root_cause and risk/rev from domain config — NOT hardcoded
            domain_cfg    = _DD.get(branch_domain, _DD["RAN"])
            branch_rc     = domain_cfg["root_cause"]
            branch_risk   = domain_cfg["risk_score"]
            branch_rev    = domain_cfg["reversibility_score"]

            # Utility computed per domain with domain-specific risk/reversibility
            # This is what allows CORE to outrank RAN if CORE has lower risk/higher rev
            branch_utility = _compute_utility(
                impact_score, criticality_score, branch_risk, branch_rev
            )

            act_def    = get_healing_actions(normalize_root_cause(branch_rc))
            action_cmd = _build_action_command_for_branch(
                branch_domain=branch_domain,
                branch_root_cause=branch_rc,
                branch_causal=rca["causal_parameters"],
                actions_def=act_def,
            )

            branches.append({
                "domain":                 branch_domain,
                "root_cause":             branch_rc,
                "root_cause_normalized":  normalize_root_cause(branch_rc),
                "priority_score":         10,
                "utility_score":          branch_utility,
                "utility_priority":       _priority_from_utility(branch_utility),
                "ranked_healing_actions": act_def.get("ranked_healing_actions", []),
                "action_command":         action_cmd,
                "target_entities":        domain_eids,
                "risk_score":             branch_risk,
                "reversibility_score":    branch_rev,
                "impact_score":           impact_score,
                "action_source":          "engineer_agent",
            })
            
    else:
        # ── Single domain: one branch per candidate healing action ────────────
        #
        # Slide 7: "Maps root cause → candidate healing actions. Scores each
        # candidate using utility formula. Ranks and sequences by utility score."
        #
        # Each Detective Agent suggested_remediation option = one candidate.
        # Option A: primary rollback action  → base risk → highest utility → seq 1
        # Option B: secondary/neighbor action → risk + 0.1 → lower utility → seq 2
        # ...
        # Fallback (no suggestions): use ranked_healing_actions from remediation_config
        #
        # All options share the same root_cause mapping and ACTIVATIONS.csv action type.
        # Utility differences come from per-option risk profiles, NOT different action types.

        suggestions = rca.get("suggested_remediation") or []

        if suggestions:
            # ── Build one branch per Detective Agent suggested option ─────────
            for idx, suggestion in enumerate(suggestions):
                # Risk escalates for each subsequent option (primary → fallback options)
                option_risk = min(risk_score + (0.1 * idx), 1.0)
                option_utility = _compute_utility(
                    impact_score, criticality_score, option_risk, reversibility
                )

                # Target: use suggestion's specific target if given, else all affected entities
                option_target = (
                    [suggestion["target"]]
                    if suggestion.get("target")
                    else rca["affected_entities"]
                )

                # Map suggestion action → healing action definition
                # All options use the same root_cause → same ACTIVATIONS.csv action type
                # The distinction is at action_detail level (ANTENNA_TILT_ADJUST vs neighbor adjust)
                option_actions_def = get_healing_actions(rca["normalized_root_cause"])

                # Build action command for this specific option
                # Special case: accept_degradation (option C per doc-3) is a NO-OP —
                # it means the change was intentional and no remediation is needed.
                # It must NOT produce an active healing command (ANTENNA_TILT_ADJUST etc).
                if suggestion.get("action") == "accept_degradation":
                    option_action_cmd = {
                        "type":          "ACCEPT_DEGRADATION",
                        "action_detail": "NO_ACTION",
                        "description":   suggestion.get("note", "Accept degradation — no remediation applied"),
                        "parameter_name": "NoOp",
                    }
                    # Accept degradation = highest risk (do nothing may worsen situation)
                    # Lowest utility → always last in sequence
                    option_risk    = min(risk_score + (0.1 * idx) + 0.2, 1.0)
                    option_utility = _compute_utility(
                        impact_score, criticality_score, option_risk, reversibility
                    )
                else:
                    option_action_cmd = _build_action_command(rca, option_actions_def)

                branches.append({
                    "domain":                 rca["domain"],
                    "root_cause":             rca["original_root_cause"],
                    "root_cause_normalized":  rca["normalized_root_cause"],
                    "option":                 suggestion.get("option", chr(65 + idx)),
                    "option_action":          suggestion.get("action", ""),
                    "option_note":            suggestion.get("note", ""),
                    "priority_score":         10 - idx,
                    "utility_score":          option_utility,
                    "utility_priority":       _priority_from_utility(option_utility),
                    # ranked_healing_actions: config-defined sub-steps for this domain action
                    "ranked_healing_actions": option_actions_def.get("ranked_healing_actions", []),
                    "recommended_action":     suggestion.get("action", ""),
                    "action_command":         option_action_cmd,
                    "target_entities":        option_target,
                    "causal_parameters":      rca["causal_parameters"],
                    "risk_score":             option_risk,
                    "reversibility_score":    reversibility,
                    "impact_score":           impact_score,
                    "action_source":          "investigation",
                })
        else:
            # ── Fallback: use ranked_healing_actions from remediation_config ──
            # No suggested_remediation from Detective Agent — use domain config
            fallback_actions = actions_def.get("ranked_healing_actions", [])
            for idx, action_name in enumerate(fallback_actions):
                option_risk    = min(risk_score + (0.1 * idx), 1.0)
                option_utility = _compute_utility(
                    impact_score, criticality_score, option_risk, reversibility
                )
                option_action_cmd = _build_action_command(rca, actions_def)
                branches.append({
                    "domain":                 rca["domain"],
                    "root_cause":             rca["original_root_cause"],
                    "root_cause_normalized":  rca["normalized_root_cause"],
                    "option":                 chr(65 + idx),
                    "option_action":          action_name,
                    "option_note":            f"Config-defined sub-action {idx + 1}",
                    "priority_score":         10 - idx,
                    "utility_score":          option_utility,
                    "utility_priority":       _priority_from_utility(option_utility),
                    "ranked_healing_actions": [action_name],
                    "recommended_action":     action_name,
                    "action_command":         option_action_cmd,
                    "target_entities":        rca["affected_entities"],
                    "causal_parameters":      rca["causal_parameters"],
                    "risk_score":             option_risk,
                    "reversibility_score":    reversibility,
                    "impact_score":           impact_score,
                    "action_source":          "config",
                })
    
    # Sort by utility descending → assign sequence after sort
    branches.sort(key=lambda b: b["utility_score"], reverse=True)
    for i, b in enumerate(branches):
        b["sequence"] = i + 1

    return branches


# ── Build TMF921 intent ────────────────────────────────────────────────────────

def _build_tmf921_intent(
    rca:                dict,
    branches:           list[dict],
    utility_priority:   str,
    criticality_label:  str,
    intent_id:          str,
) -> dict:
    """
    Build TMF921 intent expressing the desired network outcome.
    Sequence 1 branch = highest utility = primary action for Executor.
    recovery_targets from Detective Agent (PERFORMANCE.csv nominal vs degraded).
    criticality_label = INSIGHT.csv string label — display only.
    """
    expressions = [
        {
            "target_metric": t.get("target_metric"),
            "target_value":  t.get("target_value"),
            "current_value": t.get("current_value"),
            "tolerance_pct": t.get("tolerance_pct"),
        }
        for t in (rca["recovery_targets"] or []) if t.get("target_metric")
    ]
    healing         = get_healing_actions(rca["normalized_root_cause"])
    primary_branch  = branches[0] if branches else {}
    primary_action  = primary_branch.get("action_command", {})
    root_cause_node = (
        rca["primary_resource"].get("node_id", "")
        if rca.get("primary_resource") else ""
    )

    return {
        "intent_id":         intent_id,
        "intent_type":       "remediation",
        "description": (
            f"Remediate {rca['original_root_cause']} on "
            f"{len(rca['affected_entities'])} entities — "
            f"{len(branches)} branch(es) ranked by utility score"
        ),
        "root_cause":        rca["original_root_cause"],
        "root_cause_mapped": rca["normalized_root_cause"],
        "root_cause_entity": root_cause_node,
        "domain":            rca["domain"],
        "priority":          utility_priority,
        "criticality":       criticality_label,   # INSIGHT.csv string — display only
        "target_entities":   rca["affected_entities"],
        "expressions":       expressions,
        "ranked_healing_branches": [
            {
                "sequence":               b["sequence"],
                "domain":                 b["domain"],
                "action":                 b.get("action_command", {}).get("type"),
                "action_detail":          b.get("action_command", {}).get("action_detail", ""),
                "action_desc":            b.get("action_command", {}).get("description", ""),
                "action_command":         b.get("action_command", {}),
                "ranked_healing_actions": b.get("ranked_healing_actions", []),
                "target":                 b.get("target_entities"),
                "utility_score":          b["utility_score"],
                "utility_priority":       b["utility_priority"],
            }
            for b in branches
        ],
        "constraints": {
            # estimated_ttr_minutes: from YAML resolution.ttr_minutes (UC1=130, UC2=80, UC3=110)
            # expected_recovery_minutes: action effect time from HEALING_ACTIONS domain knowledge
            "estimated_ttr_minutes":       rca.get("estimated_ttr_minutes") or healing.get("expected_recovery_minutes"),
            "expected_recovery_minutes":   healing.get("expected_recovery_minutes"),
            "reversible_required":         True,
            "maintenance_window":          "immediate",
        },
        "change_request_id":      rca["change_request_id"],
        "hypothesis_id":          rca["hypothesis_id"],
        "activation_id": (
            f"ACT-SYN-{rca['change_request_id'].split('-')[-1]}"
            if rca["change_request_id"]
            else f"ACT-{str(uuid.uuid4())[:8].upper()}"
        ),
        "evidence_chain":         rca["evidence_chain"],
        "confidence_score":       rca["confidence_score"],
        "primary_action_command": primary_action,
        "created_at":             datetime.now(timezone.utc).isoformat(),
        "created_by":             "EngineerAgent",
    }


# ── Main tool ──────────────────────────────────────────────────────────────────

def generate_healing_plan(tool_context: ToolContext) -> dict:
    """
    Single tool — NO arguments.
    Slide Step 7: In: RCA + risk + reversibility → Out: TMF921 healing plan.
    """
    state = tool_context.state

    rca_event = consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)
    if not rca_event:
        return {"status": "IDLE", "reason": "No detective.rca.confirmed event in state"}

    if state.get("engineer_last_event_id") == rca_event["event_id"]:
        return {"status": "SKIPPED", "event_id": rca_event["event_id"],
                "reason": "Already processed this RCA event"}

    source_id = rca_event["event_id"]
    rca       = _parse_rca(rca_event["payload"])

    # ── Step 6: Detective Agent → EngineerAgent (display input received) ────
    print("\n" + "=" * 65)
    print("[Step 6] Detective Agent → EngineerAgent")
    print("=" * 65)
    print("\n  API CALL")
    print("  POST /generate-healing-plan")
    print("\n  REQUEST PAYLOAD")
    detective_to_engineer = {
        "eventId": rca["eventId"],
        "root_cause":          rca["original_root_cause"],
        "domain":              rca["domain"],
        "affected_entities":   rca["affected_entities"],
        "causal_parameters":   rca["causal_parameters"],
        "suggested_remediation": rca["suggested_remediation"],
        "recovery_targets":    rca["recovery_targets"],
        "kpi_impact":          rca["kpi_impact"],
        "kpi_delta_pct":       rca["kpi_delta_pct"],
        "risk_score":          rca["risk_score"],
        "reversibility_score": rca["reversibility_score"],
        "change_request_id":   rca["change_request_id"],
        "hypothesis_id":       rca["hypothesis_id"],
        "confidence_score":    rca["confidence_score"],
        "evidence_chain":      rca["evidence_chain"],
        "incident_type":       rca["incident_type"],
        "root_cause_description": rca.get("root_cause_description"),
        "timestamp_of_cause": rca.get("timestamp_of_cause"),
        "primary_resource": rca.get("primary_resource"),
        "affected_hex_bins": rca.get("affected_hex_bins", []),
        "alarm_ids": rca.get("alarm_ids", []),
        # From GNN via ReflexAgent (INSIGHT.csv)
        "impact_score": rca["impact_score"],
        "criticality_score": rca["criticality_score"],
        "criticality_label": rca["criticality_label"],
        "reference_time":      datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(detective_to_engineer, indent=4, ensure_ascii=False))
    # FIX C: add HTTP 200 OK + RESPONSE PAYLOAD for Step 6 → EngineerAgent handoff
    print("\n  API RESPONSE")
    print("  HTTP 200 OK")
    print("\n  RESPONSE PAYLOAD")
    print(json.dumps({
        "status":       "accepted",
        "eventId":      rca["eventId"],
        "root_cause":   rca["original_root_cause"],
        "domain":       rca["domain"],
        "message":      "RCA payload received by EngineerAgent — generating healing plan",
    }, indent=4, ensure_ascii=False))
    print("=" * 65)

    # ── Utility formula inputs ───────────────────────────────────────────────
    impact_score       = rca["impact_score"]
    criticality_score  = rca["criticality_score"]
    criticality_label  = rca["criticality_label"]
    biz_meta          = _fetch_business_metadata(rca["affected_entities"])
    impact_score_source = "detective_agent"
    risk_score          = rca["risk_score"]
    reversibility_score = rca["reversibility_score"]
    change_type_name    = rca["change_type_name"]

    # ── Build and rank branches ──────────────────────────────────────────────
    actions_def      = get_healing_actions(rca["normalized_root_cause"])
    branches = _build_branches(rca,actions_def,impact_score,criticality_score,risk_score,reversibility_score)
    top_utility      = branches[0]["utility_score"] if branches else 0.0
    utility_priority = _priority_from_utility(top_utility)

    intent_id = f"INT-{str(uuid.uuid4())[:8].upper()}"
    tmf921    = _build_tmf921_intent(
        rca, branches, utility_priority, criticality_label, intent_id
    )

    engineer_output = {
        "eventId": rca["eventId"],
        "source_event_id":    source_id,
        "intent_type": "TMF921_RCD_CONFIRMED_CAUSE",
        "intent_target":      "ExecutorAgent",
        "tmf921_intent":      tmf921,
        "root_cause":         rca["original_root_cause"],
        "root_cause_mapped":  rca["normalized_root_cause"],
        "domain":             rca["domain"],
        "priority":           utility_priority,
        "criticality":        criticality_label,
        "target_entities":    rca["affected_entities"],
        "ranked_healing_branches":   branches,
        "execution_sequence": [b["sequence"] for b in branches],
        "execution_order": [
        {
            "sequence":      b["sequence"],
            "domain":        b["domain"],
            "action":        b.get("action_command", {}).get("type"),
            "action_detail": b.get("action_command", {}).get("action_detail"),
            "utility_score": b["utility_score"],
        }
        for b in branches
    ],
        "utility_scoring": {
            "top_utility_score":      top_utility,
            "utility_priority":       utility_priority,
            "kpi_delta_pct":          rca["kpi_delta_pct"],
            "impact_score":          impact_score,
            "impact_score_source":   impact_score_source,
            "criticality_label":      criticality_label,
            "risk_score":             risk_score,
            "reversibility_score":    reversibility_score,
            "change_type_name":       change_type_name,
            "branch_count":           len(branches),
            "metadata_source":        os.environ.get("BIZ_METADATA_SOURCE", "mock"),
            "utility_scoring_active": len(branches) > 1,
        },
        "recovery_targets":  rca["recovery_targets"],
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "confidence_score":  rca["confidence_score"],
        "evidence_chain":    rca["evidence_chain"],
        "affected_cells":    rca["affected_cells"],
    }

    # ── Step 7 dashboard ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("[EngineerAgent — Step 7]")
    print("=" * 65)
    print(f"  Root Cause        : {rca['original_root_cause']}")
    print(f"  Normalized        : {rca['normalized_root_cause']}")
    print(f"  ACTIVATIONS.csv action: {actions_def.get('yaml_action', 'N/A')}")
    print(f"  Domain            : {rca['domain']}")
    print(f"  Confidence        : {rca['confidence_score'] * 100:.0f}%")
    print(f"  Change Request    : {rca['change_request_id']}")
    print(f"  Affected Entities : {rca['affected_entities']}")
    print(f"    impact_score      = {impact_score}")
    print(f"    criticality_score = {criticality_score}")
    print(f"    criticality_label = '{criticality_label}'")
    print(f"    risk_score        = {risk_score}")
    print(f"    reversibility     = {reversibility_score}")
    print("")
    print("  Utility formula: impact_score × criticality_score × (1-risk) × reversibility")
    print(f"    kpi_delta     = {rca['kpi_delta_pct']:.1f}%  ← PERFORMANCE.csv via Detective Agent")
    print(f"    impact_score = {impact_score}  ← {impact_score_source}")
    print(f"    criticality   = '{criticality_label}'  ← INSIGHT.csv (string label, NOT in formula)")
    print(f"    risk          = {risk_score}  ← CHANGEREQUEST.csv ({change_type_name})")
    print(f"    reversibility = {reversibility_score}  ← CHANGEREQUEST.csv")
    print("")
    print(f"  Healing branches ranked by utility (Sequence 1 = highest = Executor runs first):")
    for b in branches:
        print(f"")
        print(f"    ── Sequence {b['sequence']} ──────────────────────────────────────")
        print(f"    Domain          : {b['domain']}")
        print(f"    Root cause      : {b['root_cause']}")
        print(f"    Action (ACTIVATIONS.csv): {b.get('action_command', {}).get('type', 'N/A')}")
        print(f"    Action detail   : {b.get('action_command', {}).get('action_detail', 'N/A')}")
        print(f"    Description     : {b.get('action_command', {}).get('description', '')}")
        print(f"    Target entities : {b.get('target_entities', [])}")
        print(f"    Ranked sub-actions: {b.get('ranked_healing_actions', [])}")
        print(f"    Utility score   : {b['utility_score']} → {b['utility_priority']}")
        print(f"    Risk / Rev      : {b['risk_score']} / {b['reversibility_score']}")
    print("")
    print("  Recovery targets (from PERFORMANCE.csv via Detective Agent):")
    for rt in rca["recovery_targets"]:
        print(f"    {rt.get('target_metric')}: "
              f"{rt.get('current_value')} → {rt.get('target_value')} "
              f"(±{rt.get('tolerance_pct')}%)")

    executor_payload = {
        "eventId": rca["eventId"],
        "intent_type": "TMF921_RCD_CONFIRMED_CAUSE",
        "root_cause":      rca["original_root_cause"],
        "affected_entities": rca["affected_entities"],
        "utility_scoring": {
            "kpi_delta_pct":    rca["kpi_delta_pct"],
            "impact_score":    impact_score,
            "criticality":      criticality_label,
            "risk_score":       risk_score,
            "reversibility":    reversibility_score,
        },
        "ranked_healing_plan": [
            {
                "sequence":               b["sequence"],
                "domain":                 b["domain"],
                "utility_score":          b["utility_score"],
                "action":                 b.get("action_command", {}).get("type"),
                "action_detail":          b.get("action_command", {}).get("action_detail", ""),
                "description":            b.get("action_command", {}).get("description", ""),
                "action_command":         b.get("action_command", {}),
                "ranked_healing_actions": b.get("ranked_healing_actions", []),
                "target_entities":        b.get("target_entities", []),
            }
            for b in branches
        ],
        "tmf921_intent": {
            "intent_id":     tmf921["intent_id"],
            "intent_type":   tmf921["intent_type"],
            "priority":      tmf921["priority"],
            "criticality":   tmf921["criticality"],
            "target_entities": tmf921["target_entities"],
            "expressions":   tmf921["expressions"],
            "ranked_healing_branches": tmf921["ranked_healing_branches"],
        },
    }

    event = make_engineer_event(source_event_id=source_id, engineer_output=engineer_output)
    publish_event(state, event)
    state["engineer_last_event_id"] = rca_event["event_id"]
    state["engineer_output"]        = engineer_output
    state[NETWORK_STATUS_KEY]       = "HEALING"

    # FIX D: single POST /execute-healing-plan block with proper API RESPONSE section
    # (removed duplicate block that appeared earlier before event publish)
    print("\n" + "=" * 65)
    print("[Step 7] EngineerAgent → ExecutorAgent")
    print("=" * 65)
    print("\n  API CALL")
    print("  POST /execute-healing-plan")
    print("\n  REQUEST PAYLOAD")
    print(json.dumps(executor_payload, indent=4, ensure_ascii=False))

    engineer_response = {
        "status":           "EVENT_PUBLISHED",
        "target_agent":     "ExecutorAgent",
        "intent_id":        intent_id,
        "priority":         utility_priority,
        "root_cause":       rca["original_root_cause"],
        "root_cause_mapped": rca["normalized_root_cause"],
        "execution_order":  engineer_output["execution_order"],
        "tmf921_embedded":  True,
        "branch_count":     len(branches),
        "network_status":   "HEALING",
        "recovery_targets": [
            r["target_metric"]
            for r in rca["recovery_targets"]
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print("\n  API RESPONSE")
    print("  HTTP 200 OK")
    print("\n  RESPONSE PAYLOAD")
    print(json.dumps(engineer_response, indent=4, ensure_ascii=False))
    print("=" * 65)

    return {
        "status":            "EVENT_PUBLISHED",
        "published_event":   event["event_type"],
        "eventId": rca["eventId"],
        "intent_id":         intent_id,
        "root_cause":        rca["original_root_cause"],
        "root_cause_mapped": rca["normalized_root_cause"],
        "activations_action": actions_def.get("yaml_action"),
        "priority":          utility_priority,
        "criticality":       criticality_label,
        "top_utility_score": top_utility,
        "branch_count":      len(branches),
        "execution_order": [
            {
                "sequence":      b["sequence"],
                "domain":        b["domain"],
                "action":        b.get("action_command", {}).get("type"),
                "action_detail": b.get("action_command", {}).get("action_detail"),
                "utility_score": b["utility_score"],
            }
            for b in branches
        ],
        "target_entities":   rca["affected_entities"],
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "next_agent":        "ExecutorAgent",
        "network_status":    "HEALING",
    }