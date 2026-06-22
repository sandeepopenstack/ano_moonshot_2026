"""
ReflectionAgent — 2 tools in sequence.
Validation:
  Gate 1: execution_ok = success AND state == "completed"
  Gate 2: change request clearance validation from tmf_node_changerequest
          using eventTime from GCS and MCP/Spanner fallback.
          Gate 2 passes only when no change request remains for the same date.

"""

import json
import os
import logging
import requests
from google.cloud import storage
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
_SPANNER_DATABASE   = os.environ.get("SPANNER_DATABASE","tmforum_xl")
_TOOLBOX_URL        = os.environ.get("TOOLBOX_URL", "http://localhost:5000").rstrip("/")
REFLECTION_AGENT_URL = os.environ.get("REFLECTION_AGENT_URL", "").rstrip("/")
REFLEX_AGENT_URL     = os.environ.get("REFLEX_AGENT_URL", "https://ran-reflex-test-v2-761300295499.us-central1.run.app").rstrip("/")
_GCS_BUCKET = os.environ.get("GCS_BUCKET", "vz-tmforum-2026")
_GCS_BLOB_PATH = os.environ.get("GCS_BLOB_PATH","agent_persistent_data/agent-execution-properties.json")
MCP_TOOL_CHANGE_REQUEST = os.environ.get(
    "MCP_TOOL_CHANGE_REQUEST",
    "query_tmf_node_changerequest"
)

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
    validation_failure_reason:  str | None,
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
            f"Validation failed — {validation_failure_reason}. "
            f"Retrigger is disabled for this validation flow; publishing VALIDATION_FAILED."
        )
    logging.info(
        f"[Step 10] Resolution Decision for event '{event_id}'. "
        f"{verdict} "
        f"eventId={event_id} | resolved={resolved} | "
        f"gate1_execution={gate1_ok} | "
        f"status={validation_status} | gui={gui_status}"
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

# ── Utilities ──────────────────────────────────────────────────────────────────

def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out  = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out

def _read_event_time_from_gcs() -> str:
    """
    Read the original 2B eventTime from the GCS file written by Reflex.
    Supports both:
      1. {"payload": {"eventTime": "..."}}
      2. {"eventTime": "..."}
    """
    try:
        client = storage.Client(project=_GCP_PROJECT)
        bucket = client.bucket(_GCS_BUCKET)
        blob = bucket.blob(_GCS_BLOB_PATH)

        raw = blob.download_as_text()
        data = json.loads(raw)

        event_time = (
            data.get("payload", {}).get("eventTime")
            or data.get("eventTime")
            or data.get("payload", {}).get("event_time")
            or data.get("event_time")
        )

        if event_time:
            logging.info(
                "[ReflectionAgent] Read eventTime from GCS | gs://%s/%s | eventTime=%s",
                _GCS_BUCKET,
                _GCS_BLOB_PATH,
                event_time,
            )
            return event_time

        logging.warning(
            "[ReflectionAgent] eventTime not found in GCS file | gs://%s/%s",
            _GCS_BUCKET,
            _GCS_BLOB_PATH,
        )
        return ""

    except Exception as e:
        logging.warning(
            "[ReflectionAgent] Failed to read eventTime from GCS | gs://%s/%s | error=%s",
            _GCS_BUCKET,
            _GCS_BLOB_PATH,
            str(e),
        )
        return ""

def _is_toolbox_running() -> bool:
    """
    Health check MCP Toolbox and verify query_tmf_node_changerequest tool exists.
    """
    if not _TOOLBOX_URL:
        logging.warning("[ReflectionAgent][MCP] TOOLBOX_URL is empty")
        return False

    try:
        resp = requests.post(
            f"{_TOOLBOX_URL}/mcp",
            headers={"Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            timeout=30,
        )

        if 200 <= resp.status_code < 300:
            body = resp.json()
            tools = body.get("result", {}).get("tools", [])
            tool_names = [t.get("name") for t in tools]

            logging.info(
                "[ReflectionAgent][MCP] Toolbox running at %s/mcp | tools=%s",
                _TOOLBOX_URL,
                tool_names,
            )

            if MCP_TOOL_CHANGE_REQUEST not in tool_names:
                logging.warning(
                    "[ReflectionAgent][MCP] Required tool '%s' not found. Available tools=%s",
                    MCP_TOOL_CHANGE_REQUEST,
                    tool_names,
                )
                return False

            return True

        logging.warning(
            "[ReflectionAgent][MCP] Health check returned HTTP %s | body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return False

    except Exception as e:
        logging.warning(
            "[ReflectionAgent][MCP] Health check failed | url=%s/mcp | error=%s",
            _TOOLBOX_URL,
            str(e),
        )
        return False


def _invoke_mcp_tool(tool_name: str, params: dict) -> list[dict]:
    """
    Invoke MCP Toolbox tool through JSON-RPC.
    Expected tool: query_tmf_node_changerequest.
    """
    url = f"{_TOOLBOX_URL}/mcp"

    rpc_payload = {
        "jsonrpc": "2.0",
        "id": f"reflection-{datetime.now(timezone.utc).isoformat()}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": params,
        },
    }

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=rpc_payload,
            timeout=30,
        )

        if resp.status_code != 200:
            logging.warning(
                "[ReflectionAgent][MCP] %s returned HTTP %s | body=%s",
                tool_name,
                resp.status_code,
                resp.text[:500],
            )
            return []

        body = resp.json()

        if "error" in body:
            logging.warning(
                "[ReflectionAgent][MCP] %s RPC error=%s",
                tool_name,
                body["error"],
            )
            return []

        rows: list[dict] = []

        for block in body.get("result", {}).get("content", []):
            if block.get("type") == "text":
                try:
                    parsed = json.loads(block.get("text", ""))

                    if isinstance(parsed, list):
                        rows.extend(parsed)
                    elif isinstance(parsed, dict):
                        rows.append(parsed)

                except json.JSONDecodeError:
                    logging.warning(
                        "[ReflectionAgent][MCP] Non-JSON text block from %s | text=%s",
                        tool_name,
                        block.get("text", "")[:200],
                    )

        logging.info(
            "[ReflectionAgent][MCP] %s returned %s rows | params=%s",
            tool_name,
            len(rows),
            json.dumps(params, default=str),
        )

        return rows

    except Exception as e:
        logging.warning(
            "[ReflectionAgent][MCP] %s failed | error=%s",
            tool_name,
            str(e),
        )
        return []

def _query_change_request_direct(
    event_time: str,
    change_request_id: str = "",
) -> list[dict] | None:

    if not event_time:
        logging.warning("[ReflectionAgent] No event_time provided for Spanner CR query")
        return None

    from google.cloud import spanner as spanner_lib

    client = spanner_lib.Client(project=_GCP_PROJECT)
    instance = client.instance(_SPANNER_INSTANCE)
    db = instance.database(_SPANNER_DATABASE)

    rows_out: list[dict] = []

    sql = """
        SELECT
          change_request_id,
          event_time,
          close_time,
          status,
          change_type_name,
          risk_score,
          reversibility_score
        FROM tmf_node_changerequest
        WHERE DATE(event_time) = DATE(TIMESTAMP(@event_time))
    """

    params = {
        "event_time": event_time,
    }

    param_types = {
        "event_time": spanner_lib.param_types.STRING,
    }

    if change_request_id:
        sql += " AND change_request_id = @change_request_id"
        params["change_request_id"] = change_request_id
        param_types["change_request_id"] = spanner_lib.param_types.STRING

    sql += " ORDER BY event_time DESC"

    try:
        with db.snapshot() as snap:
            for row in snap.execute_sql(
                sql,
                params=params,
                param_types=param_types,
            ):
                (
                    cr_id,
                    ev_time,
                    close_time,
                    status,
                    change_type_name,
                    risk_score,
                    reversibility_score,
                ) = row

                rows_out.append({
                    "change_request_id": cr_id,
                    "event_time": ev_time,
                    "close_time": close_time,
                    "status": status,
                    "change_type_name": change_type_name,
                    "risk_score": float(risk_score) if risk_score is not None else None,
                    "reversibility_score": (
                        float(reversibility_score)
                        if reversibility_score is not None
                        else None
                    ),
                    "source": "spanner_direct",
                })

        logging.info(
            "[ReflectionAgent] Spanner CR query returned %s rows | event_time=%s | change_request_id=%s",
            len(rows_out),
            event_time,
            change_request_id,
        )

        return rows_out

    except Exception as e:
        logging.warning(
            "[ReflectionAgent] Spanner CR query failed | event_time=%s | change_request_id=%s | error=%s",
            event_time,
            change_request_id,
            str(e),
        )
        return None
        
        
def _query_change_request_validation(
    event_time: str,
    target_entities: list[str] = None,
) -> dict:
    """
    Validate that no change request remains for the original failure date.

    Business rule:
      - Failure injection creates a change_request_id in Spanner.
      - Automation/Executor should clear/remove the change request after completion.
      - Gate 2 PASS only when no change_request_id exists for the same GCS event date.
      - If any row is returned for the same date, Gate 2 FAIL.
      - If GCS eventTime is missing or validation query fails, Gate 2 FAIL.
    """
    target_entities = target_entities or []

    # Safety guard: without GCS eventTime, Reflection cannot validate the date.
    if not event_time:
        result = {
            "event_time_from_gcs": event_time,
            "target_entities": target_entities,
            "source": "missing_gcs_event_time",
            "rows_found": 0,
            "query_success": False,
            "no_change_request_remaining": False,
            "close_ok": False,
            "rows": [],
            "error": "GCS eventTime missing; cannot validate change request clearance",
        }

        logging.warning(
            "[ReflectionAgent] Change request validation failed | %s",
            json.dumps(result, default=str),
        )

        return result

    rows: list[dict] = []
    source = "none"
    query_success = True

    # Query by GCS event date only. 
    if _is_toolbox_running():
        rows = _invoke_mcp_tool(
            MCP_TOOL_CHANGE_REQUEST,
            {
                "event_time": event_time,
                "change_request_id": "",
                "target_entities": target_entities,
            },
        )
        source = "mcp_toolbox" if rows else "mcp_toolbox_empty"

    # Fallback to direct Spanner when MCP returns no rows or MCP is unavailable.
    if not rows:
        direct_rows = _query_change_request_direct(
            event_time=event_time,
            change_request_id="",
        )

        if direct_rows is None:
            rows = []
            source = "spanner_direct_failed"
            query_success = False
        else:
            rows = direct_rows
            source = "spanner_direct" if rows else "spanner_direct_empty"

    rows_found = len(rows)

    # Gate 2 passes only when validation query succeeded and no CR rows remain.
    no_change_request_remaining = query_success and rows_found == 0

    result = {
        "event_time_from_gcs": event_time,
        "target_entities": target_entities,
        "source": source,
        "rows_found": rows_found,
        "query_success": query_success,
        "no_change_request_remaining": no_change_request_remaining,
        "close_ok": no_change_request_remaining,
        "rows": rows,
    }

    logging.info(
        "[ReflectionAgent] Change request validation result | %s",
        json.dumps(result, default=str),
    )

    return result

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
    tmf921 = payload.get("tmf921_intent", {}) 
    expressions = (tmf921.get("expressions") or payload.get("expressions") or [])
    target_entities = (payload.get("target_entities") or tmf921.get("target_entities") or [])
    domain = (payload.get("domain") or tmf921.get("domain") or "UNKNOWN")
    root_cause = (payload.get("root_cause") or tmf921.get("root_cause") or "")
    priority = (payload.get("priority") or tmf921.get("priority") or "CRITICAL")
    affected_hex_bins = (payload.get("affected_hex_bins") or tmf921.get("affected_hex_bins") or [])

    # TMF641 order — Doc2 schema
    tmf641      = payload.get("tmf641_order", {})
    order_items = (
        tmf641.get("order_items")
        or tmf641.get("serviceOrderItem")
        or []
    )
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

    Gate 1:
      execution_ok = success=True AND state="completed"

    Gate 2:
      change request clearance validation from tmf_node_changerequest.
      Uses eventTime from GCS and queries by DATE(event_time).
      Gate 2 passes only when no change request remains for the same date.

    resolved = gate1_ok AND gate2_cr_ok.
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

    # ── Gate 2: Change Request Clearance Validation ───────────────────────
    emit_step(
        event_id,
        "change_request_validation",
        "running",
        meta="Reading 2B eventTime from GCS and validating tmf_node_changerequest…",
    )

    gcs_event_time = _read_event_time_from_gcs()
    cr_validation = _query_change_request_validation(
        event_time=gcs_event_time,
        target_entities=target_entities,
    )

    gate2_cr_ok = cr_validation.get("no_change_request_remaining", False)

    emit_step(
        event_id,
        "change_request_validation",
        "done" if gate2_cr_ok else "error",
        meta=(
            f"{'PASS' if gate2_cr_ok else 'FAIL'} · "
            f"source={cr_validation.get('source')} · "
            f"rows={cr_validation.get('rows_found')} · "
            f"eventTime={gcs_event_time}"
        ),
        payload=cr_validation,
    )
    
    resolved = gate1_ok and gate2_cr_ok

    validation_failure_reason = None
    
    if not resolved:
        if not gate1_ok:
            validation_failure_reason = (
                f"Execution gate failed: "
                f"success={exec_result.get('success')} | "
                f"state={exec_result.get('state')} | "
                f"error={exec_result.get('error', 'unknown')}"
            )
        elif not gate2_cr_ok:
            validation_failure_reason = (
                "Change request validation failed: "
                f"source={cr_validation.get('source')} | "
                f"query_success={cr_validation.get('query_success')} | "
                f"rows_found={cr_validation.get('rows_found')} | "
                f"no_change_request_remaining={cr_validation.get('no_change_request_remaining')}"
            )
        else:
            validation_failure_reason = "Validation failed"
    

    topology_state    = _pick_topology_state(domain, resolved)
    business_view     = "UTILITY_SCORE_NORMAL"      if resolved else "UTILITY_SCORE_DEGRADED"
    service_view      = "KEI_NORMAL"                if resolved else "KEI_DEGRADED"
    gui_status        = "HEALTHY_ENVIRONMENT"       if resolved else "DEGRADED_ENVIRONMENT"
    gnn_topo_view     = "STABLE_ENVIRONMENT_GRAPH"  if resolved else "UNSTABLE_ENVIRONMENT_GRAPH"
    validation_status = ("IMO_COMPLIES" if resolved else "VALIDATION_FAILED")
    next_network_status = ("RESOLVED" if resolved else "FAILED")

    # ── Step 10 resolution log ────────────────────────────────────────────────
    _log_10_resolution(
        event_id, resolved, gate1_ok,
        validation_failure_reason, validation_status, gui_status, topology_state,
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
                else validation_failure_reason
            ),
        },
        "validation_gates": {
            "gate1_execution_ok": gate1_ok,
            "gate2_no_change_request_remaining": gate2_cr_ok,
        },
        "change_request_validation": cr_validation,
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
        "timestamp":         datetime.now(timezone.utc).isoformat(),
    }

    if not resolved:
        validation_output["validation_failure_reason"] = validation_failure_reason
        validation_output["next_target"] = None

    # ── Publish reflection.result on ADK event bus ────────────────────────────
    reflection_event = make_reflection_result_event(
        source_event_id=source_id,
        resolved=resolved,
        reflection_output=validation_output,
    )
    reflection_event["network_status"] = next_network_status
    publish_event(state, reflection_event)

    
    emit_step(
        event_id,
        "published", "done",
        meta=(f"network_status={next_network_status} · {validation_status}"),
        payload={"status": validation_status, "resolved": resolved,
                 "network_status": next_network_status,
                 "activation_id": exec_result.get("activation_id", "")},
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