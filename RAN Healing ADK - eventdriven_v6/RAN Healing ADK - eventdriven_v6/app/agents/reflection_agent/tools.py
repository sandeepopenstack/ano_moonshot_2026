"""
app/agents/reflection_agent/tools.py
======================================
ReflectionAgent — 2 tools in sequence.

Slide 10 implementation:
  check_execution_result  → parses ExecutorAgent output (TMF641/TMF921)
  evaluate_and_publish    → re-runs GNN on post-action topology,
                            compares z-score vs baseline,
                            publishes reflection.result (IMO_COMPLIES or RETRIGGER)

Design:
  - Both tools take NO arguments — all state read from tool_context.state.
  - evaluate_and_publish is the real tool registered in agent.py.
  - evaluate_resolution is a backward-compat wrapper for root_agent direct calls.
  - root_agent calls evaluate_and_publish directly (not the wrapper).

Pre-action z-score source:
  state["pre_action_z_score"] — set by call_gnn_engine in ReflexAgent tools.
  Falls back to triage_result.composite_score, then latest_gnn_result.

Post-action z-score source:
  Re-runs GNN via generate_gnn_inference_event(scenario="POST_ACTION_VALIDATION").
  This calls the same GNN provider — returns low score when network is healthy.
  When real GNN is connected, this re-reads Spanner post-remediation state.
"""
import json
import logging
from datetime import datetime, timezone
from google.adk.tools import ToolContext


from app.events import (
    EVT_EXECUTION_COMPLETED,
    NETWORK_STATUS_KEY,
    consume_latest,
    publish_event,
    make_reflection_result_event,
    make_failure_notification_event,
)
from app.config.remediation_config import (
    BASELINE_Z_SCORE,
    REFLECTION_CONFIG,
)
from gnn_inference_provider import generate_gnn_inference_event


# ── Tool 1: check_execution_result ────────────────────────────────────────────

def check_execution_result(tool_context: ToolContext) -> str:
    """
    Tool 1 of 2 — NO arguments.

    Reads execution.completed event from session state.
    Parses ExecutorAgent output (TMF641 v5 + TMF921 schema).
    Stores parsed result in state["reflection_exec_result"].
    """
    state      = tool_context.state
    exec_event = consume_latest(state, EVT_EXECUTION_COMPLETED)

    if not exec_event:
        print("\n[ReflectionAgent] ERROR: No execution.completed event found")
        return "CHECK_ERROR: No execution.completed event found in state"

    # Idempotency
    if state.get("reflection_last_exec_event_id") == exec_event["event_id"]:
        return "CHECK_SKIPPED: Already processed this execution event"

    payload      = exec_event.get("payload", {})
    event_id = payload.get("eventId", "")
    success      = payload.get("success", False)
    state_str    = payload.get("state", "failed")
    error        = payload.get("error", "")
    execution_ok = (
        success and state_str == REFLECTION_CONFIG["execution_success_state"]
    )

    # Parse TMF921 intent for KPI recovery targets
    tmf921          = payload.get("tmf921_intent", {})
    expressions     = tmf921.get("expressions", [])
    target_entities = tmf921.get("target_entities", [])
    domain          = tmf921.get("domain", "UNKNOWN")
    root_cause      = tmf921.get("root_cause", "")
    priority        = tmf921.get("priority", "CRITICAL")

    # Parse TMF641 order for activation/intent IDs and action type
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

    activation_id = payload.get("activation_id", tmf921.get("activation_id", ""))
    intent_id     = payload.get("intent_id", tmf921.get("intent_id", ""))

    print("\n" + "=" * 65)
    print("[ReflectionAgent — Step 10] EXECUTION RESULT RECEIVED")
    print("=" * 65)
    print(f"  Execution Status : {'SUCCESS' if execution_ok else 'FAILED'}")
    print(f"  Success          : {success}")
    print(f"  State            : {state_str}")
    print(f"  Activation ID    : {activation_id}")
    print(f"  Intent ID        : {intent_id}")
    print(f"  Action Type      : {action_type}")
    print(f"  Domain           : {domain}")
    print(f"  Target Entities  : {target_entities}")
    if error:
        print(f"  Error            : {error}")
    print("")
    print("  KPI recovery targets (TMF921 expressions):")
    for expr in expressions:
        print(f"    • {expr.get('target_metric')}: "
              f"current={expr.get('current_value')} "
              f"→ target={expr.get('target_value')} "
              f"(±{expr.get('tolerance_pct')}%)")
    print("-" * 65)

    result = {
        "eventId":          event_id,
        "event_id":         exec_event["event_id"],
        "execution_ok":     execution_ok,
        "success":          success,
        "state":            state_str,
        "error":            error,
        "activation_id":    activation_id,
        "intent_id":        intent_id,
        "action_type":      action_type,
        "target_entities":  target_entities,
        "recovery_targets": expressions,
        "domain":           domain,
        "root_cause":       root_cause,
        "priority":         priority,
    }

    state["reflection_exec_result"]       = result
    state["reflection_last_exec_event_id"] = exec_event["event_id"]

    return (
        f"CHECK_COMPLETE: execution_ok={execution_ok} | "
        f"state={state_str} | activation_id={activation_id} | "
        f"intent_id={intent_id}"
    )


