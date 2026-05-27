"""
app/agents/reflection_agent/tools.py
======================================
ReflectionAgent — 2 tools in sequence.

Validation — Gate 1 only (active):
  Gate 1: execution_ok = success AND state == "completed"

  Gate 2 (anomaly_label) and Gate 3 (is_degraded) are commented out.
  Z-score comparison removed — not required while Gate 1 is the only check.
"""

import json
import os
import logging
import requests

logging.basicConfig(level=logging.INFO)

from datetime import datetime, timezone
from google.adk.tools import ToolContext

from ran_healing_shared.events import (
    EVT_EXECUTION_COMPLETED,
    EVT_FAILURE_NOTIFICATION,
    NETWORK_STATUS_KEY,
    consume_latest,
    latest_key,
    publish_event,
    make_reflection_result_event,
    make_failure_notification_event,
)
from ran_healing_shared.remediation_config import (
    REFLECTION_CONFIG,
)
from reflection_agent.step_events import emit_step   # SSE step streaming

_GCP_PROJECT        = os.environ.get("GOOGLE_CLOUD_PROJECT","poc-z-in2300756")
_SPANNER_INSTANCE   = os.environ.get("SPANNER_INSTANCE","verizon-gnn")
_SPANNER_DATABASE   = os.environ.get("SPANNER_DATABASE","syndata")
_TOOLBOX_URL        = os.environ.get("TOOLBOX_URL", "http://localhost:5000").rstrip("/")
REFLECTION_AGENT_URL = os.environ.get("REFLECTION_AGENT_URL", "").rstrip("/")
REFLEX_AGENT_URL     = os.environ.get("REFLEX_AGENT_URL", "https://ran-reflex-test-v2-761300295499.us-central1.run.app").rstrip("/")


# ══════════════════════════════════════════════════════════════════════════════
# Natural language log helpers
# ══════════════════════════════════════════════════════════════════════════════

def _log_10_executor_received(
    event_id:      str,
    execution_ok:  bool,
    exec_state:    str,
    activation_id: str,
    action_type:   str,
    domain:        str,
    entities:      list,
) -> None:
    """Step 10 — Executor result received."""
    status_human = (
        "reports the remediation completed successfully"
        if execution_ok else
        f"reports the execution FAILED — state is '{exec_state}'"
    )
    logging.info(
        f"[Step 10] Executor result received for event '{event_id}'. "
        f"Activation '{activation_id}' {status_human}. "
        f"Action: {action_type} on {len(entities)} {domain} "
        f"{'entity' if len(entities) == 1 else 'entities'}. "
        f"Now I need to validate whether the network actually recovered — "
        f"I'll check the execution gate. "
        f"eventId={event_id} | execution_ok={execution_ok} | "
        f"state={exec_state} | activation_id={activation_id} | "
        f"action_type={action_type} | domain={domain} | "
        f"target_entities={entities}"
    )


def _log_10_executor_payload(
    event_id:          str,
    success:           bool,
    exec_state:        str,
    activation_id:     str,
    intent_id:         str,
    action_type:       str,
    domain:            str,
    root_cause:        str,
    priority:          str,
    target_entities:   list,
    affected_hex_bins: list,
    expressions:       list,
    error:             str,
) -> None:
    """Step 10 — Structured executor payload log."""
    logging.info(
        f"[Step 10] Executor Response Payload | "
        f"{json.dumps({'eventId': event_id, 'success': success, 'state': exec_state, 'activation_id': activation_id, 'intent_id': intent_id, 'action_type': action_type, 'domain': domain, 'root_cause': root_cause, 'priority': priority, 'target_entities': target_entities, 'affected_hex_bins': affected_hex_bins, 'expressions': expressions, 'error': error}, default=str)}"
    )


def _log_10_validation_start(
    event_id:        str,
    execution_ok:    bool,
    target_entities: list,
    domain:          str,
) -> None:
    """Step 10 — Validation starting."""
    logging.info(
        f"[Step 10] Starting post-remediation validation for event '{event_id}'. "
        f"Execution gate: {'PASS' if execution_ok else 'FAIL'}. "
        f"Checking {len(target_entities)} "
        f"{'entity' if len(target_entities) == 1 else 'entities'} "
        f"on the {domain} domain. "
        f"eventId={event_id} | execution_ok={execution_ok} | "
        f"target_entities={target_entities} | domain={domain}"
    )


