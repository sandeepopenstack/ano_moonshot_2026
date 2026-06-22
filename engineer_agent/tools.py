"""
app/agents/engineer_agent/tools.py
====================================
EngineerAgent — single tool: generate_healing_plan.
"""

import uuid
import os
import json
import requests
import logging

logging.basicConfig(level=logging.INFO)

from datetime import datetime, timezone
from google.adk.tools import ToolContext

from ran_healing_shared.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,
    NETWORK_STATUS_KEY,
    consume_latest,
    make_engineer_event,
    publish_event,
)
from ran_healing_shared.remediation_config import UTILITY_SCORING
from engineer_agent.step_events import emit_step   # SSE step streaming

EXECUTOR_AGENT_URL = os.environ.get("EXECUTOR_AGENT_URL", "http://10.63.4.22:8000").rstrip("/")

# ══════════════════════════════════════════════════════════════════════════════
# Natural language log helpers — MODULE LEVEL
# ══════════════════════════════════════════════════════════════════════════════
def _log_6_rca_received(rca: dict) -> None:
    """Step 6 — RCA payload received from Detective Agent."""
    event_id    = rca.get("eventId", "unknown")
    root_cause  = rca.get("root_cause", "unknown")
    domain      = rca.get("domain", "unknown")
    confidence  = rca.get("confidence_score", 0.0)
    risk        = rca.get("risk_score", 0.0)
    reversible  = rca.get("reversibility_score", 0.0)
    entities    = rca.get("affected_entities", [])
    suggestions = rca.get("suggested_remediation", [])
    cr_id       = rca.get("change_request_id", "unknown")
    impact      = rca.get("impact_score", 0.0)
    criticality = rca.get("criticality_label", "unknown")

    rc_human = {
        "antenna_tilt_misconfiguration": "a bad antenna tilt push",
        "physical_fiber_cut_backhaul":   "a physical fiber cut on the backhaul",
        "hss_subscriber_db_saturation":  "HSS subscriber database saturation",
        "hss_stale_session_loop":        "stale HSS session loop",
        "fiber_cut":                     "a fiber cut on the transport layer",
        "transport_path_failure":        "transport path failure",
    }.get(root_cause.lower(), root_cause.replace("_", " "))

    logging.info(
        f"[Step 6] Detective Agent has completed its investigation for event '{event_id}'. "
        f"Root cause confirmed: {rc_human} on the {domain} domain "
        f"(change request {cr_id}, confidence {confidence}). "
        f"Impact score {impact} — {criticality}. "
        f"Risk of remediation: {risk}, reversibility: {reversible}. "
        f"There are {len(suggestions)} remediation options to score. "
        f"Affected entities: {entities}. "
        f"Now running utility scoring to rank the options. "
        f"eventId={event_id} | root_cause={root_cause} | domain={domain} | "
        f"confidence={confidence} | risk={risk} | reversibility={reversible} | "
        f"impact_score={impact} | criticality_score={rca.get('criticality_score', 0)} | "
        f"remediation_options={len(suggestions)}"
    )
    logging.info(
        f"[Step 6] Detective RCA Payload | "
        f"{json.dumps(rca, default=str)}"
    )


def _log_7_utility_scoring(
    rca:              dict,
    branches:         list,
    top_utility:      float,
    utility_priority: str,
) -> None:
    """Step 7 — Utility scoring and branch ranking complete."""
    event_id    = rca.get("eventId", "unknown")
    impact      = rca.get("impact_score", 0.0)
    criticality = rca.get("criticality_score", 0.0)
    reversible  = rca.get("reversibility_score", 0.0)

    seq1        = branches[0] if branches else {}
    seq1_action = seq1.get("action", "unknown")
    seq1_domain = seq1.get("domain", rca.get("domain", "unknown"))
    seq1_util   = seq1.get("utility_score", 0.0)
    seq1_impact = seq1.get("impact_score", impact)
    seq1_criticality = seq1.get("criticality_score", criticality)
    seq1_risk = seq1.get("risk_score", 0.0)
    seq1_reversibility = seq1.get("reversibility_score", reversible)

    logging.info(
        f"[Step 7] Utility scoring complete for event '{event_id}'. "
        f"Formula: {impact} (impact) × {criticality} (criticality) "
        f"× (1 − risk) × {reversible} (reversibility). "
        f"Scored {len(branches)} {'branch' if len(branches) == 1 else 'branches'}, "
        f"ranked highest to lowest utility. "
        f"Top branch → sequence 1: '{seq1_action}' on {seq1_domain} "
        f"(utility {seq1_util}, priority {utility_priority}). "
        f"This is what the Executor will run first. "
        f"eventId={event_id} | branches={len(branches)} | "
        f"top_utility={top_utility} | utility_priority={utility_priority} | "
        f"Formula for top branch: {seq1_impact} (impact) × "f"{seq1_criticality} (criticality) × "f"(1 − {seq1_risk}) × {seq1_reversibility} (reversibility). ")
    execution_order = [
        {
            "sequence":      b.get("sequence"),
            "option":        b.get("option", ""),
            "domain":        b.get("domain"),
            "action":        b.get("action"),
            "utility_score": b.get("utility_score"),
            "risk_score":    b.get("risk_score"),
        }
        for b in branches
    ]
    logging.info(
        f"[Step 7] Ranked Healing Branches | "
        f"{json.dumps(execution_order, default=str)}"
    )


