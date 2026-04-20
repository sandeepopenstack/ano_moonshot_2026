"""
events.py  —  Domain Event Contracts
======================================
Every agent publishes ONE event when it finishes.
The next agent subscribes to that event type.

Flow:
  GNN             publishes  →  GnnAnomalyDetectedEvent
  MonitoringAgent subscribes →  publishes MonitoringTriageReadyEvent
  InvestigationAgent(mock)   →  publishes InvestigationRcaConfirmedEvent
  SolutionPlanningAgent      →  publishes SolutionPlanReadyEvent
  ExecutionAgent(mock)       →  publishes ExecutionCompletedEvent
  ValidationAgent            →  publishes ValidationResultEvent
                                  ├─ status=RESOLVED  →  done
                                  └─ status=RETRIGGER →  re-publishes GnnAnomalyDetectedEvent

All events stored in ADK session state under:
  state["event_bus"]  →  list of event dicts in arrival order
  state["latest_<event_type>"]  →  latest instance for quick read
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
import uuid


# ── Base ─────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _uid() -> str:
    return str(uuid.uuid4())


# ── Event type constants (used as state keys + routing) ──────────────────────

EVT_GNN_ANOMALY_DETECTED         = "gnn.anomaly.detected"          # Stage 4
EVT_MONITORING_TRIAGE_READY      = "monitoring.triage.ready"        # Stage 5 out
EVT_INVESTIGATION_RCA_CONFIRMED  = "investigation.rca.confirmed"    # Stage 6 out
EVT_SOLUTION_PLAN_READY          = "solution.plan.ready"            # Stage 7 out
EVT_EXECUTION_COMPLETED          = "execution.completed"            # Stage 9 out
EVT_VALIDATION_RESULT            = "validation.result"              # Stage 10 out


# ── State key helpers ─────────────────────────────────────────────────────────

def latest_key(event_type: str) -> str:
    """state key for the latest event of a given type"""
    return f"latest_{event_type.replace('.', '_')}"

NETWORK_STATUS_KEY = "network_status"   # "HEALTHY" | "ANOMALY_DETECTED" | "HEALING" | "RESOLVED"
EVENT_BUS_KEY      = "event_bus"        # ordered log of all events


# ── Event factory functions ───────────────────────────────────────────────────

def make_gnn_anomaly_event(gnn_inference: dict) -> dict:
    """Stage 4: GNN inference engine fires this when it detects an anomaly."""
    return {
        "event_id":        _uid(),
        "event_type":      EVT_GNN_ANOMALY_DETECTED,
        "event_time":      _now(),
        "source":          "GNN_CORRELATION_ENGINE",
        "network_status":  "ANOMALY_DETECTED",
        "payload":         gnn_inference,
    }


def make_monitoring_triage_event(
    source_event_id: str,
    domain_triage: str,
    priority_flag: str,
    subgraph: dict,
    confidence: float,
    business_priority: str,
    ranked_branches: list,
    execution_order: list,
) -> dict:
    """Stage 5 out: MonitoringAgent fires this after domain triage."""
    return {
        "event_id":          _uid(),
        "event_type":        EVT_MONITORING_TRIAGE_READY,
        "event_time":        _now(),
        "source":            "MonitoringAgent",
        "source_event_id":   source_event_id,
        "network_status":    "HEALING",
        "payload": {
            "domain_triage":               domain_triage,
            "priority_flag":               priority_flag,
            "subgraph":                    subgraph,
            "confidence":                  confidence,
            "business_priority":           business_priority,
            "ranked_remediation_branches": ranked_branches,
            "execution_order":             execution_order,
        },
    }


def make_rca_confirmed_event(
    source_event_id: str,
    rca_output: dict,
) -> dict:
    """Stage 6 out: InvestigationAgent fires this with confirmed RCA."""
    return {
        "event_id":        _uid(),
        "event_type":      EVT_INVESTIGATION_RCA_CONFIRMED,
        "event_time":      _now(),
        "source":          "InvestigationAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         rca_output,
    }


def make_solution_plan_event(
    source_event_id: str,
    plan_output: dict,
) -> dict:
    """Stage 7 out: SolutionPlanningAgent fires this with TMF921 intent."""
    return {
        "event_id":        _uid(),
        "event_type":      EVT_SOLUTION_PLAN_READY,
        "event_time":      _now(),
        "source":          "SolutionPlanningAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         plan_output,
    }


def make_execution_completed_event(
    source_event_id: str,
    execution_output: dict,
) -> dict:
    """Stage 9 out: ExecutionAgent fires this after TMF641 sub-orders complete."""
    return {
        "event_id":        _uid(),
        "event_type":      EVT_EXECUTION_COMPLETED,
        "event_time":      _now(),
        "source":          "ExecutionAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         execution_output,
    }


def make_validation_result_event(
    source_event_id: str,
    resolved: bool,
    validation_output: dict,
) -> dict:
    """Stage 10 out: ValidationAgent fires this with final verdict."""
    return {
        "event_id":        _uid(),
        "event_type":      EVT_VALIDATION_RESULT,
        "event_time":      _now(),
        "source":          "ValidationAgent",
        "source_event_id": source_event_id,
        "network_status":  "RESOLVED" if resolved else "ANOMALY_DETECTED",
        "resolved":        resolved,
        "payload":         validation_output,
    }


# ── Event bus helpers (operate on tool_context.state) ────────────────────────

def publish_event(state: dict, event: dict) -> None:
    """
    Write event to the in-session event bus.
    Called from every agent tool via:
        publish_event(tool_context.state, my_event)
    """
    if EVENT_BUS_KEY not in state:
        state[EVENT_BUS_KEY] = []

    state[EVENT_BUS_KEY].append(event)
    state[latest_key(event["event_type"])] = event
    state[NETWORK_STATUS_KEY] = event.get("network_status", state.get(NETWORK_STATUS_KEY))


def consume_latest(state: dict, event_type: str) -> dict | None:
    """
    Read the latest event of a given type from session state.
    Called by the subscribing agent's tool:
        gnn_event = consume_latest(tool_context.state, EVT_GNN_ANOMALY_DETECTED)
    """
    return state.get(latest_key(event_type))
