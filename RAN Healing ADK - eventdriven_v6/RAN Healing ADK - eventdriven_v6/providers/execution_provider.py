"""
providers/execution_provider.py
=================================
Mock for Executor Agent (Ericsson EIC) — Slide Step 8/9.

THIS IS A MOCK. Replace the body of generate_execution_output() when the
real Ericsson EIC/ENM execution endpoint is available:

    import httpx
    resp = httpx.post(
        os.environ["EXECUTION_URL"] + "/execute",
        json=engineer_payload.get("tmf921_intent", {}),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()

Slide 9: Automation Engine updates to Spanner (9a RAN, 9b CORE, 9c TRANSPORT)
  When real API available: this mock should POST to EIC which then
  updates Spanner ACTIVATIONS and CHANGEREQUEST tables.
"""

from datetime import datetime, timezone


def generate_execution_output(engineer_payload: dict) -> dict:
    """
    Takes engineer.ready payload from EngineerAgent.
    Returns execution.completed payload (TMF641 v5 schema).

    All values derived from engineer_payload — no hardcoded EIDs.

    REPLACE WITH REAL API CALL when EIC endpoint available.
    """
    tmf921          = engineer_payload.get("tmf921_intent", {})
    intent_id       = tmf921.get("intent_id", "INT-UNKNOWN")
    activation_id   = tmf921.get("activation_id", "ACT-UNKNOWN")
    target_entities = tmf921.get("target_entities", [])
    root_cause      = tmf921.get("root_cause", "")
    root_cause_map  = tmf921.get("root_cause_mapped", root_cause)
    domain          = tmf921.get("domain", "UNKNOWN")
    expressions     = tmf921.get("expressions", [])
    cr_id           = tmf921.get("change_request_id", "")
    priority        = tmf921.get("priority", "CRITICAL")

    # Primary action: from sequence=1 branch (highest utility score)
    primary_action_cmd = tmf921.get("primary_action_command", {})
    action_type        = primary_action_cmd.get("type", "UNKNOWN_ACTION")

    # Branch summary for order items
    healing_branches = tmf921.get("healing_branches", [])
    branch_desc = ", ".join(
        f"{b.get('branch_id')}[{b.get('domain')}]:{b.get('action')}"
        for b in healing_branches
    )

    # Target entities string for TMF641 characteristics
    target_entities_str = ",".join(target_entities) if target_entities else ""
    n_targets           = len(target_entities)

    now = datetime.now(timezone.utc).isoformat()

    # Slide 9: domain-specific action_type for automation engine
    _domain_action_map = {
        "RAN":         "RAN_PARAM_ROLLBACK",
        "CORE":        "HSS_SESSION_CLEAR",
        "TRANSPORT":   "BACKHAUL_REROUTE",
        "CROSS_DOMAIN": "MULTI_DOMAIN_SEQUENCE",
    }
    tmf641_action = _domain_action_map.get(domain, action_type)

    return {
        # Primary status fields — ReflectionAgent reads both
        "eventId": engineer_payload.get("eventId"),
        "success":        True,
        "state":          "completed",
        "error":          "",
        "activation_id":  activation_id,
        "intent_id":      intent_id,
        "timestamp":      now,
        "order_type":     "ServiceOrder",
        "description": (
            f"{tmf641_action} on {n_targets} {domain} entities — "
            f"root_cause: {root_cause} — intent: {intent_id}"
        ),

        # TMF641 v5 service order — Slide 8 schema
        "tmf641_order": {
            "@type":       "ServiceOrder",
            "external_id": activation_id,
            "category":    "autonomous_healing",
            "description": (
                f"{tmf641_action} on {n_targets} entities"
            ),
            "priority": "1",
            "state":    "acknowledged",
            "intent_id": intent_id,
            "order_items": [
                {
                    "id":     "1",
                    "action": "modify",
                    "service": {
                        "id":   tmf641_action,
                        "name": f"{domain} Healing — {tmf641_action}",
                        "service_characteristics": [
                            # action_type — ReflectionAgent reads this
                            {"name": "action_type",           "value": tmf641_action},
                            # target_entities — ReflectionAgent reads this
                            {"name": "target_entities",       "value": target_entities_str},
                            {"name": "parameter_name",        "value": primary_action_cmd.get("description", "")},
                            {"name": "risk_level",            "value": "LOW"},
                            {"name": "reversible",            "value": "true"},
                            {"name": "estimated_ttr_minutes", "value": str(
                                tmf921.get("constraints", {}).get("max_impact_duration_minutes", 30)
                            )},
                            {"name": "domain",                "value": domain},
                            {"name": "branches",              "value": branch_desc},
                        ],
                        "related_entities": [
                            e for e in [
                                {"@referredType": "ChangeRequest",
                                 "id": cr_id, "role": "root_cause"} if cr_id else None,
                                {"@referredType": "NetworkEntity",
                                 "id": target_entities[0],
                                 "role": "primary_target"} if target_entities else None,
                            ] if e is not None
                        ],
                    },
                }
            ],
            "related_parties": [
                {"role": "requester",     "name": "OriginIDAgent"},
                {"role": "executor",      "name": "Ericsson_EIC"},
                {"role": "investigation", "name": "DetectiveAgent"},
                {"role": "planning",      "name": "EngineerAgent"},
            ],
            "notes": [
                {"author": "DetectiveAgent",
                 "text":   f"RCD confirmed: {root_cause}"},
                {"author": "EngineerAgent",
                 "text":   f"TMF921 Intent: Remediate {root_cause} — {n_targets} entities"},
                {"author": "ExecutorAgent",
                 "text":   f"Executing {tmf641_action}"},
                # Slide 9: note Spanner update (stub — real API updates Spanner)
                {"author": "AutomationEngine",
                 "text":   f"Slide 9: {domain} config changes applied (Spanner update pending real API)"},
            ],
        },

        # TMF921 intent echoed back — ReflectionAgent reads expressions for KPI targets
        "tmf921_intent": {
            "intent_id":         intent_id,
            "intent_type":       "remediation",
            "description":       tmf921.get("description", ""),
            "root_cause":        root_cause,
            "root_cause_entity": tmf921.get("root_cause_entity", ""),
            "domain":            domain,
            "priority":          priority,
            "target_entities":   target_entities,
            # expressions = KPI recovery targets from EngineerAgent
            # ReflectionAgent check_execution_result reads these
            "expressions": expressions if expressions else [],
            "constraints":       tmf921.get("constraints", {}),
        },

        "response": {
            "id":             f"SO-{activation_id}",
            "state":          "acknowledged",
            "orderDate":      now,
            "completionDate": None,
            "note": [
                {"text": f"Mock execution of {tmf641_action}"},
                {"text": f"TMF921 intent {intent_id} acknowledged"},
                {"text": f"Targets: {target_entities_str}"},
            ],
        },
    }