def _log_7_tmf921_built(
    rca:              dict,
    intent_id:        str,
    utility_priority: str,
    n_expressions:    int,
) -> None:
    """Step 7 — TMF921 intent built."""
    event_id    = rca.get("eventId", "unknown")
    criticality = rca.get("criticality_label", "unknown")
    domain      = rca.get("domain", "unknown")

    logging.info(
        f"[Step 7] TMF921 healing intent built for event '{event_id}'. "
        f"Intent '{intent_id}' — "
        f"{domain} domain, priority {utility_priority}, criticality {criticality}. "
        f"Includes {n_expressions} KPI recovery "
        f"{'target' if n_expressions == 1 else 'targets'} "
        f"from the Detective Agent for the Executor to validate against. "
        f"Sending to the Executor now. "
        f"eventId={event_id} | intent_id={intent_id} | "
        f"priority={utility_priority} | criticality={criticality} | "
        f"expressions={n_expressions}"
    )


def _log_7_out_calling_executor(
    rca: dict,
    executor_url: str,
    intent_id: str,
    branches: list,
) -> None:
    """Step 7 OUT — Calling ExecutorAgent."""
    event_id = rca.get("eventId", "unknown")
    domain = rca.get("domain", "unknown")
    priority = branches[0].get("utility_priority", "unknown") if branches else "unknown"
    top_branch = branches[0] if branches else {}

    top_action = top_branch.get("action", "unknown")
    top_target_entities = top_branch.get("target_entities", [])
    top_param = top_branch.get("param", "")
    top_direction = top_branch.get("direction", "")
    n_branches = len(branches)

    logging.info(
        f"[Step 7 OUT] Handing the healing plan to the Executor for event '{event_id}'. "
        f"Top-ranked action is '{top_action}' on {domain}. "
        f"Targets: {top_target_entities}. "
        f"Parameter: {top_param or 'n/a'}, direction: {top_direction or 'n/a'}. "
        f"Sending {n_branches} ranked {'branch' if n_branches == 1 else 'branches'} — "
        f"Executor should start with sequence 1 and escalate only if needed. "
        f"eventId={event_id} | url={executor_url} | intent_id={intent_id} | "
        f"priority={priority} | branch_count={n_branches}"
    )


def _log_7_out_executor_request(executor_payload: dict) -> None:
    logging.info(
        f"[Step 7 OUT] Executor Request Payload | "
        f"{json.dumps(executor_payload, default=str)}"
    )


def _log_7_out_executor_response(rca: dict, executor_response: dict) -> None:
    """Step 7 OUT — Executor response received."""
    event_id = rca.get("eventId", "unknown")
    intent_id = executor_response.get("intent_id", "unknown")
    exec_state = executor_response.get("state", "unknown")
    success = bool(executor_response.get("success", False))
    error = executor_response.get("error", "")

    if success and str(exec_state).lower() == "completed":
        logging.info(
            f"[Step 7 OUT] Executor accepted and completed the healing plan for event '{event_id}'. "
            f"Intent '{intent_id}' is now '{exec_state}'. "
            f"The remediation execution completed successfully. "
            f"Engineer will publish engineer.healing.plan.ready and Reflection can validate Gate 1. "
            f"eventId={event_id} | intent_id={intent_id} | "
            f"state={exec_state} | success={success}"
        )
    else:
        logging.warning(
            f"[Step 7 OUT] Executor failed the healing plan for event '{event_id}'. "
            f"Intent '{intent_id}' is now '{exec_state}'. "
            f"Executor error: {error or 'unknown'}. "
            f"Engineer will not publish engineer.healing.plan.ready for this failed execution. "
            f"Reflection Gate 1 would fail because execution success/state is not completed. "
            f"eventId={event_id} | intent_id={intent_id} | "
            f"state={exec_state} | success={success} | error={error}"
        )

    logging.info(
        f"[Step 7 OUT] Executor Response Payload | "
        f"{json.dumps(executor_response, default=str)}"
    )


def _log_7_out_response_summary(
    rca:              dict,
    intent_id:        str,
    utility_priority: str,
    n_branches:       int,
    execution_order:  list,
) -> None:
    """Step 7 OUT — Final engineer response summary."""
    event_id = rca.get("eventId", "unknown")
    logging.info(
        f"[Step 7 OUT] Engineer Response Payload | "
        f"{json.dumps({'status': 'EVENT_PUBLISHED', 'eventId': event_id, 'intent_id': intent_id, 'priority': utility_priority, 'branch_count': n_branches, 'execution_order': execution_order, 'network_status': 'HEALING'}, default=str)}"
    )