# ── Tool 2: evaluate_and_publish ──────────────────────────────────────────────

def evaluate_and_publish(tool_context: ToolContext) -> str:
    """
    Tool 2 of 2 — NO arguments.

    Slide 10 implementation:
      1. Reads execution result from state["reflection_exec_result"].
      2. Reads pre-action z-score (set by ReflexAgent call_gnn_engine).
      3. Re-runs GNN on post-action topology to get post-action z-score.
      4. Compares post_z vs BASELINE_Z_SCORE (2.0).
      5. If execution_ok AND post_z <= baseline → IMO_COMPLIES (resolved=True).
      6. Else → RETRIGGER_INVESTIGATION (resolved=False).
      7. Publishes reflection.result event.
      8. If not resolved and under max retrigger limit → re-publishes
         gnn.anomaly.detected to restart the pipeline.
    """
    state       = tool_context.state
    exec_result = state.get("reflection_exec_result", {})
    event_id = exec_result.get("eventId")

    if not exec_result:
        print("\n[ReflectionAgent] ERROR: No execution result in state")
        return "EVAL_ERROR: No execution result in state. Run check_execution_result first."

    source_id    = exec_result.get("event_id", "")
    execution_ok = exec_result.get("execution_ok", False)

    # Pre-action z-score — set by ReflexAgent call_gnn_engine
    pre_z = float(
        state.get("pre_action_z_score")
        or state.get("triage_result", {}).get("composite_score")
        or state.get("latest_gnn_result", {}).get("anomalyScore", {}).get("compositeScore")
        or state.get("latest_gnn_result", {}).get("anomalyScore", {}).get("zScore")
        or 0.0
    )

    # Post-action z-score — re-run GNN on post-remediation topology (Slide 10)
    print("[ReflectionAgent → GNN Engine] RE-RUNNING GNN ON POST-ACTION TOPOLOGY:")
    print('  "Remediation applied. Check current z-score — has baseline restored?"')
    print("-" * 65)

    post_gnn = generate_gnn_inference_event(scenario="POST_ACTION_VALIDATION")
    # FUTURE:
    # Real GNN response will return stable topology graph
    # after remediation validation from Spanner graph state.
    post_action_topology = post_gnn.get(
        "anomalousSubgraph",
        {}
    )
    
    post_z = float(
        post_gnn.get("anomalyScore", {}).get("compositeScore")
        or post_gnn.get("anomalyScore", {}).get("zScore")
        or BASELINE_Z_SCORE
    )

    print("[GNN Engine → ReflectionAgent] POST-ACTION RESPONSE:")
    print(f"  Post-Action Z-Score : {post_z}")
    print(f"  Baseline Threshold  : {BASELINE_Z_SCORE}")
    print(f"  Z-Score Resolved    : {'YES' if post_z <= BASELINE_Z_SCORE else 'NO'}")
    print("-" * 65)

    z_ok = post_z <= BASELINE_Z_SCORE   
    # ------------------------------------------------------------------
    # KPI VALIDATION (Slide 10)
    #
    # CURRENT:
    # Mock KPI restoration values used for demo/testing.
    #
    # FUTURE:
    # ReflectionAgent will query:
    #   - aw_base_hex07_anom_validation_details
    #   - PERFORMANCE.csv
    #   - Spanner validation tables
    #
    # to validate:
    #   - KPI restoration
    #   - alarm reduction
    #   - topology stabilization
    #   - eNB recovery percentage
    # ------------------------------------------------------------------
    
    recovery_targets = exec_result.get("recovery_targets", [])
    
    kpi_validation = []
    
    # FUTURE:
    # validation_df = pd.read_csv(
    #     "synthetic_data/aw_base_hex07_anom_validation_details.csv"
    # )
    #
    # latest_validation = validation_df.iloc[-1].to_dict()
    
    for expr in recovery_targets:
    
        metric = expr.get("target_metric")
        target = float(expr.get("target_value", 0))
        tolerance = float(expr.get("tolerance_pct", 0))
    
        lower_bound = target * (1 - tolerance / 100)
        upper_bound = target * (1 + tolerance / 100)
    
        # ----------------------------------------------------------
        # MOCK post-remediation KPI values
        # Replace later with Spanner / validation table values
        # ----------------------------------------------------------
    
        if metric == "dl_throughput_mbps":
            post_value = 49.5
    
        elif metric == "rrc_setup_success_rate":
            post_value = 99.1
    
        elif metric == "attach_success_rate":
            post_value = 99.2
    
        elif metric == "cpu_utilization_pct":
            post_value = 38.0
    
        elif metric == "link_utilization_pct":
            post_value = 42.0
    
        elif metric == "packet_loss_rate":
            post_value = 0.001
    
        else:
            post_value = target
    
        LOWER_IS_BETTER = {
            "cpu_utilization_pct",
            "link_utilization_pct",
            "packet_loss_rate",
        }
        
        if metric in LOWER_IS_BETTER:
            within_range = post_value <= upper_bound
        else:
            within_range = post_value >= lower_bound
    
        kpi_validation.append({
            "metric": metric,
            "post_value": post_value,
            "target": target,
            "within_tolerance": within_range,
    
            # FUTURE:
            # "major_alarm_count":
            #     latest_validation.get("noi_hr_MAJOR_count", 0),
            #
            # "critical_alarm_count":
            #     latest_validation.get("noi_hr_CATA_count", 0),
            #
            # "match_percentage":
            #     latest_validation.get("match_percentage", 0),
        })
    
    kpi_ok = all(v["within_tolerance"] for v in kpi_validation)
    
    resolved = execution_ok and z_ok and kpi_ok

    # Retrigger limit enforcement
    max_attempts = REFLECTION_CONFIG["max_retrigger_attempts"]
    attempts     = state.get("retrigger_count", 0)

    if not resolved:
        attempts += 1
        state["retrigger_count"] = attempts
        if attempts >= max_attempts:
            print(f"\n[ReflectionAgent] MAX RETRIGGER ATTEMPTS ({max_attempts}) REACHED")
            print("  Pipeline stopped. Manual intervention required.")
            state[NETWORK_STATUS_KEY] = "FAILED"
            return (
                f"FAILED_AFTER_RETRIES: attempts={attempts} | "
                f"post_z={post_z} | execution_ok={execution_ok}"
            )

    # Determine GUI/topology status labels
    domain         = exec_result.get("domain", "")
    topology_state = _pick_topology_state(domain, resolved)
    business_view  = "UTILITY_SCORE_NORMAL"      if resolved else "UTILITY_SCORE_DEGRADED"
    service_view   = "KEI_NORMAL"                if resolved else "KEI_DEGRADED"
    gui_status     = "HEALTHY_ENVIRONMENT"       if resolved else "DEGRADED_ENVIRONMENT"
    gnn_topo_view  = "STABLE_ENVIRONMENT_GRAPH"  if resolved else "UNSTABLE_ENVIRONMENT_GRAPH"

    print("\n" + "=" * 65)
    if resolved:
        print("[ReflectionAgent — Step 10] RESOLUTION: IMO_COMPLIES")
    else:
        print("[ReflectionAgent — Step 10] RESOLUTION: RETRIGGER_INVESTIGATION")
    print("=" * 65)
    print(f"  Execution OK    : {execution_ok}")
    print(f"  Pre-Action Z    : {round(pre_z, 3)}")
    print(f"  Post-Action Z   : {round(post_z, 3)}")
    print(f"  Baseline        : {BASELINE_Z_SCORE}")
    print(f"  Z Resolved      : {'YES' if z_ok else 'NO'}")
    print(f"  KPI Restored    : {'YES' if kpi_ok else 'NO'}")
    print("")
    print("  GUI Dashboard:")
    print(f"    Business View  : {business_view}")
    print(f"    Service View   : {service_view}")
    print(f"    GUI Status     : {gui_status}")
    print(f"    GNN Topology   : {gnn_topo_view}")
    print(f"    Topology State : {topology_state}")

    retrigger_reason = None

    if not resolved:
    
        if not execution_ok:
            retrigger_reason = (
                f"Execution failed: {exec_result.get('error')}"
            )
    
        elif not z_ok:
            retrigger_reason = (
                f"Post-action Z-score {post_z:.2f} "
                f"above baseline {BASELINE_Z_SCORE}"
            )
    
        elif not kpi_ok:
            failed_metrics = [
                k["metric"]
                for k in kpi_validation
                if not k["within_tolerance"]
            ]
    
            retrigger_reason = (
                "KPI recovery validation failed: "
                + ", ".join(failed_metrics)
            )
    
        print(f"    Retrigger Reason: {retrigger_reason}")
        print(f"    Attempt         : {attempts}/{max_attempts}")
    
    print("=" * 65)

    validation_output = {
        "eventId": event_id,
        "status":            "IMO_COMPLIES" if resolved else "RETRIGGER_INVESTIGATION",
        "resolved":          resolved,
        "imo_status": {
        "complies": resolved,
        "reason": (
            "Network restored to baseline"
            if resolved
            else "Topology or KPI validation failed"
        ),
    },
    
    "kpi_validation": kpi_validation,
    
    # FUTURE:
    # Real post-remediation topology returned by GNN
    "post_action_topology": post_action_topology,
    
    # FUTURE:
    # Real anomaly validation summary from validation tables
    #
    # "anomaly_validation_summary": {
    #     "major_alarm_count":
    #         latest_validation.get("noi_hr_MAJOR_count", 0),
    #
    #     "critical_alarm_count":
    #         latest_validation.get("noi_hr_CATA_count", 0),
    #
    #     "match_percentage":
    #         latest_validation.get("match_percentage", 0),
    # },
        "execution_ok":      execution_ok,
        "execution_state":   exec_result.get("state"),
        "execution_error":   exec_result.get("error", ""),
        "zscore_comparison": {
            "pre_action_z":  round(pre_z, 3),
            "post_action_z": round(post_z, 3),
            "baseline":      BASELINE_Z_SCORE,
            "z_resolved":    z_ok,
        },
        "topology_state":    topology_state,
        "business_view":     business_view,
        "service_view":      service_view,
        "gui_status":        gui_status,
        "gnn_topology_view": gnn_topo_view,
        "post_action_gnn_analysis": post_gnn,
        "activation_id":     exec_result.get("activation_id"),
        "intent_id":         exec_result.get("intent_id"),
        "domain":            domain,
        "root_cause":        exec_result.get("root_cause"),
        "target_entities":   exec_result.get("target_entities", []),
        "recovery_targets":  exec_result.get("recovery_targets", []),
        "retrigger_count":   attempts,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
    }

    if not resolved:
        validation_output["retrigger_reason"] = retrigger_reason
        validation_output["next_target"]      = "DetectiveAgent"

    # Publish reflection.result
    reflection_event = make_reflection_result_event(
        source_event_id=source_id,
        resolved=resolved,
        reflection_output=validation_output,
    )
    publish_event(state, reflection_event)
    print("\n=================================================================")
    print("[ReflectionAgent] PUBLISHED: reflection.result")
    print("=================================================================\n")
    
    print("  API CALL")
    print("  POST /reflection-result\n")
    
    reflection_payload = {
        "status": validation_output["status"],
        "eventId": event_id,
        "resolved": resolved,
        "execution_ok": execution_ok,
    
        "zscore_validation": {
            "pre_action_z": round(pre_z, 3),
            "post_action_z": round(post_z, 3),
            "baseline": BASELINE_Z_SCORE,
            "resolved": z_ok
        },
    
        "kpi_validation": kpi_validation,
    
        "gui_dashboard": {
            "business_view": business_view,
            "service_view": service_view,
            "gui_status": gui_status,
            "gnn_topology": gnn_topo_view,
            "topology_state": topology_state
        },
    
        "tmf_metadata": {
            "activation_id": exec_result.get("activation_id"),
            "intent_id": exec_result.get("intent_id"),
            "domain": domain,
            "target_entities": exec_result.get("target_entities", [])
        },
    
        "retrigger": {
            "attempt": attempts,
            "max_attempts": max_attempts,
            "reason": retrigger_reason
        },
    
        "timestamp": validation_output["timestamp"]
    }
    
    print("  REQUEST PAYLOAD")
    print(json.dumps(reflection_payload, indent=4))
    print("\n-----------------------------------------------------------------")
    
    reflection_response = {
        "status": validation_output["status"],
        "resolved": resolved,
        "network_status": (
            "RESOLVED"
            if resolved
            else "ANOMALY_DETECTED"
        ),
        "gui_status": gui_status,
        "topology_state": topology_state,
        "gnn_topology": gnn_topo_view
    }
    
    print("  API RESPONSE")
    print("  HTTP 200 OK\n")
    
    print("  RESPONSE PAYLOAD")
    print(json.dumps(reflection_response, indent=4))
    
    print("=================================================================")

    # Re-trigger pipeline if not resolved and under limit
    if not resolved:
        retrigger_payload = {
            "retriggered": True,
            "retrigger_count": attempts,
            "retrigger_reason": retrigger_reason,
            "previous_failure": {
                "domain": domain,
                "root_cause": exec_result.get("root_cause"),
                "post_z": post_z,
            },
        }
    
        publish_event(
            state,
            make_failure_notification_event(retrigger_payload)
        )
    
        print("\n=================================================================")
        print("[ReflectionAgent → ReflexAgent]")
        print("=================================================================\n")
    
        print("  API CALL")
        print("  POST /failure-notification\n")
    
        print("  REQUEST PAYLOAD")
        print(json.dumps(retrigger_payload, indent=4))
    
        print("\n-----------------------------------------------------------------")
        print("  API RESPONSE")
        print("  HTTP 202 ACCEPTED\n")
    
        retrigger_response = {
            "status": "RETRIGGERED",
            "next_agent": "ReflexAgent",
            "attempt": attempts,
            "max_attempts": max_attempts,
            "reason": retrigger_reason,
            "network_status": "ANOMALY_DETECTED"
        }
    
        print("  RESPONSE PAYLOAD")
        print(json.dumps(retrigger_response, indent=4))
    
        print("=================================================================")
    state["reflection_output"]    = validation_output
    state[NETWORK_STATUS_KEY]     = "RESOLVED" if resolved else "ANOMALY_DETECTED"

    return (
        f"{'RESOLVED' if resolved else 'RETRIGGER'}: "
        f"status={validation_output['status']} | "
        f"pre_z={round(pre_z, 3)} | post_z={round(post_z, 3)} | "
        f"baseline={BASELINE_Z_SCORE} | gui={gui_status}"
    )


# ── Topology state helper ──────────────────────────────────────────────────────

def _pick_topology_state(domain: str, resolved: bool) -> str:
    if not resolved:
        return "UNSTABLE"
    return {
        "CORE":      "STABLE_GRAPH_CORE_V2",
        "TRANSPORT": "STABLE_GRAPH_TRANSPORT_V2",
    }.get(domain, "STABLE_GRAPH_V2")


# ── Backward-compat wrapper for root_agent direct calls ───────────────────────

def evaluate_resolution(tool_context: ToolContext, exec_result: dict = None) -> dict:
    """
    Backward compatibility wrapper.
    root_agent calls this directly (not via LlmAgent tool path).
    Delegates to evaluate_and_publish — exec_result arg is ignored
    because evaluate_and_publish reads from state directly.
    """
    evaluate_and_publish(tool_context)
    output = tool_context.state.get("reflection_output", {})
    return {
        "status":         output.get("status", "UNKNOWN"),
        "resolved":       output.get("resolved", False),
        "gui_status":     output.get("gui_status"),
        "network_status": tool_context.state.get(NETWORK_STATUS_KEY),
    }