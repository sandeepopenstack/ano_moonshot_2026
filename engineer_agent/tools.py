import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import uuid
import os
import json
import logging
from datetime import datetime, timezone
from google.adk.tools import ToolContext

from shared.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,
    NETWORK_STATUS_KEY,
    consume_latest,
    make_engineer_event,
    publish_event,
)
from shared.remediation_config import (
    get_healing_actions,
    get_tilt_correction,
    normalize_root_cause,
    UTILITY_SCORING,
)


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


def _get_gnn_criticality_label(state: dict) -> str:
    gnn = state.get("latest_gnn_result", {})
    return gnn.get("criticality_label", "CRITICAL")


def _fetch_business_metadata(entity_ids: list[str]) -> dict[str, dict]:
    source = os.environ.get("BIZ_METADATA_SOURCE", "mock").lower()
    if source == "bigquery":
        try:
            return _fetch_from_bigquery(entity_ids)
        except Exception as e:
            logging.error(f"[EngineerAgent] BigQuery failed: {e} \u2014 structural fallback")
    return _fetch_structural_fallback(entity_ids)


def _fetch_structural_fallback(entity_ids: list[str]) -> dict[str, dict]:
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
    tier_weight = UTILITY_SCORING["tier_weight"]
    tier_rank   = {"Tier1": 3, "Tier2": 2, "Tier3": 1}
    best        = UTILITY_SCORING["fallback_metadata"]["biz_service_criticality_tier"]
    for eid in entity_ids:
        t = metadata.get(eid, {}).get("biz_service_criticality_tier", "Tier3")
        if tier_rank.get(t, 1) > tier_rank.get(best, 1):
            best = t
    return tier_weight.get(best, 0.7)


def _parse_rca(raw: dict) -> dict:
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
        "confirmed_rca_branches": raw.get("confirmedRcaBranches", raw.get("confirmed_rca_branches", [])),
        "business_priority":      raw.get("businessPriority", "CRITICAL"),
        "severity":               raw.get("severity", "P1"),
        "root_cause_description": raw.get("root_cause_description", ""),
        "timestamp_of_cause": raw.get("timestamp_of_cause", ""),
        "affected_hex_bins": raw.get("affected_hex_bins", []),
        "risk_score":             float(raw.get("risk_score", 0.5)),
        "reversibility_score":    float(raw.get("reversibility_score", 0.8)),
        "impact_score": float(raw.get("impact_score", 0.94)),
        "criticality_score": float(raw.get("criticality_score", 1.0)),
        "criticality_label": raw.get("criticality_label", "CRITICAL"),
        "change_type_name":       raw.get("change_type_name", "UNKNOWN"),
        "estimated_ttr_minutes":  int(raw.get("estimated_ttr_minutes", 0)),
    }