# ── Utility scoring ────────────────────────────────────────────────────────────

def _compute_utility(
    impact_score:      float,
    criticality_score: float,
    risk_score:        float,
    reversibility:     float,
) -> float:
    """
    Utility = impact_score x criticality_score x (1 - risk) x reversibility
    All inputs come from Detective Agent payload.
    Sequence 1 = highest utility = Executor runs this first.
    """
    return round(
        impact_score * criticality_score * (1.0 - risk_score) * reversibility,
        4,
    )


def _priority_from_utility(utility: float) -> str:
    t = UTILITY_SCORING["priority_thresholds"]
    if utility >= t["CRITICAL"]: return "CRITICAL"
    if utility >= t["HIGH"]:     return "HIGH"
    if utility >= t["MEDIUM"]:   return "MEDIUM"
    return "LOW"


def _build_intent_id(rca: dict) -> str:
    if rca["change_request_id"]:
        return f"INTENT-{rca['change_request_id']}"
    if rca["hypothesis_id"]:
        return f"INTENT-{rca['hypothesis_id']}"
    if rca["eventId"]:
        return f"INTENT-{rca['eventId']}"
    return "INTENT-UNKNOWN"

# ── Build Action command per branch ───────────────────────────────────────── 
def _build_action_command(suggestion: dict,causal_params: dict) -> dict:
    """
    Parse Detective Agent RCA payload (investigation.rca.confirmed).
    All scores sourced from Detective Agent — no domain knowledge here.
    """
    action = suggestion.get("action", "")

    # No concrete command
    if action == "accept_degradation" or not action:
        return {
            "action":    action or "no_action",
            "parameter_name":       None,
            "current_value":        None,
            "target_value":         None,
            "direction":            "no_action",
            "target_entity":        None,
        }

    # Concrete command
    # 1. suggestion.value if detective provided it
    # 2. else causal_parameters.previous_value

    target_value = suggestion.get("value")
    if target_value is None:
        target_value = causal_params.get("previous_value")

    return {
        "action":      action,
        "parameter_name": suggestion.get("param") or causal_params.get("parameter",""),
        "current_value":  causal_params.get("current_value"),
        "target_value":   target_value,
        "direction":      suggestion.get("direction", ""),
        "target_entity":   suggestion.get("target", ""),
    }

# ── Parse Detective Agent RCA payload ─────────────────────────────────────────

def _parse_rca(raw: dict) -> dict:
    """
    Parse Detective Agent RCA payload (investigation.rca.confirmed).
    All scores sourced from Detective Agent — no domain knowledge here.
    """
    kpi_impact = raw.get("kpi_impact", {})
    raw_delta  = (
        kpi_impact.get("primary_metric_delta_pct")
        or raw.get("kpi_delta_pct")
        or UTILITY_SCORING.get("default_kpi_delta_pct", 30.0)
    )

    return {
        "eventId":                raw.get("eventId") or raw.get("event_id", ""),
        "hypothesis_id":          raw.get("hypothesis_id", ""),
        "change_request_id":      raw.get("change_request_id", ""),
        "incident_type":          raw.get("incident_type", ""),
        "ranked_list": raw.get("ranked_list", []),
        "root_cause":             raw.get("root_cause", ""),
        "root_cause_description": raw.get("root_cause_description", ""),
        "timestamp_of_cause":     raw.get("timestamp_of_cause", ""),
        "domain":                 raw.get("domain", "UNKNOWN"),
        "confidence_label":       raw.get("confidence", ""),
        "confidence_score":       float(raw.get("confidence_score", 0.0)),
        "affected_entities":      raw.get("affected_entities",raw.get("entity_ids", [])),
        "neighbor_entities":      raw.get("neighbor_entities", []),
        "affected_cells":         raw.get("affected_cells", []),
        "affected_hex_bins":      raw.get("affected_hex_bins", []),
        "alarm_ids":              raw.get("alarm_ids", []),
        "primary_resource":       raw.get("primary_resource", {}),
        "causal_parameters":      raw.get("causal_parameters", {}),
        "suggested_remediation":  raw.get("suggested_remediation", []),
        "recovery_targets":       raw.get("recovery_targets", []),
        "kpi_impact":             kpi_impact,
        "kpi_delta_pct":          abs(float(raw_delta)),
        "evidence_chain":         raw.get("evidence_chain", []),
        "confirmed_rca_branches": raw.get(
            "confirmedRcaBranches",
            raw.get("confirmed_rca_branches", [])
        ),
        # Scores from Detective Agent (CHANGEREQUEST.csv + YAML resolution.*)
        "risk_score":             float(raw.get("risk_score", 0.5)),
        "reversibility_score":    float(raw.get("reversibility_score", 0.8)),
        # Scores from Detective Agent (INSIGHT.csv via GNN)
        "impact_score":           float(raw.get("impact_score", 0.94)),
        "criticality_score":      float(raw.get("criticality_score", 1.0)),
        "criticality_label":      raw.get("criticality_label", "CRITICAL"),
        # YAML resolution.ttr_minutes via Detective Agent
        "estimated_ttr_minutes":  int(raw.get("estimated_ttr_minutes", 0)),
        "severity":               raw.get("severity", "P1"),
        "business_priority":      raw.get("businessPriority", "CRITICAL"),
        "change_type_name":       raw.get("change_type_name", "UNKNOWN"),
    }

