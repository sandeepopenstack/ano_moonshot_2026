"""
gnn_inference_provider.py
===========================
GNN Correlation Engine — Step 4b ONLY.

=================================================================
API: POST /analyze-anomaly
     ReflexAgent → GNN Inference Engine  (Step 4a input)
=================================================================
=================================================================
HOW TO CONNECT REAL GNN API — replace prompt_gnn_engine() body:
=================================================================
    import httpx
    response = httpx.post(
        os.environ["GNN_INFERENCE_URL"] + "/analyze-anomaly",
        json=gnn_prompt,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()
    # Real GNN returns same schema:
    # anomalousSubgraph, rankedList (with full node details),
    # impact_score, criticality_score
"""

import logging
import uuid
from datetime import datetime, timezone


# ── Step 4b: GNN Inference Engine → ReflexAgent ───────────────────────────────

def prompt_gnn_engine(gnn_prompt: dict) -> dict:
    """
    Step 4b — GNN Inference Engine response to ReflexAgent.

    IN:  tools call JSON payload from ReflexAgent (Step 4a)
    OUT: anomalousSubgraph + rankedList + impact_score + criticality_score

    GNN computes everything internally (Spanner graph traversal, z-score scoring,
    anomalyImpactRanking formula). Only the final 4 outputs are returned.

    rankedList contains only "rank" as placeholder — full details provided
    by GNN team when real API connects.
    """
    logging.info(
        f"[GNN] Analysing Spanner graph"
        f" | trigger={gnn_prompt.get('trigger')}"
        f" | use_case={gnn_prompt.get('use_case_id')}"
        f" | entities={gnn_prompt.get('all_affected_entities', gnn_prompt.get('affected_enodebs', []))}"
    )

    # ── GNN internal calculation (not exposed in output) ──────────────────
    composite_score = 9.4
    impact_score    = round(min(composite_score / 10.0, 1.0), 3)  # 0.94

    # Real synth EIDs from 2b payload (passed via 4a prompt)
    # affected_enodebs for RAN, core_elements for CORE
    affected_enodebs = gnn_prompt.get("affected_enodebs", [])
    core_elements    = gnn_prompt.get("core_elements", [])
    all_entities     = affected_enodebs + core_elements

    # anomalousSubgraph.nodes = real EIDs consistent with 2b → 4a → 4b → Step 5
    # GNN traverses Spanner graph using these EIDs as starting points
    nodes = all_entities if all_entities else []

    # edges: GNN graph traversal result — connections between affected nodes
    edges = [[nodes[i], nodes[i + 1]] for i in range(len(nodes) - 1)] if len(nodes) > 1 else []

    # rankedList: GNN ranks by business impact (GNN_score × sub × rev × ToD × app)
    # node_id = same real EIDs as anomalousSubgraph.nodes
    # Full scoring details provided by GNN team when real API connects
    ranked_list = [{"rank": i + 1, "node_id": nid} for i, nid in enumerate(nodes)]

    # ── Output: only the 4 required fields ───────────────────────────────
    return {

        # (1) Anomalous subgraph — nodes + edges from Spanner graph traversal
        # nodes and edges use real synth EIDs consistent with 2b payload
        "anomalousSubgraph": {
            "nodes": nodes,
            "edges": edges,
        },

        # (2) Ranked list — GNN ranks nodes by business impact (highest first)
        # node_id matches anomalousSubgraph.nodes (real synth EIDs from 2b)
        "rankedList": ranked_list,

        # (3) Overall impact_score — from INSIGHT.csv z_impact/10 (Stage 12)
        # Float 0-1 — used by EngineerAgent as impact_weight in utility formula
        "impact_score": impact_score,

        # (4) Overall criticality_score — from INSIGHT.csv anomaly_label (Stage 3)
        # String CRITICAL/MAJOR/MINOR — display/routing only, NOT in utility formula
        "criticality_score": 1.0,
        "criticality_label": "CRITICAL",
    }


# ── Post-action validation (ReflectionAgent uses after execution) ──────────────

def generate_gnn_inference_event(scenario: str = "UC_MULTI_DOMAIN_HEALING") -> dict:
    """
    Post-action GNN check for ReflectionAgent (Step 10).
    Returns low scores confirming network recovered (composite < 2.0 baseline).
    """
    if scenario == "POST_ACTION_VALIDATION":
        return {
            "anomalousSubgraph": {"nodes": [], "edges": []},
            "rankedList":  [],
            "impact_score":      0.12,
            "criticality_score": "NORMAL",
            # Internal: compositeScore=1.2 < BASELINE_Z_SCORE(2.0) → resolved
            "anomalyScore": {"compositeScore": 1.2, "zScore": 1.2, "confidence": 0.92},
        }

    return prompt_gnn_engine({"request_id": str(uuid.uuid4()), "trigger": "internal"})