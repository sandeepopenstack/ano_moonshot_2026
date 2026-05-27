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
        f"formula=impact({impact}) x criticality({criticality}) x (1-risk) x reversibility"
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
    rca:          dict,
    executor_url: str,
    intent_id:    str,
    n_branches:   int,
) -> None:
    """Step 7 OUT — Calling ExecutorAgent."""
    event_id = rca.get("eventId", "unknown")
    domain   = rca.get("domain", "unknown")
    rc       = rca.get("root_cause", "unknown")

    action_desc = {
        "antenna_tilt_misconfiguration": "roll back the antenna tilt parameter to its baseline",
        "physical_fiber_cut_backhaul":   "reroute backhaul traffic to the redundant AGG path",
        "hss_subscriber_db_saturation":  "clear stale HSS sessions and restore capacity",
        "hss_stale_session_loop":        "clear stale HSS sessions",
        "fiber_cut":                     "reroute backhaul to the redundant path",
        "transport_path_failure":        "failover to the backup transport path",
    }.get(rc.lower(), f"execute the {domain} remediation plan")

    logging.info(
        f"[Step 7 OUT] Handing the healing plan to the Executor for event '{event_id}'. "
        f"Asking it to {action_desc}. "
        f"Sending {n_branches} ranked {'branch' if n_branches == 1 else 'branches'} — "
        f"it should start with sequence 1 and escalate only if needed. "
        f"eventId={event_id} | url={executor_url} | "
        f"intent_id={intent_id} | branch_count={n_branches}"
    )


def _log_7_out_executor_request(executor_payload: dict) -> None:
    logging.info(
        f"[Step 7 OUT] Executor Request Payload | "
        f"{json.dumps(executor_payload, default=str)}"
    )


def _log_7_out_executor_response(rca: dict, executor_response: dict) -> None:
    """Step 7 OUT — Executor response received."""
    event_id   = rca.get("eventId", "unknown")
    intent_id  = executor_response.get("intent_id", "unknown")
    exec_state = executor_response.get("state", "unknown")

    logging.info(
        f"[Step 7 OUT] Executor accepted the healing plan for event '{event_id}'. "
        f"Intent '{intent_id}' is now '{exec_state}'. "
        f"The remediation is in progress — "
        f"I'll publish the engineer.healing.plan.ready event and stand by for the Reflection Agent. "
        f"eventId={event_id} | intent_id={intent_id} | state={exec_state}"
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
        "root_cause":             raw.get("root_cause", ""),
        "root_cause_description": raw.get("root_cause_description", ""),
        "timestamp_of_cause":     raw.get("timestamp_of_cause", ""),
        "domain":                 raw.get("domain", "UNKNOWN"),
        "confidence_label":       raw.get("confidence", ""),
        "confidence_score":       float(raw.get("confidence_score", 0.0)),
        "affected_entities":      raw.get("affected_entities", []),
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
      Option B: risk + 0.10 → lower utility → seq 2
      Option C (accept_degradation): risk + 0.30 penalty → always last

    Sequence assigned AFTER sort — pure utility math, no hardcoding.
    """
    impact_score       = rca["impact_score"]
    criticality_score  = rca["criticality_score"]
    base_risk          = rca["risk_score"]
    base_reversibility = rca["reversibility_score"]
    branches: list[dict] = []

    # ── Cross-domain: 1 branch per domain from Detective confirmedRcaBranches ──
    if rca["confirmed_rca_branches"]:
        for branch in rca["confirmed_rca_branches"]:
            b_risk       = float(branch.get("risk_score", base_risk))
            b_reversible = float(branch.get("reversibility", base_reversibility))
            utility      = _compute_utility(impact_score, criticality_score, b_risk, b_reversible)
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
                "impact_score":        impact_score,
                "criticality_score":   criticality_score,
                "utility_score":       utility,
                "utility_priority":    _priority_from_utility(utility),
                "action_source":       "detective_confirmed_rca_branches",
                "priority_score":      float(branch.get("priority_score", 10)),
            })

    # ── Single-domain: 1 branch per suggested_remediation option ───────────────
    elif rca["suggested_remediation"]:
        for idx, suggestion in enumerate(rca["suggested_remediation"]):
            option_risk = min(base_risk + (0.10 * idx), 1.0)
            is_noop     = suggestion.get("action") == "accept_degradation"
            if is_noop:
                option_risk = min(option_risk + 0.20, 1.0)
            utility = _compute_utility(impact_score, criticality_score, option_risk, base_reversibility)
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
                "param":               suggestion.get("param", ""),
                "value":               suggestion.get("value"),
                "direction":           suggestion.get("direction", ""),
                "causal_parameters":   rca["causal_parameters"],
                "is_noop":             is_noop,
                "risk_score":          option_risk,
                "reversibility_score": base_reversibility,
                "impact_score":        impact_score,
                "criticality_score":   criticality_score,
                "utility_score":       utility,
                "utility_priority":    _priority_from_utility(utility),
                "action_source":       "detective_suggested_remediation",
                "priority_score":      float(10 - idx),
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
    _log_7_out_calling_executor(rca, executor_url, intent_id, len(branches))
    _log_7_out_executor_request(executor_payload)
    emit_step(
        rca.get("eventId", ""),
        "executor_called", "running",
        meta=(f"POST {executor_url} · "
              f"{len(branches)} {'branch' if len(branches)==1 else 'branches'}…"),
    )

    try:
        response = requests.post(executor_url, json=executor_payload, timeout=60)
        response.raise_for_status()
        executor_response = response.json()

        # ── Step 7 OUT log: Executor response ─────────────────────────────────
        _log_7_out_executor_response(rca, executor_response)
        emit_step(
            rca.get("eventId", ""),
            "executor_called", "done",
            meta=(f"intent={executor_response.get('intent_id', intent_id)} · "
                  f"activation={executor_response.get('activation_id','')} · "
                  f"state={executor_response.get('state','?')}"),
            payload={"intent_id": executor_response.get("intent_id", intent_id),
                     "activation_id": executor_response.get("activation_id", ""),
                     "state": executor_response.get("state", "")},
        )

        state["executor_response"] = executor_response

    except Exception as e:
        logging.exception(
            f"[Step 7 OUT] ExecutorAgent call FAILED | "
            f"eventId={rca['eventId']} | url={executor_url} | error={str(e)}"
        )
        emit_step(rca.get("eventId", ""), "executor_called", "error",
            meta=f"FAILED: {str(e)}")
        return {"status": "EXECUTOR_CALL_FAILED", "error": str(e)}

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