def _build_ranked_entity_index(ranked_list: list[dict]) -> dict:
    """
    Build lookup index from Reflex/Detective ranked_list.

    entity_id -> impact_score, criticality_score, criticality_label, domain, rank
    """
    entity_index = {}

    for item in ranked_list or []:
        entity_id = item.get("entity_id") or item.get("eid") or item.get("node_id")
        if not entity_id:
            continue

        entity_index[entity_id] = {
            "rank": item.get("rank"),
            "domain": item.get("domain", "UNKNOWN"),
            "priority_flag": item.get("priority_flag", ""),
            "impact_score": float(item.get("impact_score", 0.0)),
            "criticality_score": float(item.get("criticality_score", 0.0)),
            "criticality_label": item.get("criticality_label", "UNKNOWN"),
        }

    return entity_index


def _resolve_branch_scores(
    branch_or_suggestion: dict,
    rca: dict,
    entity_index: dict,
) -> dict:
    """
    Resolve branch impact/criticality using source_entity.

    Priority:
    1. source_entity from Detective suggestion/branch
    2. target if target exists in ranked_list
    3. rank-1 entity from ranked_list
    4. top-level RCA fallback
    """
    source_entity = branch_or_suggestion.get("source_entity", "")
    target_entity = branch_or_suggestion.get("target", "")
    
    target_entities = branch_or_suggestion.get("target_entities", [])
    if isinstance(target_entities, str):
        target_entities = [target_entities]
    
    lookup_entity = source_entity
    
    if not lookup_entity and target_entity in entity_index:
        lookup_entity = target_entity
    
    if not lookup_entity:
        for target_candidate in target_entities:
            if target_candidate in entity_index:
                lookup_entity = target_candidate
                break
    

    if not lookup_entity and rca.get("ranked_list"):
        top_ranked = rca["ranked_list"][0]
        lookup_entity = (
            top_ranked.get("entity_id")
            or top_ranked.get("eid")
            or top_ranked.get("node_id", "")
        )

    scores = entity_index.get(lookup_entity, {})

    return {
        "source_entity": lookup_entity,
        "impact_score": float(
            branch_or_suggestion.get(
                "impact_score",
                scores.get("impact_score", rca["impact_score"])
            )
        ),
        "criticality_score": float(
            branch_or_suggestion.get(
                "criticality_score",
                scores.get("criticality_score", rca["criticality_score"])
            )
        ),
        "criticality_label": branch_or_suggestion.get(
            "criticality_label",
            scores.get("criticality_label", rca["criticality_label"])
        ),
        "source_domain": scores.get("domain", rca.get("domain", "UNKNOWN")),
        "source_rank": scores.get("rank"),
        "source_priority_flag": scores.get("priority_flag", ""),
    }

# ── Build utility-scored branches ──────────────────────────────────────────────

