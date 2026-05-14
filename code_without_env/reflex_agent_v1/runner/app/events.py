"""
app/events.py — Domain Event Contracts

Every agent publishes ONE event when it finishes.
The next agent subscribes to that event type.

Flow:
EventTriggerMS publishes → failure.notification
ReflexAgent/GNN publishes → gnn.anomaly.detected
  ReflexAgent          publishes → reflex.triage.ready
  DetectiveAgent (ext)       → detective.rca.confirmed
  EngineerAgent        publishes → engineer.ready
  ExecutorAgent (ext)           → execution.completed
  ReflectionAgent      publishes → reflection.result
                                     ├─ resolved=True  → pipeline done
                                     └─ resolved=False → retrigger via failure.notification

RULE: This file must NEVER import from agent modules, app.orchestrator,
      app.config, or any other app.* module. It is the lowest-level
      shared module — everything else imports from it.
"""

from datetime import datetime, timezone
from typing import Any
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _uid() -> str:
    return str(uuid.uuid4())


# ── Event type constants ───────────────────────────────────────────────────────
EVT_GNN_ANOMALY_DETECTED        = "gnn.anomaly.detected"
EVT_FAILURE_NOTIFICATION        = "failure.notification"
EVT_REFLEX_TRIAGE_READY         = "reflex.triage.ready"
EVT_DETECTIVE_RCA_CONFIRMED = "detective.rca.confirmed"
EVT_ENGINEER_READY              = "engineer.ready"
EVT_EXECUTION_COMPLETED         = "execution.completed"
EVT_REFLECTION_RESULT           = "reflection.result"

# Backward-compat aliases
EVT_MONITORING_TRIAGE_READY  = EVT_REFLEX_TRIAGE_READY
EVT_SOLUTION_PLAN_READY      = EVT_ENGINEER_READY
EVT_VALIDATION_RESULT        = EVT_REFLECTION_RESULT


# ── State key helpers ──────────────────────────────────────────────────────────
def latest_key(event_type: str) -> str:
    """Convert event type string to session state key."""
    return f"latest_{event_type.replace('.', '_')}"

NETWORK_STATUS_KEY = "network_status"
EVENT_BUS_KEY      = "event_bus"


# ── Event factory functions ────────────────────────────────────────────────────
def make_failure_notification_event(trigger_payload: dict) -> dict:
    return {
        "event_id":       _uid(),
        "event_type":     EVT_FAILURE_NOTIFICATION,
        "event_time":     _now(),
        "source":         "FailureInjectionMS",
        "network_status": "ANOMALY_DETECTED",
        "payload":        trigger_payload,
    }

def make_reflex_triage_event(source_event_id: str, payload: dict) -> dict:
    return {
        "event_id":        _uid(),
        "event_type":      EVT_REFLEX_TRIAGE_READY,
        "event_time":      _now(),
        "source":          "ReflexAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         payload,
    }


def make_rca_confirmed_event(source_event_id: str, rca_output: dict) -> dict:
    return {
        "event_id":        _uid(),
        "event_type":      EVT_DETECTIVE_RCA_CONFIRMED,
        "event_time":      _now(),
        "source":          "DetectiveAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         rca_output,
    }


def make_engineer_event(source_event_id: str, engineer_output: dict) -> dict:
    return {
        "event_id":        _uid(),
        "event_type":      EVT_ENGINEER_READY,
        "event_time":      _now(),
        "source":          "EngineerAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         engineer_output,
    }

# Alias
make_engineer_ready_event = make_engineer_event


def make_execution_completed_event(source_event_id: str, execution_output: dict) -> dict:
    return {
        "event_id":        _uid(),
        "event_type":      EVT_EXECUTION_COMPLETED,
        "event_time":      _now(),
        "source":          "ExecutorAgent",
        "source_event_id": source_event_id,
        "network_status":  "HEALING",
        "payload":         execution_output,
    }


def make_reflection_result_event(
    source_event_id: str,
    resolved: bool,
    reflection_output: dict,
) -> dict:
    return {
        "event_id":        _uid(),
        "event_type":      EVT_REFLECTION_RESULT,
        "event_time":      _now(),
        "source":          "ReflectionAgent",
        "source_event_id": source_event_id,
        "network_status":  "RESOLVED" if resolved else "ANOMALY_DETECTED",
        "resolved":        resolved,
        "payload":         reflection_output,
    }

# Alias
make_validation_result_event = make_reflection_result_event


# ── Event bus helpers ──────────────────────────────────────────────────────────

def publish_event(state: dict, event: dict) -> None:
    """Append event to bus, update latest_* key, update network_status."""
    if EVENT_BUS_KEY not in state:
        state[EVENT_BUS_KEY] = []
    state[EVENT_BUS_KEY].append(event)
    state[latest_key(event["event_type"])] = event
    state[NETWORK_STATUS_KEY] = event.get("network_status", state.get(NETWORK_STATUS_KEY))


def consume_latest(state: dict, event_type: str) -> dict | None:
    """Read the latest event of a given type (non-destructive)."""
    return state.get(latest_key(event_type))