def _log_10_gate1(
    event_id:   str,
    gate1_ok:   bool,
    success:    bool,
    exec_state: str,
) -> None:
    """Step 10 — Gate 1 result."""
    logging.info(
        f"[Step 10] Gate 1 — Execution OK | "
        f"{'The Executor confirms the remediation action completed.' if gate1_ok else 'Execution failed — cannot validate recovery without a completed action.'} "
        f"eventId={event_id} | "
        f"result={'PASS' if gate1_ok else 'FAIL'} | "
        f"success={success} | state={exec_state}"
    )


def _log_10_resolution(
    event_id:          str,
    resolved:          bool,
    gate1_ok:          bool,
    retrigger_reason:  str | None,
    validation_status: str,
    gui_status:        str,
    topology_state:    str,
) -> None:
    """Step 10 — Final resolution decision."""
    if resolved:
        verdict = (
            f"Execution gate passed. "
            f"Publishing IMO_COMPLIES — the healing was successful. "
            f"GUI status: {gui_status}, topology: {topology_state}."
        )
    else:
        verdict = (
            f"Validation failed — {retrigger_reason}. "
            f"I'll retrigger the pipeline so the agents can try again."
        )
    logging.info(
        f"[Step 10] Resolution Decision for event '{event_id}'. "
        f"{verdict} "
        f"eventId={event_id} | resolved={resolved} | "
        f"gate1_execution={gate1_ok} | "
        f"status={validation_status} | gui={gui_status}"
    )


def _log_10_retrigger(
    event_id:          str,
    attempts:          int,
    max_attempts:      int,
    retrigger_reason:  str,
    retrigger_payload: dict,
) -> None:
    """Step 10 OUT — Retriggering the pipeline."""
    logging.info(
        f"[Step 10 OUT] Retriggering the healing pipeline for event '{event_id}'. "
        f"This is attempt {attempts} of {max_attempts}. "
        f"Reason: {retrigger_reason}. "
        f"Firing a new failure notification so ReflexAgent can start a fresh cycle. "
        f"eventId={event_id} | attempt={attempts}/{max_attempts} | "
        f"reason={retrigger_reason}"
    )
    logging.info(
        f"[Step 10 OUT] Retrigger Payload | "
        f"{json.dumps(retrigger_payload, default=str)}"
    )


def _log_10_complete(
    event_id:          str,
    validation_status: str,
    gate1_ok:          bool,
    gui_status:        str,
    network_status:    str,
) -> None:
    """Step 10 — ReflectionAgent cycle complete."""
    logging.info(
        f"[Step 10] ReflectionAgent cycle complete for event '{event_id}'. "
        f"Final status: {validation_status} | GUI: {gui_status} | "
        f"Network: {network_status}. "
        f"eventId={event_id} | status={validation_status} | "
        f"gate1_execution={gate1_ok} | gui={gui_status} | "
        f"network_status={network_status}"
    )


def _log_10_max_retries(
    event_id:     str,
    attempts:     int,
    max_attempts: int,
) -> None:
    logging.warning(
        f"[Step 10] MAX RETRIGGER ATTEMPTS REACHED for event '{event_id}'. "
        f"After {attempts} attempts the network has still not recovered. "
        f"Stopping the pipeline — manual intervention required. "
        f"eventId={event_id} | attempts={attempts} | max={max_attempts}"
    )


# ── Utilities ──────────────────────────────────────────────────────────────────

def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out  = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


# ── Optional HTTP POST helper ──────────────────────────────────────────────────

def _post_if_configured(url: str, payload: dict, label: str, event_id: str) -> None:
    """POST payload to url only if url is non-empty. Never raises."""
    if not url:
        logging.info(
            f"[{label}] URL not configured — skipping POST | eventId={event_id}"
        )
        return
    logging.info(f"[{label}] POST | eventId={event_id} | url={url}")
    logging.info(f"[{label}] Request Payload | {json.dumps(payload, default=str)}")
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        logging.info(
            f"[{label}] POST SUCCESS | eventId={event_id} | "
            f"http_status={resp.status_code}"
        )
        logging.info(
            f"[{label}] Response Payload | {json.dumps(resp.json(), default=str)}"
        )
    except Exception as e:
        logging.warning(
            f"[{label}] POST FAILED | eventId={event_id} | "
            f"url={url} | error={str(e)}"
        )