def _build_branches(rca: dict) -> list[dict]:
    """
    Build one branch per remediation option and rank by utility score.

    Cross-domain (confirmedRcaBranches from Detective):
      Each branch has its own domain-specific risk + reversibility from Detective.
      Utility computed independently → branch with lowest risk + highest
      reversibility gets sequence 1, regardless of domain name.
      Example: RAN(risk=0.4,rev=0.95) → util=0.5358 → seq 1
               TRANSPORT(risk=0.5,rev=0.90) → util=0.423 → seq 2
               CORE(risk=0.6,rev=0.70) → util=0.2632 → seq 3

    Single-domain (suggested_remediation from Detective):
      Option A: base risk → highest utility → seq 1

    Sequence assigned AFTER sort — pure utility math, no hardcoding.
    """
    impact_score       = rca["impact_score"]
    criticality_score  = rca["criticality_score"]
    base_risk          = rca["risk_score"]
    base_reversibility = rca["reversibility_score"]
    branches: list[dict] = []

    entity_index = _build_ranked_entity_index(rca.get("ranked_list", []))

    # ── Cross-domain: 1 branch per domain from Detective confirmedRcaBranches ──
    if rca["confirmed_rca_branches"]:
        for branch in rca["confirmed_rca_branches"]:

            branch_scores = _resolve_branch_scores(branch, rca, entity_index,)
            b_impact = branch_scores["impact_score"]
            b_criticality = branch_scores["criticality_score"]
            b_criticality_label = branch_scores["criticality_label"]
            source_entity = branch_scores["source_entity"]
            
            b_risk = float(branch.get("risk_score", base_risk))
            b_reversible = float(
                branch.get(
                    "reversibility_score",
                    branch.get("reversibility", base_reversibility)
                )
            )
            
            utility = _compute_utility(
                b_impact,
                b_criticality,
                b_risk,
                b_reversible,
            )
            
            branches.append({
                "domain":              branch.get("domain", rca["domain"]),
                "root_cause":          branch.get("root_cause", rca["root_cause"]),
                "action":              branch.get("action", ""),
                "action_detail":       branch.get("action_detail", ""),
                "description":         branch.get("description", branch.get("note", "")),
                "target_entities":     branch.get("target_entities", rca["affected_entities"]),
                "causal_parameters":   branch.get("causal_parameters", rca["causal_parameters"]),
                "param":               branch.get("param", ""),
                "value":               branch.get("value"),
                "direction":           branch.get("direction", ""),
                "risk_score":          b_risk,
                "reversibility_score": b_reversible,
                "source_entity":       source_entity,
                "source_rank":         branch_scores.get("source_rank"),
                "source_priority_flag":branch_scores.get("source_priority_flag", ""),
                "impact_score":        b_impact,
                "criticality_score":   b_criticality,
                "criticality_label":   b_criticality_label,
                "utility_score":       utility,
                "utility_priority":    _priority_from_utility(utility),
                "action_source":       "detective_confirmed_rca_branches",
                "priority_score":      float(branch.get("priority_score", 10)),
                "action_command":      _build_action_command(branch, branch.get("causal_parameters", rca["causal_parameters"])),
            })

    # ── Single-domain: 1 branch per suggested_remediation option ───────────────
    elif rca["suggested_remediation"]:
        for idx, suggestion in enumerate(rca["suggested_remediation"]):
    
            branch_scores = _resolve_branch_scores(
                suggestion,
                rca,
                entity_index,
            )
    
            branch_impact_score = branch_scores["impact_score"]
            branch_criticality_score = branch_scores["criticality_score"]
            branch_criticality_label = branch_scores["criticality_label"]
            source_entity = branch_scores["source_entity"]
    
            # Risk and reversibility are branch-level values from Detective.
            option_risk = float(
                suggestion.get("risk_score", base_risk)
            )
    
            branch_reversibility = float(
                suggestion.get("reversibility_score", base_reversibility)
            )
    
            is_noop = suggestion.get("action") == "accept_degradation"

            # Guardrail: do not allow accept_degradation/no-op to rank first
            # when Detective sends risk_score=0.0 and reversibility_score=1.0.
            if is_noop:
                option_risk = max(option_risk, 0.9)
            
            utility = _compute_utility(
                branch_impact_score,
                branch_criticality_score,
                option_risk,
                branch_reversibility,
            )
    
            target_entities = (
                [suggestion["target"]]
                if suggestion.get("target")
                else rca["affected_entities"]
            )
    
            branches.append({
                "domain":              rca["domain"],
                "root_cause":          rca["root_cause"],
                "option":              suggestion.get("option", chr(65 + idx)),
                "action":              suggestion.get("action", ""),
                "action_detail":       "",
                "description":         suggestion.get("note", ""),
                "target_entities":     target_entities,
    
                # Important: source entity is impacted/root entity used for scoring.
                "source_entity":       source_entity,
                "source_rank":         branch_scores.get("source_rank"),
                "source_priority_flag": branch_scores.get("source_priority_flag", ""),
    
                "param":               suggestion.get("param", ""),
                "value":               suggestion.get("value"),
                "direction":           suggestion.get("direction", ""),
                "causal_parameters":   rca["causal_parameters"],
                "is_noop":             is_noop,
    
                # Utility inputs
                "risk_score":          option_risk,
                "reversibility_score": branch_reversibility,
                "impact_score":        branch_impact_score,
                "criticality_score":   branch_criticality_score,
                "criticality_label":   branch_criticality_label,
    
                # Utility output
                "utility_score":       utility,
                "utility_priority":    _priority_from_utility(utility),
    
                "action_source":       "detective_suggested_remediation",
                "priority_score":      float(10 - idx),
                "action_command":      _build_action_command(suggestion, rca["causal_parameters"],),
            })

    # ── Fallback: no remediation options from Detective Agent ──────────────────
    else:
        utility = _compute_utility(impact_score, criticality_score, base_risk, base_reversibility)
        branches.append({
            "domain":              rca["domain"],
            "root_cause":          rca["root_cause"],
            "option":              "A",
            "action":              "MANUAL_INVESTIGATION_REQUIRED",
            "action_detail":       "",
            "description":         f"No remediation options provided by Detective Agent for {rca['root_cause']}",
            "target_entities":     rca["affected_entities"],
            "causal_parameters":   rca["causal_parameters"],
            "param":               "",
            "value":               None,
            "direction":           "",
            "risk_score":          base_risk,
            "reversibility_score": base_reversibility,
            "impact_score":        impact_score,
            "criticality_score":   criticality_score,
            "utility_score":       utility,
            "utility_priority":    _priority_from_utility(utility),
            "action_source":       "fallback",
            "priority_score":      10.0,
            "action_command":      {
                "action": "MANUAL_INVESTIGATION_REQUIRED",
                "parameter_name": None, "current_value": None,
                "target_value": None, "direction": "manual",
                "target_entity": None,
            },
        })

    # Sort descending by utility → assign sequence AFTER sort
    branches.sort(key=lambda b: b["utility_score"], reverse=True)
    for i, b in enumerate(branches):
        b["sequence"] = i + 1

    return branches