def _build_action_command(rca: dict, actions_def: dict) -> dict:
    root_cause  = rca["normalized_root_cause"].lower()
    causal      = rca["causal_parameters"]
    bounds      = actions_def.get("tmf915_parameter_bounds", {})
    yaml_action = actions_def.get("yaml_action", "UNKNOWN_ACTION")
    suggestions = rca.get("suggested_remediation") or []
    best        = suggestions[0] if suggestions else None
    alternatives = suggestions[1:] if len(suggestions) > 1 else []

    if root_cause in ("bad_antenna_tilt_push", "antenna_tilt_misconfiguration"):
        current_tilt = causal.get("current_value")
        target_tilt  = causal.get("previous_value")
        if target_tilt is None and best:
            target_tilt = best.get("value")
        if current_tilt is not None and target_tilt is not None:
            correction = get_tilt_correction(float(current_tilt), float(target_tilt))
            return {
                "type":               yaml_action,
                "action_detail":      "ANTENNA_TILT_ADJUST",
                "current_degrees":    correction["current_tilt_degrees"],
                "target_degrees":     correction["target_tilt_degrees"],
                "delta_degrees":      correction["correction_delta"],
                "within_safe_bounds": correction["within_safe_bounds"],
                "clamped":            correction["clamped"],
                "description": (
                    f"Rollback tilt: {current_tilt}\u00b0 \u2192 "
                    f"{correction['target_tilt_degrees']}\u00b0 "
                    f"(delta {correction['correction_delta']:+.2f}\u00b0"
                    + (", CLAMPED to safe bound \u00b15\u00b0" if correction["clamped"] else "") + ")"
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

    if root_cause in (
        "hss_stale_session_loop", "hss_saturation",
        "hss_session_table_overflow", "hss_subscriber_db_saturation",
    ):
        return {
            "type":                yaml_action,
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

    if root_cause in (
        "fiber_cut", "path_degradation", "physical_fiber_cut_backhaul",
        "transport_path_failure", "transport_path_degradation",
    ):
        return {
            "type":           yaml_action,
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
                    f"Rollback tilt: {current}\u00b0 \u2192 {correction['target_tilt_degrees']}\u00b0"
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


def _build_branches(
    rca,
    actions_def,
    impact_score,
    criticality_score,
    risk_score,
    reversibility,
)-> list[dict]:
    branches: list[dict] = []

    if rca["confirmed_rca_branches"] and (
        rca["domain"] == "CROSS_DOMAIN"
        or len({b.get("domain") for b in rca["confirmed_rca_branches"]}) > 1
    ):
        branches = []
        for branch in rca["confirmed_rca_branches"]:
            b_domain     = branch.get("domain", rca["domain"])
            b_root_cause = branch.get("root_cause", "")
            b_rc_norm    = normalize_root_cause(b_root_cause)
            act_def      = get_healing_actions(b_rc_norm)

            b_risk       = float(branch.get("risk_score", risk_score))
            b_reversible = float(branch.get("reversibility", reversibility))
            b_priority   = float(branch.get("priority_score", 10))

            b_kpi   = rca["kpi_delta_pct"] * (b_priority / 10.0)
            utility = _compute_utility(impact_score, criticality_score, b_risk, b_reversible)

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
        from providers.detective_provider import _DOMAIN_DEFAULTS as _DD

        branches = []

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
                domain_entity_map.setdefault("RAN", []).append(eid)

        for branch_domain, domain_eids in domain_entity_map.items():
            domain_cfg    = _DD.get(branch_domain, _DD["RAN"])
            branch_rc     = domain_cfg["root_cause"]
            branch_risk   = domain_cfg["risk_score"]
            branch_rev    = domain_cfg["reversibility_score"]

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
        suggestions = rca.get("suggested_remediation") or []

        if suggestions:
            for idx, suggestion in enumerate(suggestions):
                option_risk = min(risk_score + (0.1 * idx), 1.0)
                option_utility = _compute_utility(
                    impact_score, criticality_score, option_risk, reversibility
                )

                option_target = (
                    [suggestion["target"]]
                    if suggestion.get("target")
                    else rca["affected_entities"]
                )

                option_actions_def = get_healing_actions(rca["normalized_root_cause"])

                if suggestion.get("action") == "accept_degradation":
                    option_action_cmd = {
                        "type":          "ACCEPT_DEGRADATION",
                        "action_detail": "NO_ACTION",
                        "description":   suggestion.get("note", "Accept degradation \u2014 no remediation applied"),
                        "parameter_name": "NoOp",
                    }
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

    branches.sort(key=lambda b: b["utility_score"], reverse=True)
    for i, b in enumerate(branches):
        b["sequence"] = i + 1

    return branches


def _build_tmf921_intent(
    rca:                dict,
    branches:           list[dict],
    utility_priority:   str,
    criticality_label:  str,
    intent_id:          str,
) -> dict:
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
            f"{len(rca['affected_entities'])} entities \u2014 "
            f"{len(branches)} branch(es) ranked by utility score"
        ),
        "root_cause":        rca["original_root_cause"],
        "root_cause_mapped": rca["normalized_root_cause"],
        "root_cause_entity": root_cause_node,
        "domain":            rca["domain"],
        "priority":          utility_priority,
        "criticality":       criticality_label,
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


def generate_healing_plan(tool_context: ToolContext) -> dict:
    state = tool_context.state

    rca_event = consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)
    if not rca_event:
        return {"status": "IDLE", "reason": "No detective.rca.confirmed event in state"}

    if state.get("engineer_last_event_id") == rca_event["event_id"]:
        return {"status": "SKIPPED", "event_id": rca_event["event_id"],
                "reason": "Already processed this RCA event"}

    source_id = rca_event["event_id"]
    rca       = _parse_rca(rca_event["payload"])

    print("\n" + "=" * 65)
    print("[Step 6] Detective Agent \u2192 EngineerAgent")
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
        "impact_score": rca["impact_score"],
        "criticality_score": rca["criticality_score"],
        "criticality_label": rca["criticality_label"],
        "reference_time":      datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(detective_to_engineer, indent=4, ensure_ascii=False))
    print("\n  API RESPONSE")
    print("  HTTP 200 OK")
    print("\n  RESPONSE PAYLOAD")
    print(json.dumps({
        "status":       "accepted",
        "eventId":      rca["eventId"],
        "root_cause":   rca["original_root_cause"],
        "domain":       rca["domain"],
        "message":      "RCA payload received by EngineerAgent \u2014 generating healing plan",
    }, indent=4, ensure_ascii=False))
    print("=" * 65)

    impact_score       = rca["impact_score"]
    criticality_score  = rca["criticality_score"]
    criticality_label  = rca["criticality_label"]
    biz_meta          = _fetch_business_metadata(rca["affected_entities"])
    impact_score_source = "detective_agent"
    risk_score          = rca["risk_score"]
    reversibility_score = rca["reversibility_score"]
    change_type_name    = rca["change_type_name"]

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

    print("\n" + "=" * 65)
    print("[EngineerAgent \u2014 Step 7]")
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
    print("  Utility formula: impact_score \u00d7 criticality_score \u00d7 (1-risk) \u00d7 reversibility")
    print(f"    kpi_delta     = {rca['kpi_delta_pct']:.1f}%  \u2190 PERFORMANCE.csv via Detective Agent")
    print(f"    impact_score = {impact_score}  \u2190 {impact_score_source}")
    print(f"    criticality   = '{criticality_label}'  \u2190 INSIGHT.csv (string label, NOT in formula)")
    print(f"    risk          = {risk_score}  \u2190 CHANGEREQUEST.csv ({change_type_name})")
    print(f"    reversibility = {reversibility_score}  \u2190 CHANGEREQUEST.csv")
    print("")
    print(f"  Healing branches ranked by utility (Sequence 1 = highest = Executor runs first):")
    for b in branches:
        print(f"")
        print(f"    \u2500\u2500 Sequence {b['sequence']} \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
        print(f"    Domain          : {b['domain']}")
        print(f"    Root cause      : {b['root_cause']}")
        print(f"    Action (ACTIVATIONS.csv): {b.get('action_command', {}).get('type', 'N/A')}")
        print(f"    Action detail   : {b.get('action_command', {}).get('action_detail', 'N/A')}")
        print(f"    Description     : {b.get('action_command', {}).get('description', '')}")
        print(f"    Target entities : {b.get('target_entities', [])}")
        print(f"    Ranked sub-actions: {b.get('ranked_healing_actions', [])}")
        print(f"    Utility score   : {b['utility_score']} \u2192 {b['utility_priority']}")
        print(f"    Risk / Rev      : {b['risk_score']} / {b['reversibility_score']}")
    print("")
    print("  Recovery targets (from PERFORMANCE.csv via Detective Agent):")
    for rt in rca["recovery_targets"]:
        print(f"    {rt.get('target_metric')}: "
              f"{rt.get('current_value')} \u2192 {rt.get('target_value')} "
              f"(\u00b1{rt.get('tolerance_pct')}%)")

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

    print("\n" + "=" * 65)
    print("[Step 7] EngineerAgent \u2192 ExecutorAgent")
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
        "event_id":          source_id,
        "target_agent":      "ExecutorAgent",
        "intent_id":         intent_id,
        "priority":          utility_priority,
        "execution_order":   engineer_output["execution_order"],
        "branch_count":      len(branches),
        "top_utility_score": top_utility,
        "root_cause":        rca["original_root_cause"],
        "network_status":    "HEALING",
    }