# ── Retrigger payload builder ──────────────────────────────────────────────────

def _build_retrigger_failure_payload(
    state:       dict,
    exec_result: dict,
    attempts:    int,
) -> dict:
    """Reconstruct a 2b-style FailureInjectionCreateEvent for ReflexAgent retrigger."""
    original_event   = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    original_payload = original_event.get("payload", {})
    target_entities  = exec_result.get("target_entities", [])
    domain = (
        original_payload.get("domain")
        or exec_result.get("domain")
        or "RAN"
    ).upper()

    affected_enodebs       = original_payload.get("affected_enodebs")
    affected_core_elements = original_payload.get("affected_core_elements")
    if affected_enodebs is None or affected_core_elements is None:
        core_prefixes    = ("HSS", "MME", "AMF", "UPF", "SMF")
        affected_enodebs = [
            eid for eid in target_entities
            if eid.upper().startswith(("ENB", "GNB"))
        ]
        affected_core_elements = [
            eid for eid in target_entities
            if eid.upper().startswith(core_prefixes)
        ]

    affected_layers = original_payload.get("affected_layers")
    if not affected_layers:
        affected_layers = (
            ["HSS", "MME", "eNodeB", "Hex Bin"]
            if domain == "CORE"
            else ["eNodeB", "Hex Bin"]
        )

    base_event_id = (
        original_payload.get("eventId")
        or exec_result.get("eventId")
        or "EV-RETRIGGER"
    )

    return {
        "id":           f"{original_payload.get('id', 'retrigger')}-R{attempts}",
        "eventId":      f"{base_event_id}-RETRY-{attempts}",
        "eventTime":    datetime.now(timezone.utc).isoformat(),
        "eventType":    original_payload.get("eventType", "FailureInjectionCreateEvent"),
        "sourceSystem": "REFLECTION_AGENT",
        "probableDomain": original_payload.get("probableDomain", "CROSS_DOMAIN"),
        "trigger": (
            original_payload.get("trigger")
            or exec_result.get("root_cause")
            or "reflection_retrigger"
        ),
        "useCaseId": original_payload.get(
            "useCaseId",
            "uc2" if domain == "CORE" else "uc1",
        ),
        "domain":                    domain,
        "affected_layers":           affected_layers,
        "affected_core_elements":    affected_core_elements or [],
        "affected_enodebs":          affected_enodebs or [],
        "affected_neighbor_enodebs": original_payload.get("affected_neighbor_enodebs", []),
    }


# ── Tool 1: check_execution_result ────────────────────────────────────────────

