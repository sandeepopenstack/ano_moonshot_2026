"""
app/agents/engineer_agent/tools.py
====================================
EngineerAgent — single tool: generate_healing_plan.

ARCHITECTURE OWNERSHIP (Final Target):
  DetectiveAgent owns EVERYTHING about the fault:
    - root_cause, root_cause_description, domain
    - causal_parameters (what changed, from/to values)
    - suggested_remediation (options A/B/C with action, target, param, value)
    - risk_score, reversibility_score  (from CHANGEREQUEST.csv via YAML)
    - impact_score, criticality_score, criticality_label  (from INSIGHT.csv via GNN)
    - recovery_targets  (from PERFORMANCE.csv nominal vs degraded)
    - kpi_impact, evidence_chain, affected_entities, alarm_ids, etc.

  EngineerAgent owns ONLY:
    - Utility scoring:  Utility = impact_score x criticality_score x (1-risk) x reversibility
    - Branch ranking:   sort by utility descending -> assign sequence 1, 2, 3...
    - TMF921 intent:    package ranked branches + recovery expressions
    - Executor call:    POST ranked_healing_plan + tmf921_intent to ExecutorAgent

  NO domain knowledge here. NO hardcoded actions. NO BigQuery. NO CSV reads.
  Everything comes from the Detective Agent payload. EngineerAgent is pure
  orchestration + math.

Slide Step 7:
  IN : investigation.rca.confirmed (Detective Agent output - Doc2 schema)
         -> root_cause, causal_parameters, suggested_remediation,
            risk_score, reversibility_score, impact_score, criticality_score,
            recovery_targets, affected_entities
  OUT: TMF921 RCD Confirmed Cause -> POSTed to ExecutorAgent /execute-healing-plan

Detective Agent suggested_remediation schema (Doc2):
  [
    {"option": "A", "action": "revert_antenna_tilt", "target": "eNB-SYN-003",
     "param": "antenna_tilt", "value": 85.3, "note": "Revert to pre-change state"},
    {"option": "B", "action": "adjust_neighbor_antenna_tilt", "target": "gNB-SYN-003",
     "param": "antenna_tilt", "direction": "compensate", "note": "Absorb overflow"},
    {"option": "C", "action": "accept_degradation", "target": "",
     "note": "Accept if change was intentional"}
  ]

Each option becomes one scored branch. Option A -> Sequence 1 (primary), etc.
accept_degradation receives a risk penalty -> always last in sequence.
"""

import uuid
import os
import json
import requests
import logging

logging.basicConfig(level=logging.INFO)

from datetime import datetime, timezone
from google.adk.tools import ToolContext

from app.events import (
    EVT_DETECTIVE_RCA_CONFIRMED,
    NETWORK_STATUS_KEY,
    consume_latest,
    make_engineer_event,
    publish_event,
)
from app.config.remediation_config import UTILITY_SCORING

EXECUTOR_AGENT_URL = os.environ.get("EXECUTOR_AGENT_URL")


# ── Utility scoring ────────────────────────────────────────────────────────────

def _compute_utility(
    impact_score:      float,
    criticality_score: float,
    risk_score:        float,
    reversibility:     float,
) -> float:
    """
    Slide 7 utility formula:
      Utility = impact_score x criticality_score x (1 - risk) x reversibility

    All four inputs come directly from the Detective Agent payload:
      impact_score       -> INSIGHT.csv z_impact/10, forwarded by Detective
      criticality_score  -> INSIGHT.csv criticality numeric, forwarded by Detective
      risk_score         -> CHANGEREQUEST.csv + YAML resolution.risk_score
      reversibility      -> CHANGEREQUEST.csv + YAML resolution.reversibility_score
    """
    return round(
        impact_score * criticality_score * (1.0 - risk_score) * reversibility,
        4,
    )


