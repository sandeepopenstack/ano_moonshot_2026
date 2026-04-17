"""
Investigation Agent Mock Output — Stage 6 (PPT)
================================================
The Investigation Agent is built by another team (Ericsson per PPT diagram).
This module simulates its confirmed RCA output so SolutionPlanningAgent
can be developed and tested independently.

PPT Stage 6 contract:
  In:  Domain triage + priority flag  (from MonitoringAgent)
  Out: Structured Root Cause Hypothesis
       UC1: Bad Antenna Tilt Push
       UC2: HSS Saturation

The real agent will:
  • Pull PM counters, FM alarms, CM config from BigQuery
  • Run RCD pipeline + cross-reference knowledge base
  • Send structured RCA to SolutionPlanningAgent via A2A API
"""

from datetime import datetime, timezone
import uuid


def generate_investigation_output(
    scenario: str = "UC_MULTI_DOMAIN_RCA",
) -> dict:
    """Stage 6 → Stage 7 confirmed RCA output."""

    event_id = str(uuid.uuid4())

    if scenario == "UC_MULTI_DOMAIN_RCA":
        return {
            "eventId":   event_id,
            "eventType": "investigation.rootCause.confirmed",
            "eventTime": datetime.now(timezone.utc).isoformat(),

            # Legacy single-cause block (backward compatibility)
            "rootCauseAnalysis": {
                "confirmedRootCause": "MULTI_DOMAIN_SERVICE_DEGRADATION",
                "domain":             "CROSS_DOMAIN",
                "severity":           "P1",
                "affectedResources":  ["RAN_CELL_101", "HSS_CORE_01", "TRANSPORT_LINK_01"],
                "confidence":         0.98,
            },

            # PRIMARY: multi-domain RCA branches — SolutionPlanningAgent must use this
            "confirmedRcaBranches": [
                {"action_id": "A", "domain": "RAN",       "root_cause": "BAD_ANTENNA_TILT_PUSH",   "priority_score": 10},
                {"action_id": "B", "domain": "CORE",      "root_cause": "HSS_STALE_SESSION_LOOP",  "priority_score": 7},
                {"action_id": "C", "domain": "TRANSPORT",  "root_cause": "PATH_DEGRADATION",        "priority_score": 4},
            ],

            "recommendedHealingScope": "MULTI_DOMAIN_PRIORITY_SEQUENCE",
            "businessPriority":        "CRITICAL",
        }

    # UC1 single-domain fallback
    return {
        "eventId":   event_id,
        "eventType": "investigation.rootCause.confirmed",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "rootCauseAnalysis": {
            "confirmedRootCause": "BAD_ANTENNA_TILT_PUSH",
            "domain":             "RAN",
            "severity":           "P1",
            "affectedResources":  ["RAN_CELL_101"],
            "confidence":         0.97,
        },
        "confirmedRcaBranches": [
            {"action_id": "A", "domain": "RAN", "root_cause": "BAD_ANTENNA_TILT_PUSH", "priority_score": 10},
        ],
        "recommendedHealingScope": "SINGLE_DOMAIN",
        "businessPriority":        "HIGH",
    }


def publish_investigation_output(payload: dict) -> dict:
    """Stage 6 → Stage 7 handoff envelope."""
    return {
        "status":       "published",
        "target_agent": "SolutionPlanningAgent",
        "payload":      payload,
    }