"""
app/agents/reflection_agent/tools.py
======================================
ReflectionAgent — 2 tools in sequence.

Slide 10 implementation:
  check_execution_result  → parses ExecutorAgent output (TMF641 v5 + TMF921)
  evaluate_and_publish    → validates post-remediation network state,
                            publishes reflection.result (IMO_COMPLIES or RETRIGGER)

Validation — 3 gates defined, Gate 1 active for now:

  Gate 1 — Execution OK                          [ACTIVE]
    Source : ExecutorAgent payload (Doc2)
    Check  : success == True AND state == "completed"
    Why    : Without successful execution nothing else is meaningful.

  resolved = Gate1 

Executor Agent output schema (Doc2):
  success, state, activation_id, intent_id, error,
  tmf921_intent.expressions, tmf921_intent.target_entities,
  tmf921_intent.domain, tmf921_intent.root_cause,
  tmf641_order.order_items[].service.service_characteristics
"""

import json
import os
import logging
import requests

logging.basicConfig(level=logging.INFO)

from datetime import datetime, timezone
from google.adk.tools import ToolContext

from app.events import (
    EVT_EXECUTION_COMPLETED,
    EVT_FAILURE_NOTIFICATION,
    NETWORK_STATUS_KEY,
    consume_latest,
    latest_key,
    publish_event,
    make_reflection_result_event,
    make_failure_notification_event,
)
from app.config.remediation_config import (
    BASELINE_Z_SCORE,
    REFLECTION_CONFIG,
)

_GCP_PROJECT         = os.environ.get("GOOGLE_CLOUD_PROJECT", "poc-z-in2300756")
_SPANNER_INSTANCE    = os.environ.get("SPANNER_INSTANCE", "verizon-gnn")
_SPANNER_DATABASE    = os.environ.get("SPANNER_DATABASE", "syndata")
_TOOLBOX_URL         = os.environ.get("TOOLBOX_URL", "http://localhost:5000").rstrip("/")
REFLECTION_AGENT_URL = os.environ.get("REFLECTION_AGENT_URL", "").rstrip("/")
REFLEX_AGENT_URL     = os.environ.get("REFLEX_AGENT_URL", "").rstrip("/")


# ── Utilities ──────────────────────────────────────────────────────────────────

def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out = []
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


# ── MCP Toolbox helpers (shared pattern with ReflexAgent) ─────────────────────

def _is_toolbox_running() -> bool:
    try:
        resp = requests.post(
            f"{_TOOLBOX_URL}/mcp",
            headers={"Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "reflection-agent", "version": "1.0"},
                },
            },
            timeout=3,
        )
        if 200 <= resp.status_code < 300:
            logging.info(
                f"[MCP] Toolbox running at {_TOOLBOX_URL}/mcp "
                f"(status {resp.status_code})"
            )
            return True
    except requests.exceptions.ConnectionError:
        return False
    except Exception as e:
        logging.debug(f"[MCP] Health check /mcp failed: {e}")
    return False


