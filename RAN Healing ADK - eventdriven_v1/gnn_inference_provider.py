"""
GNN Inference Provider — Stage 4 (PPT)
=======================================
Autonomous anomaly injection — randomly selects UC1 or UC2.
"""

from datetime import datetime, timezone
import uuid
import random

from app.events import make_gnn_anomaly_event


def generate_gnn_inference_event(scenario: str = None) -> dict:
    """
    Autonomously produces a GNN inference event.
    If scenario is None, randomly picks UC1 or UC2 to simulate
    real-world unpredictable anomaly injection.
    """
    correlation_id = str(uuid.uuid4())

    # Auto-select scenario if not specified
    if scenario is None:
        scenario = random.choice([
            "UC_MULTI_DOMAIN_HEALING",
            "UC2_CORE_CONGESTION",
        ])
        print(f"[GNN] Auto-selected scenario: {scenario}")

    if scenario == "UC_MULTI_DOMAIN_HEALING":
        return {
            "eventId": correlation_id,
            "eventType": "gnnInference.anomalyDetected",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "sourceSystem": "GNN_CORRELATION_ENGINE",
            "probableDomain": "CROSS_DOMAIN",
            "businessPriority": "CRITICAL",
            "anomalyScore": {
                "zScore": 9.4,
                "confidence": 0.97,
            },
            "anomalousSubgraph": {
                "nodes": ["RAN_CELL_101", "HSS_CORE_01", "TRANSPORT_LINK_01"],
                "edges": [
                    ("RAN_CELL_101", "TRANSPORT_LINK_01"),
                    ("TRANSPORT_LINK_01", "HSS_CORE_01"),
                ],
            },
            "rankedRemediationBranches": [
                {"action_id": "A", "domain": "RAN",       "priority_score": 10, "recommended_fix": "ROLLBACK_TILT"},
                {"action_id": "B", "domain": "CORE",      "priority_score": 7,  "recommended_fix": "CLEAR_STALE_HSS_SESSIONS"},
                {"action_id": "C", "domain": "TRANSPORT", "priority_score": 4,  "recommended_fix": "RESET_TRANSPORT_PATH"},
            ],
            "routingHint": {
                "targetAgent": "MonitoringAgent",
                "priorityOrder": "A_THEN_B_THEN_C",
            },
        }

    if scenario == "UC2_CORE_CONGESTION":
        return {
            "eventId": correlation_id,
            "eventType": "gnnInference.anomalyDetected",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "sourceSystem": "GNN_CORRELATION_ENGINE",
            "probableDomain": "CORE",
            "businessPriority": "CRITICAL",
            "anomalyScore": {"zScore": 8.7, "confidence": 0.95},
            "anomalousSubgraph": {
                "nodes": ["HSS_CORE_01", "HSS_CORE_02"],
                "edges": [("HSS_CORE_01", "HSS_CORE_02")],
            },
            "rankedRemediationBranches": [
                {"action_id": "A", "domain": "CORE", "priority_score": 10,
                 "recommended_fix": "CLEAR_STALE_HSS_SESSIONS"},
            ],
            "routingHint": {"targetAgent": "MonitoringAgent", "priorityOrder": "A"},
        }

    # Default: single-domain RAN
    return {
        "eventId": correlation_id,
        "eventType": "gnnInference.anomalyDetected",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "sourceSystem": "GNN_CORRELATION_ENGINE",
        "probableDomain": "RAN",
        "businessPriority": "MEDIUM",
        "anomalyScore": {"zScore": 4.0, "confidence": 0.85},
        "anomalousSubgraph": {"nodes": ["RAN_CELL_101"], "edges": []},
        "rankedRemediationBranches": [
            {"action_id": "A", "domain": "RAN", "priority_score": 5,
             "recommended_fix": "ROLLBACK_TILT"},
        ],
        "routingHint": {"targetAgent": "MonitoringAgent", "priorityOrder": "A"},
    }


def generate_gnn_anomaly_event_wrapper(scenario: str = None) -> dict:
    gnn_payload = generate_gnn_inference_event(scenario)
    return make_gnn_anomaly_event(gnn_payload)