def check_execution_result(tool_context: ToolContext) -> str:
    """
    Tool 1 of 2 — NO arguments.
    Reads execution.completed event. Stores parsed result in state.
    """
    state      = tool_context.state
    exec_event = consume_latest(state, EVT_EXECUTION_COMPLETED)

    if not exec_event:
        logging.error("[ReflectionAgent] No execution.completed event found in state")
        return "CHECK_ERROR: No execution.completed event found in state"

    if state.get("reflection_last_exec_event_id") == exec_event["event_id"]:
        return "CHECK_SKIPPED: Already processed this execution event"

    payload   = exec_event.get("payload", {})
    event_id  = payload.get("eventId", "")
    success   = payload.get("success", False)
    state_str = payload.get("state", "failed")
    error     = payload.get("error", "")

    execution_ok = (
        success and state_str == REFLECTION_CONFIG["execution_success_state"]
    )

    # TMF921 intent — Doc2 schema
    tmf921            = payload.get("tmf921_intent", {})
    expressions       = tmf921.get("expressions", [])
    target_entities   = tmf921.get("target_entities", [])
    domain            = tmf921.get("domain", "UNKNOWN")
    root_cause        = tmf921.get("root_cause", "")
    priority          = tmf921.get("priority", "CRITICAL")
    affected_hex_bins = (
        payload.get("affected_hex_bins")
        or tmf921.get("affected_hex_bins")
        or []
    )

    # TMF641 order — Doc2 schema
    tmf641      = payload.get("tmf641_order", {})
    order_items = tmf641.get("order_items", [])
    action_type = ""
    if order_items:
        svc   = order_items[0].get("service", {})
        chars = {c["name"]: c["value"] for c in svc.get("service_characteristics", [])}
        action_type = chars.get("action_type", "")
        if not target_entities:
            raw = chars.get("target_entities", "")
            target_entities = [e.strip() for e in raw.split(",") if e.strip()]
        if not affected_hex_bins:
            raw_hex = chars.get("affected_hex_bins", "")
            affected_hex_bins = [h.strip() for h in raw_hex.split(",") if h.strip()]

    if isinstance(affected_hex_bins, str):
        affected_hex_bins = [h.strip() for h in affected_hex_bins.split(",") if h.strip()]
    affected_hex_bins = _dedupe(affected_hex_bins)

    activation_id = payload.get("activation_id") or tmf921.get("activation_id", "")
    intent_id     = payload.get("intent_id")     or tmf921.get("intent_id", "")

    # ── Step 10 IN logs ───────────────────────────────────────────────────────
    _log_10_executor_received(
        event_id, execution_ok, state_str,
        activation_id, action_type, domain, target_entities,
    )
    _log_10_executor_payload(
        event_id, success, state_str, activation_id, intent_id,
        action_type, domain, root_cause, priority,
        target_entities, affected_hex_bins, expressions, error,
    )
    emit_step(
        event_id,
        "exec_result", "done",
        meta=(f"activation={activation_id} · success={success} · "
              f"state={state_str} · {domain}"),
        payload={"activation_id": activation_id, "success": success,
                 "state": state_str, "domain": domain,
                 "target_entities": target_entities},
    )

    state["reflection_exec_result"] = {
        "eventId":           event_id,
        "event_id":          exec_event["event_id"],
        "execution_ok":      execution_ok,
        "success":           success,
        "state":             state_str,
        "error":             error,
        "activation_id":     activation_id,
        "intent_id":         intent_id,
        "action_type":       action_type,
        "target_entities":   target_entities,
        "affected_hex_bins": affected_hex_bins,
        "recovery_targets":  expressions,
        "domain":            domain,
        "root_cause":        root_cause,
        "priority":          priority,
    }
    state["reflection_last_exec_event_id"] = exec_event["event_id"]

    return (
        f"CHECK_COMPLETE: execution_ok={execution_ok} | "
        f"state={state_str} | "
        f"activation_id={activation_id} | "
        f"intent_id={intent_id}"
    )


# ── Tool 2: evaluate_and_publish ──────────────────────────────────────────────