def _invoke_tool(tool_name: str, params: dict) -> list[dict]:
    """Call MCP Toolbox via JSON-RPC 2.0 over POST /mcp."""
    import uuid
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    try:
        resp = requests.post(
            f"{_TOOLBOX_URL}/mcp",
            headers={"Content-Type": "application/json"},
            json=rpc_payload,
            timeout=15,
        )
        if resp.status_code != 200:
            logging.warning(
                f"[MCP] {tool_name} returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return []
        body = resp.json()
        if "error" in body:
            logging.warning(f"[MCP] {tool_name} RPC error: {body['error']}")
            return []
        rows: list[dict] = []
        for block in body.get("result", {}).get("content", []):
            if block.get("type") != "text":
                continue
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                logging.warning(
                    f"[MCP] {tool_name} non-JSON text: {block.get('text','')[:100]}"
                )
                continue
            if isinstance(parsed, list):
                rows.extend(parsed)
            elif isinstance(parsed, dict):
                rows.append(parsed)
        logging.info(f"[MCP] {tool_name} -> {len(rows)} rows")
        return rows
    except requests.exceptions.ConnectionError:
        logging.warning(f"[MCP] {tool_name}: connection refused")
        return []
    except requests.exceptions.Timeout:
        logging.warning(f"[MCP] {tool_name}: timeout after 15s")
        return []
    except Exception as e:
        logging.warning(f"[MCP] {tool_name} failed: {e}")
        return []


# ── Retrigger payload builder ──────────────────────────────────────────────────

def _build_retrigger_failure_payload(
    state:       dict,
    exec_result: dict,
    attempts:    int,
) -> dict:
    """
    Reconstruct a 2b-style FailureInjectionCreateEvent for ReflexAgent retrigger.
    Preserves original payload fields where available, derives missing fields
    from exec_result target_entities.
    """
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
        "domain":                  domain,
        "affected_layers":         affected_layers,
        "affected_core_elements":  affected_core_elements or [],
        "affected_enodebs":        affected_enodebs or [],
        "affected_neighbor_enodebs": original_payload.get(
            "affected_neighbor_enodebs", []
        ),
    }


# ── Tool 1: check_execution_result ────────────────────────────────────────────