# ── Build TMF921 Intent ────────────────────────────────────────────────────────

def _build_tmf921_intent(
    rca:              dict,
    branches:         list[dict],
    utility_priority: str,
    intent_id:        str,
) -> dict:
    """
    Build TMF921-aligned intent.
    activation_id is NOT set here — ExecutorAgent generates it.
    ReflectionAgent reads activation_id from ExecutorAgent response payload.
    """
    expressions = [
        {
            "target_metric": t.get("target_metric"),
            "target_value":  t.get("target_value"),
            "current_value": t.get("current_value"),
            "tolerance_pct": t.get("tolerance_pct"),
        }
        for t in (rca["recovery_targets"] or [])
        if t.get("target_metric")
    ]

    root_cause_node = (
        rca["primary_resource"].get("node_id", "")
        if rca.get("primary_resource") else ""
    )

    return {
        "intent_id":         intent_id,
        "intent_type":       "remediation",
        "description": (
            f"Remediate {rca['root_cause']} on "
            f"{len(rca['affected_entities'])} entities — "
            f"{len(branches)} branch(es) ranked by utility score"
        ),
        "root_cause":        rca["root_cause"],
        "root_cause_entity": root_cause_node,
        "domain":            rca["domain"],
        "priority":          utility_priority,
        "criticality":       rca["criticality_label"],
        "target_entities":   rca["affected_entities"],
        "affected_hex_bins": rca["affected_hex_bins"],
        "expressions":       expressions,
        "ranked_healing_branches": [
            {
                "sequence":            b["sequence"],
                "domain":              b["domain"],
                "option":              b.get("option", ""),
                "action":              b["action"],
                "action_detail":       b.get("action_detail", ""),
                "description":         b["description"],
                "target":              b["target_entities"],
                "param":               b.get("param", ""),
                "value":               b.get("value"),
                "direction":           b.get("direction", ""),
                "utility_score":       b["utility_score"],
                "utility_priority":    b["utility_priority"],
                "risk_score":          b["risk_score"],
                "reversibility_score": b["reversibility_score"],
                "action_command":      b.get("action_command", {}),
            }
            for b in branches
        ],
        "constraints": {
            "estimated_ttr_minutes": rca["estimated_ttr_minutes"] or None,
            "reversible_required":   True,
            "maintenance_window":    "immediate",
        },
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "evidence_chain":    rca["evidence_chain"],
        "confidence_score":  rca["confidence_score"],
        "created_at":        datetime.now(timezone.utc).isoformat(),
        "created_by":        "EngineerAgent",
    }


# ── Main tool ──────────────────────────────────────────────────────────────────

