"""
app/workflow_state.py
======================
Session state schema documentation and final summary extractor.

Session state keys written by each agent:
  ReflexAgent     → latest_reflex_triage_ready, reflex_output, reflex_last_event_id
  EngineerAgent   → latest_engineer_ready, engineer_output, engineer_last_event_id
  ReflectionAgent → latest_reflection_result, reflection_output, reflection_last_event_id
  (all agents)    → network_status, event_bus

extract_final_summary() is called by main.py at the end of the pipeline loop
to produce the human-readable summary printed to stdout.

This module is service-local and imports from app.events inside runner.
"""

from __future__ import annotations
from typing import Any

from app.events import (
    EVT_REFLEX_TRIAGE_READY,
    EVT_DETECTIVE_RCA_CONFIRMED,
    EVT_ENGINEER_READY,
    EVT_EXECUTION_COMPLETED,
    EVT_REFLECTION_RESULT,
    NETWORK_STATUS_KEY,
    EVENT_BUS_KEY,
    consume_latest,
)


def extract_final_summary(state: dict[str, Any]) -> dict[str, Any]:
    """
    Build a structured summary from session state after the pipeline completes.
    Reads the latest event payload for each agent stage.
    All field names match exactly what each agent tool returns in its payload.
    """

    reflex_payload     = (consume_latest(state, EVT_REFLEX_TRIAGE_READY)         or {}).get("payload", {})
    rca_payload        = (consume_latest(state, EVT_DETECTIVE_RCA_CONFIRMED)  or {}).get("payload", {})
    engineer_payload   = (consume_latest(state, EVT_ENGINEER_READY)               or {}).get("payload", {})
    execution_payload  = (consume_latest(state, EVT_EXECUTION_COMPLETED)          or {}).get("payload", {})
    reflection_payload = (consume_latest(state, EVT_REFLECTION_RESULT)            or {}).get("payload", {})

    event_bus = state.get(EVENT_BUS_KEY, [])

    return {
        "pipeline":       "RAN_SELF_HEALING",
        "network_status": state.get(NETWORK_STATUS_KEY),
        "event_count":    len(event_bus),
        "event_sequence": [e["event_type"] for e in event_bus],

        # ReflexAgent — perform_triage return fields
        "reflex": {
            "domain_triage":     reflex_payload.get("domain_triage"),
            "priority_flag":     reflex_payload.get("priority_flag"),
            "priority_external": reflex_payload.get("priority_external"),
            "composite_score":   reflex_payload.get("composite_score"),
            "scoring_factors":   reflex_payload.get("scoring_factors"),
            "business_priority": reflex_payload.get("business_priority"),
            "entity_ids":        reflex_payload.get("entity_ids"),
            "gnn_branch_order":  reflex_payload.get("execution_order"),
        },

        # DetectiveAgent (external — Ericsson)
        "investigation": {
            "root_cause":        rca_payload.get("root_cause"),
            "domain":            rca_payload.get("domain"),
            "confidence_score":  rca_payload.get("confidence_score"),
            "hypothesis_id":     rca_payload.get("hypothesis_id"),
            "change_request_id": rca_payload.get("change_request_id"),
            "affected_entities": rca_payload.get("affected_entities", []),
            "rca_branch_count":  len(rca_payload.get("confirmedRcaBranches") or []),
        },

        # EngineerAgent — generate_healing_plan return fields
        "engineer": {
            "intent_type":            engineer_payload.get("intent_type"),
            "priority":               engineer_payload.get("priority"),
            "domain":                 engineer_payload.get("domain"),
            "root_cause":             engineer_payload.get("root_cause"),
            "root_cause_mapped":      engineer_payload.get("root_cause_mapped"),
            "execution_order":        engineer_payload.get("execution_order"),
            "branch_count":           len(engineer_payload.get("healing_branches") or []),
            "top_utility_score":      (engineer_payload.get("utility_scoring") or {}).get("top_utility_score"),
            "utility_scoring_active": (engineer_payload.get("utility_scoring") or {}).get("utility_scoring_active"),
            "change_request_id":      engineer_payload.get("change_request_id"),
            "hypothesis_id":          engineer_payload.get("hypothesis_id"),
            "confidence_score":       engineer_payload.get("confidence_score"),
        },

        # ExecutorAgent (external — Ericsson, doc 18 schema)
        "execution": {
            "success":       execution_payload.get("success"),
            "state":         execution_payload.get("state"),
            "error":         execution_payload.get("error"),
            "activation_id": execution_payload.get("activation_id"),
            "intent_id":     execution_payload.get("intent_id"),
        },

        # ReflectionAgent — evaluate_resolution return fields
        "reflection": {
            "status":            reflection_payload.get("status"),
            "resolved":          reflection_payload.get("resolved"),
            "execution_ok":      reflection_payload.get("execution_ok"),
            "zscore_comparison": reflection_payload.get("zscore_comparison"),
            "gui_status":        reflection_payload.get("gui_status"),
            "gnn_topology_view": reflection_payload.get("gnn_topology_view"),
            "business_view":     reflection_payload.get("business_view"),
            "service_view":      reflection_payload.get("service_view"),
            "topology_state":    reflection_payload.get("topology_state"),
            "retrigger_reason":  reflection_payload.get("retrigger_reason"),
        },

        "resolved": state.get(NETWORK_STATUS_KEY) == "RESOLVED",
    }
