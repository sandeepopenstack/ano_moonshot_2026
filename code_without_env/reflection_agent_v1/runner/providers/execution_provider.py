"""Mock ExecutorAgent provider aligned to the Step 8/9 output payload.

The real Executor service owns execution. This mock is only for local tests and
returns the execution.completed schema that ReflectionAgent consumes.
"""

from datetime import datetime, timezone


def _first_present(*values):
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def generate_execution_output(engineer_payload: dict) -> dict:
    """Build an execution.completed payload from EngineerAgent output."""
    tmf921 = engineer_payload.get("tmf921_intent", {})

    intent_id = _first_present(
        engineer_payload.get("intent_id"),
        tmf921.get("intent_id"),
        "INTENT-UNKNOWN",
    )
    activation_id = _first_present(tmf921.get("activation_id"), "ACT-UNKNOWN")
    change_request_id = _first_present(
        engineer_payload.get("change_request_id"),
        tmf921.get("change_request_id"),
        "",
    )
    hypothesis_id = _first_present(
        engineer_payload.get("hypothesis_id"),
        tmf921.get("hypothesis_id"),
        "",
    )
    target_entities = _first_present(
        tmf921.get("target_entities"),
        engineer_payload.get("affected_entities"),
        [],
    )
    affected_hex_bins = _first_present(
        engineer_payload.get("affected_hex_bins"),
        tmf921.get("affected_hex_bins"),
        [],
    )
    ranked_branches = _first_present(
        tmf921.get("ranked_healing_branches"),
        engineer_payload.get("ranked_healing_plan"),
        engineer_payload.get("ranked_branches"),
        [],
    )
    primary_branch = ranked_branches[0] if ranked_branches else {}

    root_cause = tmf921.get("root_cause", engineer_payload.get("root_cause", ""))
    domain = tmf921.get("domain", engineer_payload.get("domain", "UNKNOWN"))
    priority = tmf921.get("priority", engineer_payload.get("priority", "CRITICAL"))
    expressions = tmf921.get("expressions", [])

    domain_action_map = {
        "RAN": "RAN_PARAM_ROLLBACK",
        "CORE": "MZ_SESSION_CLEAR",
        "TRANSPORT": "BACKHAUL_REROUTE",
        "CROSS_DOMAIN": "MULTI_DOMAIN_SEQUENCE",
    }
    action_type = domain_action_map.get(
        domain,
        primary_branch.get("action", "UNKNOWN_ACTION"),
    )
    action_description = _first_present(
        primary_branch.get("description"),
        primary_branch.get("action_detail"),
        primary_branch.get("action"),
        action_type,
    )
    estimated_ttr = _first_present(
        tmf921.get("constraints", {}).get("estimated_ttr_minutes"),
        tmf921.get("constraints", {}).get("max_impact_duration_minutes"),
        30,
    )

    target_entities_str = ",".join(target_entities)
    affected_hex_bins_str = ",".join(affected_hex_bins)
    branch_desc = ", ".join(
        f"{b.get('sequence')}[{b.get('domain')}]:{b.get('action')}"
        for b in ranked_branches
    )
    now = datetime.now(timezone.utc).isoformat()

    return {
        "eventId": engineer_payload.get("eventId"),
        "success": True,
        "state": "completed",
        "error": "",
        "activation_id": activation_id,
        "intent_id": intent_id,
        "change_request_id": change_request_id,
        "hypothesis_id": hypothesis_id,
        "affected_hex_bins": affected_hex_bins,
        "timestamp": now,
        "order_type": "ServiceOrder",
        "description": (
            f"{action_type} on {len(target_entities)} {domain} entities - "
            f"root_cause: {root_cause} - intent: {intent_id}"
        ),
        "tmf641_order": {
            "@type": "ServiceOrder",
            "external_id": activation_id,
            "category": "autonomous_healing",
            "description": f"{action_type} on {len(target_entities)} entities",
            "priority": "1",
            "state": "acknowledged",
            "intent_id": intent_id,
            "order_items": [
                {
                    "id": "1",
                    "action": "modify",
                    "service": {
                        "id": action_type,
                        "name": f"{domain} Healing - {action_type}",
                        "service_characteristics": [
                            {"name": "action_type", "value": action_type},
                            {"name": "target_entities", "value": target_entities_str},
                            {"name": "affected_hex_bins", "value": affected_hex_bins_str},
                            {"name": "parameter_name", "value": action_description},
                            {"name": "estimated_ttr_minutes", "value": str(estimated_ttr)},
                            {"name": "domain", "value": domain},
                            {"name": "branches", "value": branch_desc},
                        ],
                        "related_entities": [
                            item for item in [
                                {
                                    "@referredType": "ChangeRequest",
                                    "id": change_request_id,
                                    "role": "root_cause",
                                } if change_request_id else None,
                                {
                                    "@referredType": "NetworkEntity",
                                    "id": target_entities[0],
                                    "role": "primary_target",
                                } if target_entities else None,
                            ] if item is not None
                        ],
                    },
                }
            ],
            "related_parties": [
                {"role": "requester", "name": "OriginIDAgent"},
                {"role": "executor", "name": "Ericsson_EIC"},
                {"role": "investigation", "name": "DetectiveAgent"},
                {"role": "planning", "name": "EngineerAgent"},
            ],
            "notes": [
                {"author": "DetectiveAgent", "text": f"RCD confirmed: {root_cause}"},
                {"author": "EngineerAgent", "text": f"TMF921 Intent: {intent_id}"},
                {"author": "ExecutorAgent", "text": f"Executing {action_type}"},
            ],
        },
        "tmf921_intent": {
            "intent_id": intent_id,
            "intent_type": tmf921.get("intent_type", "remediation"),
            "description": tmf921.get("description", ""),
            "root_cause": root_cause,
            "root_cause_entity": tmf921.get("root_cause_entity", ""),
            "domain": domain,
            "priority": priority,
            "target_entities": target_entities,
            "affected_hex_bins": affected_hex_bins,
            "expressions": expressions,
            "constraints": tmf921.get("constraints", {}),
            "change_request_id": change_request_id,
            "hypothesis_id": hypothesis_id,
        },
        "response": {
            "id": f"SO-{activation_id}",
            "state": "acknowledged",
            "orderDate": now,
            "completionDate": now,
            "note": [
                {"text": f"Mock execution of {action_type}"},
                {"text": f"TMF921 intent {intent_id} acknowledged"},
                {"text": f"Targets: {target_entities_str}"},
            ],
        },
    }