def evaluate_and_publish(tool_context: ToolContext) -> str:
    """
    Tool 2 of 2 — NO arguments.

    Gate 1: execution_ok  [ACTIVE]   → success=True AND state="completed"
    Gate 2: anomaly_label [DISABLED] → Spanner aw_base_hex07_anom (enable when ready)
    Gate 3: is_degraded   [DISABLED] → Spanner performance        (enable when ready)

    resolved = Gate1 only.
    Z-score comparison removed — not required while Gate 1 is the only check.
    """
    state       = tool_context.state
    exec_result = state.get("reflection_exec_result", {})

    if not exec_result:
        logging.error(
            "[ReflectionAgent] No execution result in state — "
            "run check_execution_result first"
        )
        return "EVAL_ERROR: No execution result in state. Run check_execution_result first."

    source_id         = exec_result.get("event_id", "")
    event_id          = exec_result.get("eventId", "")
    execution_ok      = exec_result.get("execution_ok", False)
    target_entities   = exec_result.get("target_entities", [])
    affected_hex_bins = exec_result.get("affected_hex_bins", [])
    domain            = exec_result.get("domain", "UNKNOWN")

    # ── Step 10 validation start log ──────────────────────────────────────────
    _log_10_validation_start(event_id, execution_ok, target_entities, domain)
    emit_step(event_id, "gate1", "running",
        meta="Checking execution_ok = success=True AND state=completed…")

    # ── Gate 1: Execution OK  [ACTIVE] ────────────────────────────────────────
    gate1_ok = execution_ok

    # ── Step 10 Gate 1 log ────────────────────────────────────────────────────
    _log_10_gate1(
        event_id, gate1_ok,
        exec_result.get("success", False),
        exec_result.get("state", "unknown"),
    )
    emit_step(
        event_id,
        "gate1", "done" if gate1_ok else "error",
        meta=(f"{'PASS' if gate1_ok else 'FAIL'} · "
              f"success={exec_result.get('success')} · "
              f"state={exec_result.get('state')}"),
        payload={"gate1_ok": gate1_ok,
                 "success": exec_result.get("success"),
                 "state": exec_result.get("state")},
    )

    # ── Gate 2: Anomaly label  [COMMENTED OUT — enable when Spanner ready] ────
    # gate2_ok = False
    # labels   = []
    # try:
    #     h3_hex_bins      = affected_hex_bins or _resolve_hex_bins(target_entities)
    #     gate2_ok, labels = _gate2_anomaly_label(h3_hex_bins)
    # ...
    # logging.info(f"[Step 10] Gate 2 — Anomaly Label | result=... | labels={labels}")

    # ── Gate 3: is_degraded  [COMMENTED OUT — enable when Spanner ready] ──────
    # gate3_ok = False
    # degraded = []
    # try:
    #     gate3_ok, degraded = _gate3_is_degraded(target_entities)
    # ...
    # logging.info(f"[Step 10] Gate 3 — is_degraded | result=... | degraded={degraded}")

    # ── Resolution  (add `and gate2_ok and gate3_ok` when gates are enabled) ──
    resolved = gate1_ok

    max_attempts = REFLECTION_CONFIG["max_retrigger_attempts"]
    attempts     = state.get("retrigger_count", 0)

    if not resolved:
        attempts += 1
        state["retrigger_count"] = attempts

    max_retries_reached = not resolved and attempts >= max_attempts
    if max_retries_reached:
        _log_10_max_retries(event_id, attempts, max_attempts)

    retrigger_reason = None
    if not resolved:
        retrigger_reason = f"Execution failed: {exec_result.get('error', 'unknown')}"

    topology_state    = _pick_topology_state(domain, resolved)
    business_view     = "UTILITY_SCORE_NORMAL"      if resolved else "UTILITY_SCORE_DEGRADED"
    service_view      = "KEI_NORMAL"                if resolved else "KEI_DEGRADED"
    gui_status        = "HEALTHY_ENVIRONMENT"       if resolved else "DEGRADED_ENVIRONMENT"
    gnn_topo_view     = "STABLE_ENVIRONMENT_GRAPH"  if resolved else "UNSTABLE_ENVIRONMENT_GRAPH"
    validation_status = (
        "IMO_COMPLIES"              if resolved
        else "FAILED_AFTER_RETRIES" if max_retries_reached
        else "RETRIGGER_INVESTIGATION"
    )
    next_network_status = (
        "RESOLVED" if resolved
        else "FAILED" if max_retries_reached
        else "ANOMALY_DETECTED"
    )

    # ── Step 10 resolution log ────────────────────────────────────────────────
    _log_10_resolution(
        event_id, resolved, gate1_ok,
        retrigger_reason, validation_status, gui_status, topology_state,
    )
    emit_step(
        event_id,
        "resolution", "done" if resolved else "error",
        meta=(f"resolved={resolved} · status={validation_status} · "
              f"gui={gui_status}"),
        payload={"resolved": resolved, "status": validation_status,
                 "gui_status": gui_status, "gate1_execution": gate1_ok},
    )

    # ── Build validation output ───────────────────────────────────────────────
    validation_output = {
        "eventId":  event_id,
        "status":   validation_status,
        "resolved": resolved,
        "imo_status": {
            "complies": resolved,
            "reason": (
                "Execution gate passed — network remediation confirmed"
                if resolved
                else retrigger_reason
            ),
        },
        "validation_gates": {
            "gate1_execution_ok": gate1_ok,
            # gate2_anomaly_normal: None,   # not yet active
            # gate3_not_degraded:   None,   # not yet active
        },
        "execution_ok":      execution_ok,
        "execution_state":   exec_result.get("state"),
        "execution_error":   exec_result.get("error", ""),
        "topology_state":    topology_state,
        "business_view":     business_view,
        "service_view":      service_view,
        "gui_status":        gui_status,
        "gnn_topology_view": gnn_topo_view,
        "activation_id":     exec_result.get("activation_id"),
        "intent_id":         exec_result.get("intent_id"),
        "domain":            domain,
        "root_cause":        exec_result.get("root_cause"),
        "target_entities":   target_entities,
        "affected_hex_bins": affected_hex_bins,
        "recovery_targets":  exec_result.get("recovery_targets", []),
        "retrigger_count":   attempts,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
    }

    if not resolved:
        validation_output["retrigger_reason"] = retrigger_reason
        validation_output["next_target"] = (
            None if max_retries_reached else "ReflexAgent"
        )

    # ── Publish reflection.result on ADK event bus ────────────────────────────
    reflection_event = make_reflection_result_event(
        source_event_id=source_id,
        resolved=resolved,
        reflection_output=validation_output,
    )
    reflection_event["network_status"] = next_network_status
    publish_event(state, reflection_event)

    # ── POST to GUI dashboard (optional) ─────────────────────────────────────
    reflection_payload = {
        "status":       validation_output["status"],
        "eventId":      event_id,
        "resolved":     resolved,
        "execution_ok": execution_ok,
        "validation_gates": validation_output["validation_gates"],
        "gui_dashboard": {
            "business_view":  business_view,
            "service_view":   service_view,
            "gui_status":     gui_status,
            "gnn_topology":   gnn_topo_view,
            "topology_state": topology_state,
        },
        "tmf_metadata": {
            "activation_id":   exec_result.get("activation_id"),
            "intent_id":       exec_result.get("intent_id"),
            "domain":          domain,
            "target_entities": target_entities,
        },
        "retrigger": {
            "attempt":      attempts,
            "max_attempts": max_attempts,
            "reason":       retrigger_reason,
        },
        "timestamp": validation_output["timestamp"],
    }

    _post_if_configured(
        url      = f"{REFLECTION_AGENT_URL}/reflection-result" if REFLECTION_AGENT_URL else "",
        payload  = reflection_payload,
        label    = "Step 10 OUT ReflectionAgent → GUI/Orchestrator",
        event_id = event_id,
    )
    emit_step(
        event_id,
        "published", "done",
        meta=(f"network_status={next_network_status} · {validation_status}"),
        payload={"status": validation_status, "resolved": resolved,
                 "network_status": next_network_status,
                 "activation_id": exec_result.get("activation_id", "")},
    )

    # ── Retrigger pipeline if not resolved and under limit ────────────────────
    if not resolved and not max_retries_reached:
        retrigger_payload = _build_retrigger_failure_payload(state, exec_result, attempts)
        publish_event(state, make_failure_notification_event(retrigger_payload))

        _log_10_retrigger(
            event_id, attempts, max_attempts,
            retrigger_reason, retrigger_payload,
        )

        _post_if_configured(
            url      = f"{REFLEX_AGENT_URL}/trigger_event" if REFLEX_AGENT_URL else "",
            payload  = retrigger_payload,
            label    = "Step 10 OUT ReflexAgent RETRIGGER",
            event_id = event_id,
        )

    state["reflection_output"] = validation_output
    state[NETWORK_STATUS_KEY]  = next_network_status

    # ── Step 10 complete log ──────────────────────────────────────────────────
    _log_10_complete(
        event_id, validation_status, gate1_ok,
        gui_status, next_network_status,
    )

    return (
        f"{next_network_status}: "
        f"status={validation_output['status']} | "
        f"gate1_execution={gate1_ok} | "
        f"gui={gui_status}"
    )


# ── Topology state helper ──────────────────────────────────────────────────────

def _pick_topology_state(domain: str, resolved: bool) -> str:
    if not resolved:
        return "UNSTABLE"
    return {
        "CORE":      "STABLE_GRAPH_CORE_V2",
        "TRANSPORT": "STABLE_GRAPH_TRANSPORT_V2",
    }.get(domain, "STABLE_GRAPH_V2")


# ── Backward-compat wrapper ────────────────────────────────────────────────────

def evaluate_resolution(tool_context: ToolContext, exec_result: dict = None) -> dict:
    """Backward compatibility wrapper — root_agent direct calls."""
    evaluate_and_publish(tool_context)
    output = tool_context.state.get("reflection_output", {})
    return {
        "status":         output.get("status", "UNKNOWN"),
        "resolved":       output.get("resolved", False),
        "gui_status":     output.get("gui_status"),
        "network_status": tool_context.state.get(NETWORK_STATUS_KEY),
    }