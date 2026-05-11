import logging
import uuid
from datetime import datetime, timezone


def prompt_gnn_engine(gnn_prompt: dict) -> dict:
    logging.info(
        f"[GNN] Analysing Spanner graph"
        f" | trigger={gnn_prompt.get('trigger')}"
        f" | use_case={gnn_prompt.get('use_case_id')}"
        f" | entities={gnn_prompt.get('all_affected_entities', gnn_prompt.get('affected_enodebs', []))}"
    )

    composite_score = 9.4
    impact_score    = round(min(composite_score / 10.0, 1.0), 3)

    affected_enodebs = gnn_prompt.get("affected_enodebs", [])
    core_elements    = gnn_prompt.get("core_elements", [])
    all_entities     = affected_enodebs + core_elements

    nodes = all_entities if all_entities else []
    edges = [[nodes[i], nodes[i + 1]] for i in range(len(nodes) - 1)] if len(nodes) > 1 else []
    ranked_list = [{"rank": i + 1, "node_id": nid} for i, nid in enumerate(nodes)]

    return {
        "anomalousSubgraph": {
            "nodes": nodes,
            "edges": edges,
        },
        "rankedList": ranked_list,
        "impact_score": impact_score,
        "criticality_score": 1.0,
        "criticality_label": "CRITICAL",
    }


def generate_gnn_inference_event(scenario: str = "UC_MULTI_DOMAIN_HEALING") -> dict:
    if scenario == "POST_ACTION_VALIDATION":
        return {
            "anomalousSubgraph": {"nodes": [], "edges": []},
            "rankedList":  [],
            "impact_score":      0.12,
            "criticality_score": "NORMAL",
            "anomalyScore": {"compositeScore": 1.2, "zScore": 1.2, "confidence": 0.92},
        }

    return prompt_gnn_engine({"request_id": str(uuid.uuid4()), "trigger": "internal"})