def _priority_from_utility(utility: float) -> str:
    """Map utility score -> human-readable priority label."""
    t = UTILITY_SCORING["priority_thresholds"]
    if utility >= t["CRITICAL"]: return "CRITICAL"
    if utility >= t["HIGH"]:     return "HIGH"
    if utility >= t["MEDIUM"]:   return "MEDIUM"
    return "LOW"


def _build_intent_id(rca: dict) -> str:
    """Derive intent lineage from Detective RCA identifiers."""
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
    Parse the Detective Agent RCA payload (investigation.rca.confirmed).
    Strictly follows Doc2 Detective Agent output schema.

    kpi_delta_pct resolution:
      1. raw['kpi_impact']['primary_metric_delta_pct']  - Doc2 canonical location
      2. raw['kpi_delta_pct']                           - root-level shortcut
      3. UTILITY_SCORING['default_kpi_delta_pct']       - fallback (30.0)

    confirmedRcaBranches: Detective Agent returns camelCase.
    Fallback to snake_case for forward compatibility.
    """
    kpi_impact = raw.get("kpi_impact", {})
    raw_delta  = (
        kpi_impact.get("primary_metric_delta_pct")
        or raw.get("kpi_delta_pct")
        or UTILITY_SCORING.get("default_kpi_delta_pct", 30.0)
    )

    return {
        # Identity
        "eventId":                raw.get("eventId", ""),
        "hypothesis_id":          raw.get("hypothesis_id", ""),
        "change_request_id":      raw.get("change_request_id", ""),
        "incident_type":          raw.get("incident_type", ""),

        # Root cause - Detective Agent owns this, passed through unchanged
        "root_cause":             raw.get("root_cause", ""),
        "root_cause_description": raw.get("root_cause_description", ""),
        "timestamp_of_cause":     raw.get("timestamp_of_cause", ""),
        "domain":                 raw.get("domain", "UNKNOWN"),

        # Confidence
        "confidence_label":       raw.get("confidence", ""),
        "confidence_score":       float(raw.get("confidence_score", 0.0)),

        # Entities
        "affected_entities":      raw.get("affected_entities", []),
        "neighbor_entities":      raw.get("neighbor_entities", []),
        "affected_cells":         raw.get("affected_cells", []),
        "affected_hex_bins":      raw.get("affected_hex_bins", []),
        "alarm_ids":              raw.get("alarm_ids", []),
        "primary_resource":       raw.get("primary_resource", {}),

        # Remediation intelligence - Detective Agent owns all of this
        "causal_parameters":      raw.get("causal_parameters", {}),
        "suggested_remediation":  raw.get("suggested_remediation", []),
        "recovery_targets":       raw.get("recovery_targets", []),
        "kpi_impact":             kpi_impact,
        "kpi_delta_pct":          abs(float(raw_delta)),
        "evidence_chain":         raw.get("evidence_chain", []),

        # Cross-domain branches (Detective Agent populates when domain=CROSS_DOMAIN)
        "confirmed_rca_branches": raw.get(
            "confirmedRcaBranches",
            raw.get("confirmed_rca_branches", [])
        ),

        # Scores - all sourced from Detective Agent
        # risk_score + reversibility_score: CHANGEREQUEST.csv + YAML resolution.*
        #   UC1 RAN:       0.4 / 0.95
        #   UC2 CORE:      0.6 / 0.70
        #   UC3 TRANSPORT: 0.5 / 0.90
        "risk_score":             float(raw.get("risk_score", 0.5)),
        "reversibility_score":    float(raw.get("reversibility_score", 0.8)),

        # impact_score + criticality_score: INSIGHT.csv via GNN, forwarded by Detective
        "impact_score":           float(raw.get("impact_score", 0.94)),
        "criticality_score":      float(raw.get("criticality_score", 1.0)),
        "criticality_label":      raw.get("criticality_label", "CRITICAL"),

        # YAML resolution.ttr_minutes (UC1=130, UC2=80, UC3=110) via Detective Agent
        "estimated_ttr_minutes":  int(raw.get("estimated_ttr_minutes", 0)),

        # Severity / business priority
        "severity":               raw.get("severity", "P1"),
        "business_priority":      raw.get("businessPriority", "CRITICAL"),
        "change_type_name":       raw.get("change_type_name", "UNKNOWN"),
    }


# ── Build utility-scored branches ──────────────────────────────────────────────

def _build_branches(rca: dict) -> list[dict]:
    """
    Build one branch per suggested_remediation option from Detective Agent.
    Score each with the utility formula. Sort descending. Assign sequence.

    Each suggested_remediation option (Doc2 schema):
      {option, action, target, param, value, direction, note}

    Utility differentiation across options:
      Option A (idx=0): base risk_score         -> highest utility -> Sequence 1
      Option B (idx=1): risk_score + 0.10       -> lower  utility -> Sequence 2
      Option C (idx=2): risk_score + 0.20       -> lowest utility -> Sequence 3

    accept_degradation: additional +0.20 risk penalty on top of index penalty
      -> always ends up last regardless of option letter.

    Cross-domain (confirmedRcaBranches populated by Detective Agent):
      Each branch carries its own domain-specific risk_score + reversibility.
      Utility computed independently per branch -> meaningful cross-domain ranking.

    Fallback (no suggested_remediation, no confirmedRcaBranches):
      Single branch wrapping the RCA payload - Executor decides action.
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
            # Risk escalates per option index (primary -> fallback -> no-op)
            option_risk = min(base_risk + (0.10 * idx), 1.0)

            # accept_degradation: additional penalty -> always last
            is_noop = suggestion.get("action") == "accept_degradation"
            if is_noop:
                option_risk = min(option_risk + 0.20, 1.0)

            utility = _compute_utility(impact_score, criticality_score, option_risk, base_reversibility)

            # target: suggestion's target if provided, else all affected entities
            target_entities = (
                [suggestion["target"]]
                if suggestion.get("target")
                else rca["affected_entities"]
            )

            branches.append({
                "domain":              rca["domain"],
                "root_cause":          rca["root_cause"],
                "option":              suggestion.get("option", chr(65 + idx)),
                # Detective Agent action fields - passed through unchanged
                "action":              suggestion.get("action", ""),
                "action_detail":       "",
                "description":         suggestion.get("note", ""),
                "target_entities":     target_entities,
                "param":               suggestion.get("param", ""),
                "value":               suggestion.get("value"),
                "direction":           suggestion.get("direction", ""),
                "causal_parameters":   rca["causal_parameters"],
                "is_noop":             is_noop,
                # Utility scoring - EngineerAgent computes this
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

    # Sort descending by utility -> assign sequence AFTER sort (Slide 7)
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
    Build TMF921-aligned intent expressing the desired network outcome.
    Follows the simplified TMF921 structure from Doc2 (tmf921_intent block).

    Sequence 1 branch = highest utility = primary action for ExecutorAgent.
    recovery_targets -> expressions (from PERFORMANCE.csv via Detective Agent).
    All values sourced from rca (Detective Agent) or branches (utility scoring).
    No domain-specific logic here.
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

    # activation_id derived from change_request_id (same pattern as Doc2)
    activation_id = (
        f"ACT-SYN-{rca['change_request_id'].split('-')[-1]}"
        if rca["change_request_id"]
        else f"ACT-{str(uuid.uuid4())[:8].upper()}"
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
            # estimated_ttr_minutes from YAML resolution.ttr_minutes via Detective Agent
            "estimated_ttr_minutes": rca["estimated_ttr_minutes"] or None,
            "reversible_required":   True,
            "maintenance_window":    "immediate",
        },
        "change_request_id": rca["change_request_id"],
        "hypothesis_id":     rca["hypothesis_id"],
        "activation_id":     activation_id,
        "evidence_chain":    rca["evidence_chain"],
        "confidence_score":  rca["confidence_score"],
        "created_at":        datetime.now(timezone.utc).isoformat(),
        "created_by":        "EngineerAgent",
    }


# ── Main tool ──────────────────────────────────────────────────────────────────

def generate_healing_plan(tool_context: ToolContext) -> dict:
    """
    Single tool - NO arguments.

    Slide Step 7:
      IN : investigation.rca.confirmed (Detective Agent)
      OUT: TMF921 RCD Confirmed Cause -> POST to ExecutorAgent /execute-healing-plan

    Idempotent: skips if already processed this RCA event_id.
    """
    state = tool_context.state

    # ── Consume Detective Agent event ────────────────────────────────────────
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

    # ── Step 6 IN: Detective Agent -> EngineerAgent ──────────────────────────
    logging.info(
        f"[Step 6] DetectiveAgent -> EngineerAgent | "
        f"eventId={rca['eventId']} | "
        f"root_cause={rca['root_cause']} | "
        f"domain={rca['domain']} | "
        f"confidence={rca['confidence_score']} | "
        f"risk={rca['risk_score']} | "
        f"reversibility={rca['reversibility_score']} | "
        f"impact_score={rca['impact_score']} | "
        f"criticality_score={rca['criticality_score']} | "
        f"remediation_options={len(rca['suggested_remediation'])}"
    )

    rca_log = {
        "eventId":                rca["eventId"],
        "hypothesis_id":          rca["hypothesis_id"],
        "root_cause":             rca["root_cause"],
        "root_cause_description": rca["root_cause_description"],
        "timestamp_of_cause":     rca["timestamp_of_cause"],
        "domain":                 rca["domain"],
        "confidence":             rca["confidence_label"],
        "confidence_score":       rca["confidence_score"],
        "incident_type":          rca["incident_type"],
        "change_request_id":      rca["change_request_id"],
        "primary_resource":       rca["primary_resource"],
        "affected_entities":      rca["affected_entities"],
        "neighbor_entities":      rca["neighbor_entities"],
        "affected_cells":         rca["affected_cells"],
        "affected_hex_bins":      rca["affected_hex_bins"],
        "alarm_ids":              rca["alarm_ids"],
        "causal_parameters":      rca["causal_parameters"],
        "suggested_remediation":  rca["suggested_remediation"],
        "confirmed_rca_branches": rca["confirmed_rca_branches"],
        "recovery_targets":       rca["recovery_targets"],
        "kpi_impact":             rca["kpi_impact"],
        "kpi_delta_pct":          rca["kpi_delta_pct"],
        "risk_score":             rca["risk_score"],
        "reversibility_score":    rca["reversibility_score"],
        "impact_score":           rca["impact_score"],
        "criticality_score":      rca["criticality_score"],
        "criticality_label":      rca["criticality_label"],
        "estimated_ttr_minutes":  rca["estimated_ttr_minutes"],
        "evidence_chain":         rca["evidence_chain"],
    }
    logging.info(
        f"[Step 6] Detective RCA Payload | "
        f"{json.dumps(rca_log, default=str)}"
    )

    # ── Step 7: Utility scoring + branch ranking ──────────────────────────────
    branches         = _build_branches(rca)
    top_utility      = branches[0]["utility_score"] if branches else 0.0
    utility_priority = _priority_from_utility(top_utility)

    logging.info(
        f"[Step 7] EngineerAgent Utility Scoring | "
        f"eventId={rca['eventId']} | "
        f"branches={len(branches)} | "
        f"top_utility={top_utility} | "
        f"utility_priority={utility_priority} | "
        f"formula=impact({rca['impact_score']}) x criticality({rca['criticality_score']}) "
        f"x (1-risk) x reversibility"
    )

    execution_order = [
        {
            "sequence":        b["sequence"],
            "option":          b.get("option", ""),
            "domain":          b["domain"],
            "action":          b["action"],
            "target_entities": b["target_entities"],
            "utility_score":   b["utility_score"],
            "risk_score":      b["risk_score"],
        }
        for b in branches
    ]

    logging.info(
        f"[Step 7] Ranked Healing Branches | "
        f"{json.dumps(execution_order, default=str)}"
    )

    # ── Build TMF921 intent ──────────────────────────────────────────────────
    intent_id = _build_intent_id(rca)
    tmf921    = _build_tmf921_intent(rca, branches, utility_priority, intent_id)

    logging.info(
        f"[Step 7] TMF921 Intent Built | "
        f"eventId={rca['eventId']} | "
        f"intent_id={intent_id} | "
        f"activation_id={tmf921['activation_id']} | "
        f"priority={utility_priority} | "
        f"criticality={rca['criticality_label']} | "
        f"expressions={len(tmf921['expressions'])}"
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
        # Utility scoring summary for Executor audit trail
        "utility_scoring": {
            "impact_score":      rca["impact_score"],
            "criticality_score": rca["criticality_score"],
            "criticality_label": rca["criticality_label"],
            "kpi_delta_pct":     rca["kpi_delta_pct"],
            "top_utility_score": top_utility,
            "utility_priority":  utility_priority,
        },
        # Ranked healing plan - sequence 1 = Executor runs first
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
        # TMF921 intent (Doc2-aligned structure)
        "tmf921_intent": tmf921,
    }

    # ── POST to ExecutorAgent ────────────────────────────────────────────────
    if not EXECUTOR_AGENT_URL:
        logging.error("[Step 7 OUT] EXECUTOR_AGENT_URL is not configured")
        return {
            "status": "EXECUTOR_URL_MISSING",
            "error": "EXECUTOR_AGENT_URL environment variable is required",
        }

    executor_url = f"{EXECUTOR_AGENT_URL}/execute-healing-plan"

    logging.info(
        f"[Step 7 OUT] EngineerAgent -> ExecutorAgent | "
        f"eventId={rca['eventId']} | "
        f"url={executor_url} | "
        f"intent_id={intent_id} | "
        f"branch_count={len(branches)}"
    )

    logging.info(
        f"[Step 7 OUT] Executor Request Payload | "
        f"{json.dumps(executor_payload, default=str)}"
    )

    try:
        response = requests.post(
            executor_url,
            json=executor_payload,
            timeout=60,
        )
        response.raise_for_status()
        executor_response = response.json()

        logging.info(
            f"[Step 7 OUT] ExecutorAgent SUCCESS | "
            f"eventId={rca['eventId']} | "
            f"activation_id={executor_response.get('activation_id')} | "
            f"intent_id={executor_response.get('intent_id')} | "
            f"state={executor_response.get('state')}"
        )

        logging.info(
            f"[Step 7 OUT] Executor Response Payload | "
            f"{json.dumps(executor_response, default=str)}"
        )

        state["executor_response"] = executor_response

    except Exception as e:
        logging.exception(
            f"[Step 7 OUT] ExecutorAgent call FAILED | "
            f"eventId={rca['eventId']} | "
            f"url={executor_url} | "
            f"error={str(e)}"
        )
        return {"status": "EXECUTOR_CALL_FAILED", "error": str(e)}

    # ── Publish event on internal ADK bus ────────────────────────────────────
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

    engineer_response_log = {
        "status":           "EVENT_PUBLISHED",
        "eventId":          rca["eventId"],
        "intent_id":        intent_id,
        "priority":         utility_priority,
        "root_cause":       rca["root_cause"],
        "domain":           rca["domain"],
        "branch_count":     len(branches),
        "execution_order":  execution_order,
        "network_status":   "HEALING",
        "recovery_targets": [
            r["target_metric"] for r in rca["recovery_targets"]
        ],
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }
    logging.info(
        f"[Step 7 OUT] Engineer Response Payload | "
        f"{json.dumps(engineer_response_log, default=str)}"
    )

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