def generate_healing_plan(tool_context: ToolContext) -> dict:
    """
    Single tool — NO arguments.
    Step 7: IN: investigation.rca.confirmed → OUT: TMF921 → POST ExecutorAgent.
    Idempotent: skips if already processed this RCA event_id.
    """
    state = tool_context.state

    rca_event = consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)
    if not rca_event:
        return {"status": "IDLE", "reason": "No investigation.rca.confirmed event in state"}

    if state.get("engineer_last_event_id") == rca_event["event_id"]:
        return {
            "status":   "SKIPPED",
            "event_id": rca_event["event_id"],
            "reason":   "Already processed this RCA event",
        }

    source_id = rca_event["event_id"]
    rca       = _parse_rca(rca_event["payload"])

    # ── Step 6 IN log ─────────────────────────────────────────────────────────
    _log_6_rca_received(rca)
    emit_step(
        rca.get("eventId", ""),
        "rca_received", "done",
        meta=(f"{rca['root_cause']} · {rca['domain']} · "
              f"confidence={rca['confidence_score']} · "
              f"risk={rca['risk_score']} · rev={rca['reversibility_score']}"),
        payload={"root_cause": rca["root_cause"], "domain": rca["domain"],
                 "confidence_score": rca["confidence_score"],
                 "risk_score": rca["risk_score"],
                 "reversibility_score": rca["reversibility_score"]},
    )

    # ── Step 7: Utility scoring + branch ranking ──────────────────────────────
    emit_step(rca.get("eventId", ""), "utility_scoring", "running",
        meta="Computing BIEM utility · ranking branches…")
    branches         = _build_branches(rca)
    top_utility      = branches[0]["utility_score"] if branches else 0.0
    utility_priority = _priority_from_utility(top_utility)

    # ── Step 7 scoring log ────────────────────────────────────────────────────
    _log_7_utility_scoring(rca, branches, top_utility, utility_priority)
    emit_step(
        rca.get("eventId", ""),
        "utility_scoring", "done",
        meta=(f"{len(branches)} {'branch' if len(branches)==1 else 'branches'} · "
              f"top={top_utility} · priority={utility_priority} · "
              f"seq1={branches[0].get('action','?') if branches else '?'}"),
        payload={"branch_count": len(branches), "top_utility": top_utility,
                 "utility_priority": utility_priority,
                 "execution_order": [
                     {"sequence": b.get("sequence"), "option": b.get("option",""),
                      "action": b.get("action"), "utility_score": b.get("utility_score")}
                     for b in branches
                 ]},
    )

    execution_order = [
        {
            "sequence":      b.get("sequence"),
            "option":        b.get("option", ""),
            "domain":        b.get("domain"),
            "action":        b.get("action"),
            "utility_score": b.get("utility_score"),
            "risk_score":    b.get("risk_score"),
        }
        for b in branches
    ]

    # ── Build TMF921 intent ───────────────────────────────────────────────────
    intent_id = _build_intent_id(rca)
    tmf921    = _build_tmf921_intent(rca, branches, utility_priority, intent_id)

    # ── Step 7 TMF921 log ─────────────────────────────────────────────────────
    _log_7_tmf921_built(rca, intent_id, utility_priority, len(tmf921["expressions"]))
    emit_step(
        rca.get("eventId", ""),
        "tmf921_built", "done",
        meta=(f"intent={intent_id} · priority={utility_priority} · "
              f"{len(tmf921['expressions'])} KPI "
              f"{'target' if len(tmf921['expressions'])==1 else 'targets'}"),
        payload={"intent_id": intent_id, "priority": utility_priority,
                 "expressions": len(tmf921["expressions"])},
    )

    # ── Build Executor payload ────────────────────────────────────────────────
    executor_payload = {
        "eventId":           rca["eventId"],
        "intent_id":         intent_id,
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "intent_type":       "TMF921_RCD_CONFIRMED_CAUSE",
        "root_cause":        rca["root_cause"],
        "affected_entities": rca["affected_entities"],
        "affected_hex_bins": rca["affected_hex_bins"],
        "utility_scoring": {
            "impact_score":      rca["impact_score"],
            "criticality_score": rca["criticality_score"],
            "criticality_label": rca["criticality_label"],
            "kpi_delta_pct":     rca["kpi_delta_pct"],
            "top_utility_score": top_utility,
            "utility_priority":  utility_priority,
        },
        "ranked_healing_plan": [
            {
                "sequence":            b["sequence"],
                "option":              b.get("option", ""),
                "domain":              b["domain"],
                "action":              b["action"],
                "action_detail":       b.get("action_detail", ""),
                "description":         b["description"],
                "target_entities":     b["target_entities"],
                "param":               b.get("param", ""),
                "value":               b.get("value"),
                "direction":           b.get("direction", ""),
                "causal_parameters":   b.get("causal_parameters", {}),
                "risk_score":          b["risk_score"],
                "reversibility_score": b["reversibility_score"],
                "utility_score":       b["utility_score"],
                "action_command":      b.get("action_command", {}),
            }
            for b in branches
        ],
        "tmf921_intent": tmf921,
    }

    # ── POST to ExecutorAgent ─────────────────────────────────────────────────
    if not EXECUTOR_AGENT_URL:
        logging.error("[Step 7 OUT] EXECUTOR_AGENT_URL is not configured")
        return {
            "status": "EXECUTOR_URL_MISSING",
            "error":  "EXECUTOR_AGENT_URL environment variable is required",
        }

    executor_url = f"{EXECUTOR_AGENT_URL}/execute-healing-plan"

    # ── Step 7 OUT log: calling Executor ──────────────────────────────────────
    _log_7_out_calling_executor(rca, executor_url, intent_id, branches)
    _log_7_out_executor_request(executor_payload)
    emit_step(
        rca.get("eventId", ""),
        "executor_called", "running",
        meta=(f"POST {executor_url} · "
              f"{len(branches)} {'branch' if len(branches)==1 else 'branches'}…"),
    )

    try:
        response = requests.post(executor_url, json=executor_payload, timeout=180)
        response.raise_for_status()
        executor_response = response.json()
    
        executor_success = bool(executor_response.get("success", False))
        executor_state = str(executor_response.get("state", "")).lower()
    
        if not executor_success or executor_state != "completed":
            _log_7_out_executor_response(rca, executor_response)
    
            emit_step(
                rca.get("eventId", ""),
                "executor_called",
                "error",
                meta=(
                    f"FAILED: intent={executor_response.get('intent_id', intent_id)} · "
                    f"state={executor_response.get('state', '')} · "
                    f"error={executor_response.get('error', '')}"
                ),
                payload={
                    "intent_id": executor_response.get("intent_id", intent_id),
                    "activation_id": executor_response.get("activation_id", ""),
                    "state": executor_response.get("state", ""),
                    "success": executor_response.get("success", False),
                    "error": executor_response.get("error", ""),
                },
            )
    
            state["executor_response"] = executor_response
            state[NETWORK_STATUS_KEY] = "FAILED"
    
            return {
                "status": "EXECUTOR_FAILED",
                "eventId": rca["eventId"],
                "intent_id": executor_response.get("intent_id", intent_id),
                "executor_state": executor_response.get("state", ""),
                "executor_success": executor_response.get("success", False),
                "error": executor_response.get("error", ""),
                "network_status": "FAILED",
            }
    
        # ── Step 7 OUT log: Executor response ─────────────────────────────────
        _log_7_out_executor_response(rca, executor_response)
    
        emit_step(
            rca.get("eventId", ""),
            "executor_called",
            "done",
            meta=(
                f"intent={executor_response.get('intent_id', intent_id)} · "
                f"activation={executor_response.get('activation_id','')} · "
                f"state={executor_response.get('state','?')}"
            ),
            payload={
                "intent_id": executor_response.get("intent_id", intent_id),
                "activation_id": executor_response.get("activation_id", ""),
                "state": executor_response.get("state", ""),
                "success": executor_response.get("success", False),
            },
        )
    
        state["executor_response"] = executor_response
    
    except Exception as e:
        logging.exception(
            f"[Step 7 OUT] ExecutorAgent call FAILED | "
            f"eventId={rca['eventId']} | url={executor_url} | error={str(e)}"
        )
        emit_step(
            rca.get("eventId", ""),
            "executor_called",
            "error",
            meta=f"FAILED: {str(e)}",
        )
        state[NETWORK_STATUS_KEY] = "FAILED"
        return {
            "status": "EXECUTOR_CALL_FAILED",
            "eventId": rca["eventId"],
            "error": str(e),
            "network_status": "FAILED",
        }

    # ── Publish event on internal ADK bus ─────────────────────────────────────
    engineer_output = {
        "eventId":           rca["eventId"],
        "source_event_id":   source_id,
        "intent_id":         intent_id,
        "intent_type":       "TMF921_RCD_CONFIRMED_CAUSE",
        "intent_target":     "ExecutorAgent",
        "tmf921_intent":     tmf921,
        "root_cause":        rca["root_cause"],
        "root_cause_mapped": rca["root_cause"],
        "domain":            rca["domain"],
        "priority":          utility_priority,
        "criticality":       rca["criticality_label"],
        "target_entities":   rca["affected_entities"],
        "affected_hex_bins": rca["affected_hex_bins"],
        "ranked_branches":   branches,
        "execution_order":   execution_order,
        "utility_scoring": {
            "impact_score":        rca["impact_score"],
            "criticality_score":   rca["criticality_score"],
            "criticality_label":   rca["criticality_label"],
            "risk_score":          rca["risk_score"],
            "reversibility_score": rca["reversibility_score"],
            "top_utility_score":   top_utility,
            "utility_priority":    utility_priority,
            "kpi_delta_pct":       rca["kpi_delta_pct"],
            "branch_count":        len(branches),
        },
        "recovery_targets":  rca["recovery_targets"],
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "confidence_score":  rca["confidence_score"],
        "evidence_chain":    rca["evidence_chain"],
        "affected_cells":    rca["affected_cells"],
    }

    event = make_engineer_event(source_event_id=source_id, engineer_output=engineer_output)
    publish_event(state, event)
    state["engineer_last_event_id"] = rca_event["event_id"]
    state["engineer_output"]        = engineer_output
    state[NETWORK_STATUS_KEY]       = "HEALING"

    # ── Step 7 OUT log: response summary ──────────────────────────────────────
    _log_7_out_response_summary(rca, intent_id, utility_priority, len(branches), execution_order)

    return {
        "status":            "EVENT_PUBLISHED",
        "published_event":   event["event_type"],
        "eventId":           rca["eventId"],
        "intent_id":         intent_id,
        "root_cause":        rca["root_cause"],
        "root_cause_mapped": rca["root_cause"],
        "domain":            rca["domain"],
        "priority":          utility_priority,
        "criticality":       rca["criticality_label"],
        "top_utility_score": top_utility,
        "branch_count":      len(branches),
        "execution_order":   execution_order,
        "target_entities":   rca["affected_entities"],
        "affected_hex_bins": rca["affected_hex_bins"],
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "next_agent":        "ExecutorAgent",
        "network_status":    "HEALING",
    }