def check_execution_result(tool_context: ToolContext) -> str:
    """
    Tool 1 of 2 — NO arguments.

    Reads execution.completed event from session state.
    Parses ExecutorAgent output (Doc2: TMF641 v5 + TMF921).
    Stores parsed result in state["reflection_exec_result"].
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

    logging.info(
        f"[Step 10] ExecutorAgent → ReflectionAgent | "
        f"eventId={event_id} | "
        f"execution_ok={execution_ok} | "
        f"success={success} | "
        f"state={state_str} | "
        f"activation_id={activation_id} | "
        f"intent_id={intent_id} | "
        f"action_type={action_type} | "
        f"domain={domain} | "
        f"target_entities={target_entities} | "
        f"affected_hex_bins={affected_hex_bins}"
    )

    logging.info(
        f"[Step 10] Executor Response Payload | "
        f"{json.dumps({
            'eventId':           event_id,
            'success':           success,
            'state':             state_str,
            'activation_id':     activation_id,
            'intent_id':         intent_id,
            'action_type':       action_type,
            'domain':            domain,
            'root_cause':        root_cause,
            'priority':          priority,
            'target_entities':   target_entities,
            'affected_hex_bins': affected_hex_bins,
            'expressions':       expressions,
            'error':             error,
        }, default=str)}"
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

    Slide 10 — validation:
      Gate 1: execution_ok  [ACTIVE]   → Executor success=True AND state="completed"
      Gate 2: anomaly_label [DISABLED] → Spanner aw_base_hex07_anom (enable when ready)
      Gate 3: is_degraded   [DISABLED] → Spanner performance        (enable when ready)

      resolved = Gate1 only (Gates 2+3 will be ANDed in when enabled)

    IMO_COMPLIES  → reflection.result published, network_status=RESOLVED
    RETRIGGER     → reflection.result + Step 2b trigger event re-fired to ReflexAgent
    MAX_RETRIES   → pipeline stopped, network_status=FAILED
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

    # Pre-action z-score — set by ReflexAgent call_gnn_engine
    pre_z = float(
        state.get("pre_action_z_score")
        or state.get("triage_result", {}).get("composite_score")
        or state.get("latest_gnn_result", {}).get("anomalyScore", {}).get("compositeScore")
        or 0.0
    )

    logging.info(
        f"[Step 10] ReflectionAgent Validation START | "
        f"eventId={event_id} | "
        f"execution_ok={execution_ok} | "
        f"pre_action_z={pre_z} | "
        f"target_entities={target_entities} | "
        f"affected_hex_bins={affected_hex_bins} | "
        f"domain={domain}"
    )

    # ── Gate 1: Execution OK  [ACTIVE] ───────────────────────────────────────
    gate1_ok = execution_ok

    logging.info(
        f"[Step 10] Gate 1 — Execution OK | "
        f"eventId={event_id} | "
        f"result={'PASS' if gate1_ok else 'FAIL'} | "
        f"success={exec_result.get('success')} | "
        f"state={exec_result.get('state')}"
    )

    # ── Resolution  (add `and gate2_ok and gate3_ok` when gates are enabled) ─
    resolved = gate1_ok

    # Retrigger limit
    max_attempts = REFLECTION_CONFIG["max_retrigger_attempts"]
    attempts     = state.get("retrigger_count", 0)

    if not resolved:
        attempts += 1
        state["retrigger_count"] = attempts

    max_retries_reached = not resolved and attempts >= max_attempts
    if max_retries_reached:
        logging.warning(
            f"[Step 10] MAX RETRIGGER ATTEMPTS REACHED | "
            f"eventId={event_id} | "
            f"attempts={attempts} | max={max_attempts}"
        )

    # Retrigger reason
    retrigger_reason = None
    if not resolved:
        retrigger_reason = (
            f"Execution failed: {exec_result.get('error', 'unknown')}"
        )

    # GUI / topology labels
    topology_state    = _pick_topology_state(domain, resolved)
    business_view     = "UTILITY_SCORE_NORMAL"     if resolved else "UTILITY_SCORE_DEGRADED"
    service_view      = "KEI_NORMAL"               if resolved else "KEI_DEGRADED"
    gui_status        = "HEALTHY_ENVIRONMENT"      if resolved else "DEGRADED_ENVIRONMENT"
    gnn_topo_view     = "STABLE_ENVIRONMENT_GRAPH" if resolved else "UNSTABLE_ENVIRONMENT_GRAPH"
    validation_status = (
        "IMO_COMPLIES"        if resolved
        else "FAILED_AFTER_RETRIES" if max_retries_reached
        else "RETRIGGER_INVESTIGATION"
    )
    next_network_status = (
        "RESOLVED" if resolved
        else "FAILED" if max_retries_reached
        else "ANOMALY_DETECTED"
    )

    logging.info(
        f"[Step 10] Resolution Decision | "
        f"eventId={event_id} | "
        f"resolved={resolved} | "
        f"gate1_execution={gate1_ok} | "
        f"status={validation_status} | "
        f"gui={gui_status}"
    )

    # ── Build validation output ──────────────────────────────────────────────
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
        "zscore_comparison": {
            "pre_action_z": round(pre_z, 3),
            "baseline":     BASELINE_Z_SCORE,
        },
        "execution_ok":    execution_ok,
        "execution_state": exec_result.get("state"),
        "execution_error": exec_result.get("error", ""),
        "topology_state":  topology_state,
        "business_view":   business_view,
        "service_view":    service_view,
        "gui_status":      gui_status,
        "gnn_topology_view": gnn_topo_view,
        "activation_id":   exec_result.get("activation_id"),
        "intent_id":       exec_result.get("intent_id"),
        "domain":          domain,
        "root_cause":      exec_result.get("root_cause"),
        "target_entities": target_entities,
        "affected_hex_bins": affected_hex_bins,
        "recovery_targets": exec_result.get("recovery_targets", []),
        "retrigger_count": attempts,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }

    if not resolved:
        validation_output["retrigger_reason"] = retrigger_reason
        validation_output["next_target"] = (
            None if max_retries_reached else "ReflexAgent"
        )

    # ── Publish reflection.result on ADK event bus ───────────────────────────
    reflection_event = make_reflection_result_event(
        source_event_id=source_id,
        resolved=resolved,
        reflection_output=validation_output,
    )
    reflection_event["network_status"] = next_network_status
    publish_event(state, reflection_event)

    # ── POST to GUI / orchestrator ────────────────────────────────────────────
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

    reflection_response = {
        "status":         validation_output["status"],
        "resolved":       resolved,
        "network_status": next_network_status,
        "gui_status":     gui_status,
        "topology_state": topology_state,
        "gnn_topology":   gnn_topo_view,
    }

    if REFLECTION_AGENT_URL:
        reflection_url = f"{REFLECTION_AGENT_URL}/reflection-result"
        logging.info(
            f"[Step 10 OUT] ReflectionAgent → GUI/Orchestrator | "
            f"eventId={event_id} | "
            f"url={reflection_url} | "
            f"status={validation_output['status']}"
        )
        logging.info(
            f"[Step 10 OUT] Reflection Request Payload | "
            f"{json.dumps(reflection_payload, default=str)}"
        )
        try:
            resp = requests.post(reflection_url, json=reflection_payload, timeout=30)
            resp.raise_for_status()
            logging.info(
                f"[Step 10 OUT] Reflection POST SUCCESS | "
                f"eventId={event_id} | http_status={resp.status_code}"
            )
            logging.info(
                f"[Step 10 OUT] Reflection Response Payload | "
                f"{json.dumps(resp.json(), default=str)}"
            )
        except Exception as e:
            logging.warning(
                f"[Step 10 OUT] Reflection POST FAILED | "
                f"eventId={event_id} | url={reflection_url} | error={str(e)}"
            )
    else:
        logging.info(
            f"[Step 10 OUT] Reflection Response Payload | "
            f"{json.dumps(reflection_response, default=str)}"
        )

    # ── Re-trigger pipeline if not resolved and under limit ───────────────────
    if not resolved and not max_retries_reached:
        retrigger_payload = _build_retrigger_failure_payload(
            state, exec_result, attempts
        )
        publish_event(state, make_failure_notification_event(retrigger_payload))

        logging.info(
            f"[Step 10 OUT] ReflectionAgent → ReflexAgent RETRIGGER | "
            f"eventId={event_id} | "
            f"attempt={attempts}/{max_attempts} | "
            f"reason={retrigger_reason}"
        )
        logging.info(
            f"[Step 10 OUT] Retrigger Payload | "
            f"{json.dumps(retrigger_payload, default=str)}"
        )

        if REFLEX_AGENT_URL:
            retrigger_url = f"{REFLEX_AGENT_URL}/trigger_event"
            try:
                resp = requests.post(retrigger_url, json=retrigger_payload, timeout=30)
                resp.raise_for_status()
                validation_output["retrigger_post_status"] = "POSTED"
                validation_output["retrigger_url"]         = retrigger_url
                logging.info(
                    f"[Step 10 OUT] ReflexAgent RETRIGGER POST SUCCESS | "
                    f"eventId={event_id} | http_status={resp.status_code}"
                )
            except Exception as e:
                validation_output["retrigger_post_status"] = "FAILED"
                validation_output["retrigger_url"]         = retrigger_url
                validation_output["retrigger_post_error"]  = str(e)
                logging.warning(
                    f"[Step 10 OUT] ReflexAgent RETRIGGER POST FAILED | "
                    f"eventId={event_id} | url={retrigger_url} | error={str(e)}"
                )
        else:
            validation_output["retrigger_post_status"] = "SKIPPED_NO_REFLEX_AGENT_URL"

    state["reflection_output"] = validation_output
    state[NETWORK_STATUS_KEY]  = next_network_status

    logging.info(
        f"[Step 10] ReflectionAgent COMPLETE | "
        f"eventId={event_id} | "
        f"status={validation_output['status']} | "
        f"gate1_execution={gate1_ok} | "
        f"gui={gui_status} | "
        f"network_status={next_network_status}"
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
    """
    Backward compatibility wrapper — root_agent direct calls.
    Delegates to evaluate_and_publish (exec_result arg ignored).
    """
    evaluate_and_publish(tool_context)
    output = tool_context.state.get("reflection_output", {})
    return {
        "status":         output.get("status", "UNKNOWN"),
        "resolved":       output.get("resolved", False),
        "gui_status":     output.get("gui_status"),
        "network_status": tool_context.state.get(NETWORK_STATUS_KEY),
    }