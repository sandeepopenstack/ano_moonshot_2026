"""
GNN Inference Provider — Stage 4 (PPT)
=======================================
Mock of the real GNN Correlation Engine inference output.
Structure is identical to what a live Vertex AI / Spanner Graph
inference endpoint would return, so swapping in the real call
requires only changing fetch_gnn_inference() in monitoring mock_api.py.

PPT Stage 4 contract:
  • Anomalous subgraph  : affected nodes + edges
  • Anomaly score       : z-score × subscribers × revenue × time-of-day × app type
  • Ranked list         : highest business impact first
"""

from datetime import datetime, timezone
import uuid
from app.events import make_gnn_anomaly_event


def generate_gnn_inference_event(
    scenario: str = "UC_MULTI_DOMAIN_HEALING",
) -> dict:
    """
    Produce a TM-Forum-aligned GNN inference event.

    Scenarios
    ---------
    UC_MULTI_DOMAIN_HEALING : cross-domain P1 (RAN + CORE + TRANSPORT)
    default                 : single-domain RAN P2
    """
    correlation_id = str(uuid.uuid4())

    if scenario == "UC_MULTI_DOMAIN_HEALING":
        return {
            "eventId": correlation_id,
            "eventType": "gnnInference.anomalyDetected",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "sourceSystem": "GNN_CORRELATION_ENGINE",
            "probableDomain": "CROSS_DOMAIN",
            "businessPriority": "CRITICAL",
            "anomalyScore": {
                "zScore": 9.4,        # composite: GNN × subscribers × revenue × ToD × app
                "confidence": 0.97,
            },
            "anomalousSubgraph": {
                "nodes": ["RAN_CELL_101", "HSS_CORE_01", "TRANSPORT_LINK_01"],
                "edges": [
                    ("RAN_CELL_101", "TRANSPORT_LINK_01"),
                    ("TRANSPORT_LINK_01", "HSS_CORE_01"),
                ],
            },
            # Ranked highest business-impact first — PPT Stage 4 requirement
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

    # Default: single-domain RAN scenario
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
            {"action_id": "A", "domain": "RAN", "priority_score": 5, "recommended_fix": "ROLLBACK_TILT"},
        ],
        "routingHint": {"targetAgent": "MonitoringAgent", "priorityOrder": "A"},
    }

def generate_gnn_anomaly_event_wrapper(
    scenario: str = "UC_MULTI_DOMAIN_HEALING",
) -> dict:
    """
    Stage 4 → Event Producer

    Wraps raw GNN inference into a proper domain event:
        EVT_GNN_ANOMALY_DETECTED

    This is what MonitoringAgent subscribes to.
    """

    gnn_payload = generate_gnn_inference_event(scenario)

    return make_gnn_anomaly_event(gnn_payload)