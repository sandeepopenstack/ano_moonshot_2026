import os
import uuid
import logging
import requests
from datetime import datetime, timezone
from google.adk.tools import ToolContext

from ran_healing_shared.events import (
    EVT_FAILURE_NOTIFICATION,
    EVT_REFLEX_TRIAGE_READY,
    NETWORK_STATUS_KEY,
    publish_event,
    latest_key,
)
from ran_healing_shared.remediation_config import (
    infer_domain,
    get_priority_from_gnn,
)
from ran_healing_shared.gnn_inference_provider import prompt_gnn_engine


_BRANCH_PRIORITY_LABEL = {0: "HIGH", 1: "MEDIUM", 2: "LOW"}

_GCP_PROJECT      = os.environ.get("GOOGLE_CLOUD_PROJECT", "poc-z-in2300756")
_SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "verizon-gnn")
_SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "syndata")
_TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://localhost:5000").rstrip("/")


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
                    "clientInfo": {"name": "reflex-agent", "version": "1.0"},
                },
            },
            timeout=3,
        )
        if 200 <= resp.status_code < 300:
            logging.info(f"[MCP] Toolbox running at {_TOOLBOX_URL}/mcp (status {resp.status_code})")
            return True
    except requests.exceptions.ConnectionError:
        return False
    except Exception as e:
        logging.debug(f"[MCP] Health check /mcp failed: {e}")

    return False


def _invoke_tool(tool_name: str, params: dict) -> list[dict]:
    import uuid
    url = f"{_TOOLBOX_URL}/mcp"
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
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
            timeout=15,
        )
        if resp.status_code != 200:
            logging.warning(f"[MCP] {tool_name} returned HTTP {resp.status_code}: {resp.text[:200]}")
            return []

        body = resp.json()

        if "error" in body:
            logging.warning(f"[MCP] {tool_name} RPC error: {body['error']}")
            return []

        result  = body.get("result", {})
        content = result.get("content", [])

        rows: list[dict] = []
        for block in content:
            if block.get("type") == "text":
                import json as _json
                try:
                    parsed = _json.loads(block["text"])
                    if isinstance(parsed, list):
                        rows.extend(parsed)
                    elif isinstance(parsed, dict):
                        rows.append(parsed)
                except _json.JSONDecodeError:
                    logging.warning(f"[MCP] {tool_name} non-JSON text block: {block['text'][:100]}")

        logging.info(f"[MCP] {tool_name} \u2192 {len(rows)} rows")
        return rows

    except requests.exceptions.ConnectionError:
        logging.warning(f"[MCP] {tool_name}: connection refused \u2014 toolbox stopped?")
        return []
    except requests.exceptions.Timeout:
        logging.warning(f"[MCP] {tool_name}: timeout after 15s")
        return []
    except Exception as e:
        logging.warning(f"[MCP] {tool_name} failed: {e}")
        return []


def _query_via_mcp_toolbox(eids: list[str]) -> dict:
    entity_details  = {}
    connected_nodes = []
    neighbor_cells  = []
    domains         = set()
    eid_set         = set(eids)

    rows1 = _invoke_tool("query_entity_domains", {"eids": eids})
    for row in rows1:
        eid = row.get("eid", "")
        dom = row.get("network_domain", "UNKNOWN")
        if not eid:
            continue
        entity_details[eid] = {
            "eid":             eid,
            "entity_type":     row.get("entity_type", "UNKNOWN"),
            "network_domain":  dom,
            "network_segment": row.get("network_segment", "UNKNOWN"),
        }
        if dom and dom != "UNKNOWN":
            domains.add(dom)

    rows2 = _invoke_tool("query_entity_connections", {"eids": eids})
    for row in rows2:
        target = row.get("target_eid", "")
        if target and target not in eid_set:
            connected_nodes.append({
                "source_eid":    row.get("source_eid", ""),
                "target_eid":    target,
                "edge_type":     row.get("edge_type", "connected"),
                "target_domain": row.get("network_domain", "UNKNOWN"),
            })

    rows3 = _invoke_tool("query_neighbor_cells", {"eids": eids})
    for row in rows3:
        src = row.get("source_eid", "")
        nbr = row.get("neighbor_eid", "")
        if src and nbr:
            neighbor_cells.append({"source_eid": src, "neighbor_eid": nbr})

    return {
        "source":           "mcp_toolbox",
        "tables_queried":   ["entity", "edge_entitytoentity", "edge_entitytoneighbor"],
        "query_eids":       eids,
        "entity_details":   entity_details,
        "connected_nodes":  connected_nodes,
        "neighbor_cells":   neighbor_cells,
        "impact_radius":    len(eids) + len(connected_nodes),
        "affected_domains": sorted(domains),
    }


def _query_spanner_direct(eids: list[str]) -> dict:
    from google.cloud import spanner as spanner_lib

    client   = spanner_lib.Client(project=_GCP_PROJECT)
    instance = client.instance(_SPANNER_INSTANCE)
    db       = instance.database(_SPANNER_DATABASE)
    arr_type = spanner_lib.param_types.Array(spanner_lib.param_types.STRING)

    entity_details  = {}
    connected_nodes = []
    neighbor_cells  = []
    domains         = set()
    eid_set         = set(eids)

    with db.snapshot() as snap:
        for row in snap.execute_sql(
            """SELECT string_field_0, string_field_1, string_field_2, string_field_3
               FROM entity WHERE string_field_0 IN UNNEST(@eids)""",
            params={"eids": eids},
            param_types={"eids": arr_type},
        ):
            eid_v, etype, dom, seg = row
            entity_details[eid_v] = {
                "eid": eid_v, "entity_type": etype or "UNKNOWN",
                "network_domain": dom or "UNKNOWN",
                "network_segment": seg or "UNKNOWN",
            }
            if dom and dom != "UNKNOWN":
                domains.add(dom)

    with db.snapshot() as snap:
        for row in snap.execute_sql(
            """SELECT eid, to_eid, edge_type, network_domain
               FROM edge_entitytoentity WHERE eid IN UNNEST(@eids)""",
            params={"eids": eids},
            param_types={"eids": arr_type},
        ):
            src, tgt, etype, dom = row
            if tgt and tgt not in eid_set:
                connected_nodes.append({
                    "source_eid": src, "target_eid": tgt,
                    "edge_type": etype or "connected",
                    "target_domain": dom or "UNKNOWN",
                })

    with db.snapshot() as snap:
        for row in snap.execute_sql(
            """SELECT eid, to_eid FROM edge_entitytoneighbor
               WHERE eid IN UNNEST(@eids)""",
            params={"eids": eids},
            param_types={"eids": arr_type},
        ):
            src, nbr = row
            if src and nbr:
                neighbor_cells.append({"source_eid": src, "neighbor_eid": nbr})

    return {
        "source":           "spanner_direct",
        "tables_queried":   ["entity", "edge_entitytoentity", "edge_entitytoneighbor"],
        "query_eids":       eids,
        "entity_details":   entity_details,
        "connected_nodes":  connected_nodes,
        "neighbor_cells":   neighbor_cells,
        "impact_radius":    len(eids) + len(connected_nodes),
        "affected_domains": sorted(domains),
    }


def _query_spanner_mock(eids: list[str]) -> dict:
    def _classify(eid: str) -> tuple[str, str, str]:
        u = eid.upper()
        if u.startswith("ENB") or "ENODEB" in u or "RAN_CELL" in u:
            return "RAN", "eNodeB", "LTE"
        if u.startswith("GNB") or "GNODEB" in u:
            return "RAN", "gNodeB", "NR"
        if "CELL" in u and "CORE" not in u:
            return "RAN", "eNodeB", "LTE"
        if "HSS" in u:
            return "CORE", "HSS", "EPC"
        if "MME" in u:
            return "CORE", "MME", "EPC"
        if "AMF" in u:
            return "CORE", "AMF", "5GC"
        if "UPF" in u:
            return "CORE", "UPF", "5GC"
        if "SMF" in u:
            return "CORE", "SMF", "5GC"
        if "CSR" in u or "TRANSPORT_LINK" in u:
            return "TRANSPORT", "CSR", "IP_BACKHAUL"
        if "AGG" in u or "AGG_NODE" in u:
            return "TRANSPORT", "AGG", "IP_BACKHAUL"
        return "UNKNOWN", "UNKNOWN", "UNKNOWN"

    entity_details = {}
    domains        = set()
    for eid in eids:
        dom, etype, seg = _classify(eid)
        entity_details[eid] = {
            "eid": eid, "entity_type": etype,
            "network_domain": dom, "network_segment": seg,
        }
        if dom != "UNKNOWN":
            domains.add(dom)

    return {
        "source":           "spanner_mock",
        "tables_queried":   ["entity", "edge_entitytoentity", "edge_entitytoneighbor"],
        "query_eids":       eids,
        "entity_details":   entity_details,
        "connected_nodes":  [],
        "neighbor_cells":   [],
        "impact_radius":    len(eids),
        "affected_domains": sorted(domains),
    }


def _query_spanner_impact_radius(eids: list[str]) -> dict:
    if _is_toolbox_running():
        try:
            logging.info(f"[ReflexAgent] MCP Toolbox at {_TOOLBOX_URL}")
            result = _query_via_mcp_toolbox(eids)
            logging.info(f"[ReflexAgent] MCP OK: {len(result['entity_details'])} entities | domains={result['affected_domains']}")
            return result
        except Exception as e:
            logging.warning(f"[ReflexAgent] MCP Toolbox call failed: {e} \u2192 Spanner direct")
    else:
        logging.info(f"[ReflexAgent] MCP Toolbox not running at {_TOOLBOX_URL} \u2192 trying Spanner direct")

    if _SPANNER_INSTANCE and _SPANNER_DATABASE:
        try:
            logging.info(f"[ReflexAgent] Spanner direct: {_SPANNER_INSTANCE}/{_SPANNER_DATABASE}")
            result = _query_spanner_direct(eids)
            logging.info(f"[ReflexAgent] Spanner OK: {len(result['entity_details'])} entities | domains={result['affected_domains']}")
            return result
        except Exception as e:
            logging.warning(f"[ReflexAgent] Spanner direct failed: {e} \u2192 structural mock")

    logging.info("[ReflexAgent] Using structural mock (no Spanner/MCP available)")
    return _query_spanner_mock(eids)


def _resolve_eids_from_gnn(gnn_result: dict) -> list[str]:
    nodes = gnn_result.get("anomalousSubgraph", {}).get("nodes", [])
    return nodes if nodes else []


def call_gnn_engine(tool_context: ToolContext) -> str:
    state            = tool_context.state
    gnn_wrapper      = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    event_payload    = gnn_wrapper.get("payload", {})
    trigger_event_id = gnn_wrapper.get("event_id", "")

    if state.get("reflex_last_trigger_id") == trigger_event_id and trigger_event_id:
        return "GNN_SKIPPED: Already processed this trigger event"

    import json as _json

    print("\n" + "=" * 65)
    print("[Step 2B] Failure Notification \u2192 ReflexAgent")
    print("\n  API CALL")
    print("  POST /trigger_event")
    print("\n  REQUEST PAYLOAD")
    print(_json.dumps(event_payload, indent=4))
    print("=" * 65)
    print(f"  Source      : {event_payload.get('sourceSystem', event_payload.get('source', 'FAILURE_INJECTION_MS'))}")
    print(f"  Event Type  : {event_payload.get('eventType', event_payload.get('event_type', 'FailureInjectionCreateEvent'))}")
    print(f"  Trigger     : {event_payload.get('trigger', 'N/A')}")
    print(f"  Use Case ID : {event_payload.get('useCaseId', 'N/A')}")
    print(f"  Domain      : {event_payload.get('domain', 'N/A')}")
    print(f"  Affected eNBs: {event_payload.get('affected_enodebs', [])}")
    print(f"  Core Elems  : {event_payload.get('affected_core_elements', [])}")
    print(f"  Neighbors   : {event_payload.get('affected_neighbor_enodebs', [])}")
    print(f"  Time        : {datetime.now(timezone.utc).isoformat()}")
    print("\n  API RESPONSE")
    print("  HTTP 200 OK")
    print("\n  RESPONSE PAYLOAD")
    print(_json.dumps({
        "status":    "accepted",
        "eventId":   event_payload.get("eventId"),
        "eventType": event_payload.get("eventType", "FailureInjectionCreateEvent"),
        "message":   "FailureInjectionCreateEvent received by ReflexAgent",
    }, indent=4))
    print("-" * 65)

    import uuid as _uuid
    gnn_prompt = {
        "eventId": event_payload.get("eventId"),
        "prompt_type":   "ANOMALY_DETECTION_REQUEST",
        "source_agent":  "ReflexAgent",
        "request_id":    str(_uuid.uuid4()),
        "request_time":  datetime.now(timezone.utc).isoformat(),
        "trigger_source": event_payload.get("sourceSystem",
                           event_payload.get("source", "FAILURE_INJECTION_MS")),
        "trigger":                   event_payload.get("trigger", ""),
        "use_case_id":               event_payload.get("useCaseId", ""),
        "probable_domain":           event_payload.get("probableDomain", ""),
        "affected_enodebs":          event_payload.get("affected_enodebs", []),
        "affected_neighbor_enodebs": event_payload.get("affected_neighbor_enodebs", []),
        "core_elements":             event_payload.get("affected_core_elements", []),
        "affected_layers":           event_payload.get("affected_layers", []),
        "all_affected_entities": (
            event_payload.get("affected_enodebs", []) +
            event_payload.get("affected_core_elements", [])
        ),
        "failure_event_type": event_payload.get("eventType",
                               event_payload.get("event_type", "")),
        "message": (
            "Anomaly alert received. Analyse the Spanner network graph "
            "and return: anomalous subgraph, per-node anomaly scores, "
            "composite impact ranking, ranked remediation branches, "
            "impact_score, and criticality."
        ),
    }
    print("[Step 4a] ReflexAgent \u2192 GNN Inference Engine")
    print("  Action : tools call with JSON payload")
    print("-" * 65)
    print("\n  API CALL")
    print("  POST /analyze-anomaly")
    print("\n  REQUEST PAYLOAD")
    print("  JSON payload sent to GNN Inference Engine:")
    print(_json.dumps({
        "eventId":                    gnn_prompt["eventId"],
        "prompt_type":                gnn_prompt["prompt_type"],
        "source_agent":               gnn_prompt["source_agent"],
        "request_id":                 gnn_prompt["request_id"],
        "request_time":               gnn_prompt["request_time"],
        "trigger_source":             gnn_prompt["trigger_source"],
        "trigger":                    gnn_prompt["trigger"],
        "use_case_id":                gnn_prompt["use_case_id"],
        "affected_enodebs":           gnn_prompt["affected_enodebs"],
        "affected_neighbor_enodebs":  gnn_prompt["affected_neighbor_enodebs"],
        "core_elements":              gnn_prompt["core_elements"],
        "message":                    gnn_prompt["message"],
    }, indent=4))
    print("-" * 65)

    gnn_result = prompt_gnn_engine(gnn_prompt)

    nodes        = gnn_result.get("anomalousSubgraph", {}).get("nodes", [])
    edges        = gnn_result.get("anomalousSubgraph", {}).get("edges", [])
    ranked_list  = gnn_result.get("rankedList", [])
    impact_score = gnn_result.get("impact_score", 0.94)
    criticality_score = gnn_result.get("criticality_score", 1.0)
    criticality_label = gnn_result.get("criticality_label", "CRITICAL")

    neighbor_enodebs = event_payload.get("affected_neighbor_enodebs", [])
    if neighbor_enodebs and nodes:
        for i, nbr in enumerate(neighbor_enodebs):
            anchor_node = nodes[min(i, len(nodes) - 1)]
            edges = list(edges) + [[anchor_node, nbr]]

    composite_score = round(impact_score * 10, 1)

    print("[Step 4b] GNN Inference Engine \u2192 ReflexAgent")
    print("  Action : GNN anomalous subgraph + ranked list + impact score + criticality score")
    print("-" * 65)
    print("\n  API RESPONSE")
    print("  HTTP 200 OK")
    print("\n  RESPONSE PAYLOAD")
    print("  JSON payload received from GNN Inference Engine:")
    print(_json.dumps({
        "anomalousSubgraph": {
            "nodes":          nodes,
            "edges":          edges,
            "neighbor_nodes": neighbor_enodebs,
        },
        "rankedList":        ranked_list,
        "impact_score":      impact_score,
        "criticality_score": criticality_score,
        "criticality_label": criticality_label,
    }, indent=4))
    print("=" * 65)

    state["latest_gnn_result"] = {
        "anomalousSubgraph": gnn_result.get("anomalousSubgraph", {}),
        "rankedList":  ranked_list,
        "impact_score":      impact_score,
        "criticality_score": criticality_score,
        "criticality_label": criticality_label,
        "anomalyScore": {"compositeScore": composite_score, "zScore": composite_score, "confidence": 0.97},
        "businessPriority": "CRITICAL" if impact_score >= 0.6 else "HIGH" if impact_score >= 0.3 else "MEDIUM",
    }
    state["reflex_last_trigger_id"] = trigger_event_id
    state["pre_action_z_score"]     = composite_score
    state[NETWORK_STATUS_KEY]       = "ANOMALY_DETECTED"

    return (
        f"GNN_COMPLETE: priority=CRITICAL | "
        f"compositeScore={composite_score} | impact_score={impact_score} | "
        f"criticality={criticality_label} | nodes={nodes}"
    )


def perform_triage(tool_context: ToolContext) -> str:
    state      = tool_context.state
    gnn_result = state.get("latest_gnn_result", {})

    if not gnn_result or "anomalousSubgraph" not in gnn_result:
        return "TRIAGE_ERROR: GNN result not found. Run call_gnn_engine first."

    nodes        = gnn_result.get("anomalousSubgraph", {}).get("nodes", [])
    edges        = gnn_result.get("anomalousSubgraph", {}).get("edges", [])
    ranked_list  = gnn_result.get("rankedList", [])
    impact_score = gnn_result.get("impact_score", 0.94)
    criticality_label = gnn_result.get("criticality_label", "CRITICAL")
    criticality_score = gnn_result.get("criticality_score", 1.0)

    anomaly_score   = gnn_result.get("anomalyScore", {})
    composite_score = float(
        anomaly_score.get("compositeScore") or anomaly_score.get("zScore")
        or round(impact_score * 10, 1)
    )

    from ran_healing_shared.remediation_config import get_priority_flag, PRIORITY_FLAG_TO_EXTERNAL
    priority_flag     = get_priority_flag(composite_score)
    priority_external = PRIORITY_FLAG_TO_EXTERNAL.get(priority_flag, "CRITICAL")

    if priority_flag == "NORMAL":
        state["triage_result"] = {
            "status":          "BELOW_THRESHOLD",
            "composite_score": composite_score,
        }
        return f"TRIAGE_BELOW_THRESHOLD: compositeScore={composite_score}"

    failure_event = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    failure_payload = failure_event.get("payload", {})
    affected_enodebs_2b = failure_payload.get("affected_enodebs", [])
    core_elements_2b    = failure_payload.get("affected_core_elements", [])
    neighbor_enodebs_2b = failure_payload.get("affected_neighbor_enodebs", [])
    all_entities_2b     = affected_enodebs_2b + core_elements_2b

    query_eids = all_entities_2b if all_entities_2b else _resolve_eids_from_gnn(gnn_result)

    toolbox_up = _is_toolbox_running()
    print("\n[ReflexAgent \u2014 Step 5] MCP/tools call to Spanner DB")
    mcp_status = f"RUNNING at {_TOOLBOX_URL}" if toolbox_up else "NOT RUNNING \u2014 using fallback"
    print(f"  MCP Toolbox : {mcp_status}")
    print(f"  Spanner     : {_SPANNER_INSTANCE}/{_SPANNER_DATABASE}")
    print(f"  Query EIDs  : {query_eids}")
    print(f"  Source      : {'2b payload (real synth EIDs)' if all_entities_2b else 'GNN node IDs (fallback)'}")
    print("  Tools       : query_entity_domains, query_entity_connections, query_neighbor_cells")

    spanner_data   = _query_spanner_impact_radius(query_eids)
    node_score_map = {}

    entity_ids       = []
    node_domains     = {}
    affected_domains = set()

    for node in nodes:
        entity_info = spanner_data["entity_details"].get(node, {})
        real_eid    = entity_info.get("eid", node)
        domain      = entity_info.get("network_domain", "UNKNOWN")

        if domain == "UNKNOWN":
            domain = infer_domain([node])

        node_domains[node] = domain
        if domain != "UNKNOWN":
            affected_domains.add(domain)

    entity_ids = all_entities_2b if all_entities_2b else [
        spanner_data["entity_details"].get(n, {}).get("eid", n) for n in nodes
    ]

    affected_domains_list = sorted(affected_domains)

    if len(affected_domains_list) > 1:
        domain_triage = "CROSS_DOMAIN"
    elif len(affected_domains_list) == 1:
        domain_triage = affected_domains_list[0]
    else:
        domain_triage = infer_domain(nodes)

    node_priority_ranking = []
    for item in ranked_list:
        rank    = item.get("rank", 0)
        node_id = item.get("node_id", nodes[rank - 1] if rank <= len(nodes) else "UNKNOWN")
        if rank == 1:
            p_flag, p_ext = "P1", "CRITICAL"
        elif rank == 2:
            p_flag, p_ext = "P2", "HIGH"
        else:
            p_flag, p_ext = "P3", "MEDIUM"
        node_priority_ranking.append({
            "rank":          rank,
            "node_id":       node_id,
            "priority_flag": p_flag,
            "priority":      p_ext,
            "impact_score":  impact_score,
            "criticality_score": criticality_score,
            "criticality_label": criticality_label,
        })
    if not node_priority_ranking:
        for idx, node in enumerate(nodes):
            p_vals = [("P1","CRITICAL"),("P2","HIGH"),("P3","MEDIUM")]
            p_flag, p_ext = p_vals[min(idx, 2)]
            node_priority_ranking.append({
                "rank": idx+1, "node_id": node,
                "priority_flag": p_flag, "priority": p_ext,
                "impact_score": impact_score,
                "criticality_label": criticality_label,
            })
    node_priority_ranking.sort(key=lambda x: x["rank"])
    scoring_factors = {}

    print("\n" + "=" * 65)
    print("[ReflexAgent \u2014 Step 5] DOMAIN TRIAGE RESULT")
    print("=" * 65)
    print(f"  Data Source   : {spanner_data['source']}")
    print(f"  Tables queried: {spanner_data['tables_queried']}")
    print(f"  Impact Radius : {spanner_data['impact_radius']} nodes")
    print("")
    print("  Per-node domain (Spanner entity table):")
    print(f"  {'Node ID (EID)':<30} {'Domain':<14} {'Entity Type':<14}")
    print(f"  {'-'*30} {'-'*14} {'-'*14}")
    for node in nodes:
        eid_info = spanner_data["entity_details"].get(node, {})
        eid      = eid_info.get("eid", node)
        dom      = node_domains.get(node, "UNKNOWN")
        etype    = eid_info.get("entity_type", "N/A")
        print(f"  {eid:<30} {dom:<14} {etype:<14}")

    print(f"\n  Domains detected  : {affected_domains_list}")
    print(f"  Triage conclusion : {domain_triage}")

    print("\n  Node priority ranking (from GNN rankedList):")
    print(f"  {'Rank':<6} {'Node ID (EID)':<32} {'Priority':<12} {'Impact':<8} Criticality")
    print(f"  {'-'*6} {'-'*32} {'-'*12} {'-'*8} {'-'*10}")
    for r in node_priority_ranking:
        print(f"  {r['rank']:<6} {r['node_id']:<32} {r['priority']:<12} "
              f"{str(r['impact_score']):<8} {r['criticality_label']}")

    entity_ids_with_priority = [
        {
            "node_id":       r["node_id"],
            "rank":          r["rank"],
            "priority_flag": r["priority_flag"],
            "priority":      r["priority"],
        }
        for r in node_priority_ranking
    ]
    overall_priority_flag = node_priority_ranking[0]["priority_flag"] if node_priority_ranking else "P1"

    print(f"\n  6 fields \u2192 Detective Agent (Slide Step 5):")
    for e in entity_ids_with_priority:
        print(f"      rank={e['rank']} | {e['node_id']} | {e['priority_flag']} | {e['priority']}")
    print(f"    domain_triage     : {domain_triage}")
    print(f"    priority_flag     : {overall_priority_flag}")
    print(f"    priority          : {priority_external}")
    print(f"    impact_score      : {impact_score}")
    print(f"    criticality_score : {criticality_score}")
    print(f"    criticality_label : {criticality_label}")
    print(f"    reference_time    : {datetime.now(timezone.utc).isoformat()}")
    print("=" * 65)

    triage_payload = {
        "entity_ids":        entity_ids_with_priority,
        "eventId": failure_payload.get("eventId"),
        "ranked_list": node_priority_ranking,
        "domain_triage":     domain_triage,
        "priority_flag":     overall_priority_flag,
        "priority":          priority_external,
        "reference_time":    datetime.now(timezone.utc).isoformat(),
        "impact_score":      impact_score,
        "criticality_score": criticality_score,
        "criticality_label": criticality_label,
        "internal_priority_flag":         priority_flag,
        "priority_external":              priority_external,
        "composite_score":                composite_score,
        "composite_anomaly_impact_score": composite_score,
        "scoring_factors":                {},
        "node_priority_ranking":          node_priority_ranking,
        "confidence":                     anomaly_score.get("confidence", 0.0),
        "business_priority":              gnn_result.get("businessPriority"),
        "raw_nodes":                      nodes,
        "raw_edges":                      edges,
        "node_domains":                   node_domains,
        "affected_domains":               affected_domains_list,
        "spanner_source":                 spanner_data["source"],
        "spanner_tables":                 spanner_data["tables_queried"],
        "spanner_impact_radius":          spanner_data["impact_radius"],
        "spanner_connected_nodes":        spanner_data["connected_nodes"],
        "spanner_neighbor_cells":         spanner_data["neighbor_cells"],
    }

    state["triage_result"] = triage_payload
    return (
        f"TRIAGE_COMPLETE: domains={affected_domains_list} | "
        f"result={domain_triage} | source={spanner_data['source']} | "
        f"priority={overall_priority_flag} | score={composite_score} | "
        f"impact={impact_score} | entities={entity_ids}"
    )


def publish_triage(tool_context: ToolContext) -> str:
    state          = tool_context.state
    triage_payload = state.get("triage_result", {})

    if not triage_payload:
        return "PUBLISH_ERROR: No triage result. Run perform_triage first."

    if triage_payload.get("status") == "BELOW_THRESHOLD":
        return f"PUBLISH_SKIPPED: Score below threshold ({triage_payload.get('composite_score')})"

    gnn_wrapper  = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    gnn_event_id = gnn_wrapper.get("event_id", "")
    if state.get("reflex_last_event_id") == gnn_event_id and gnn_event_id:
        return "PUBLISH_SKIPPED: Already published for this trigger event"

    event = {
        "event_id":       str(uuid.uuid4()),
        "eventId": triage_payload.get("eventId"),
        "event_type":     EVT_REFLEX_TRIAGE_READY,
        "source":         "ReflexAgent",
        "event_time":     triage_payload["reference_time"],
        "network_status": "HEALING",
        "payload":        triage_payload,
    }
    publish_event(state, event)
    state["reflex_last_event_id"] = gnn_event_id
    state["reflex_output"]        = event
    state[NETWORK_STATUS_KEY]     = "HEALING"

    import json as _json
    ranking = triage_payload.get("node_priority_ranking", [])

    detective_payload = {
        "eventId":           triage_payload.get("eventId"),
        "entity_ids":        triage_payload.get("raw_nodes", []),
        "anomalous_subgraph": {
            "nodes": triage_payload.get("raw_nodes", []),
            "edges": triage_payload.get("raw_edges", []),
        },
        "ranked_list":       triage_payload.get("ranked_list"),
        "domain_triage":     triage_payload.get("domain_triage"),
        "priority_flag":     triage_payload.get("priority_flag"),
        "priority":          triage_payload.get("priority"),
        "impact_score":      triage_payload.get("impact_score"),
        "criticality_score": triage_payload.get("criticality_score"),
        "reference_time":    triage_payload.get("reference_time"),
    }

    print("\n" + "=" * 65)
    print("[Step 5] ReflexAgent \u2192 Detective Agent")
    print("=" * 65)
    print("\n  API CALL")
    print("  POST /investigation-request")
    print("\n  REQUEST PAYLOAD")
    print(_json.dumps(detective_payload, indent=4))
    print("=" * 65)
    print(f"  Affected Domains  : {triage_payload.get('affected_domains')}")
    print(f"  Domain Triage     : {triage_payload.get('domain_triage')}")
    print(f"  Priority Flag     : {triage_payload.get('priority_flag')}")
    print(f"  Priority          : {triage_payload.get('priority')}")
    print(f"  Impact Score      : {triage_payload.get('impact_score')}")
    print(f"  Criticality       : {triage_payload.get('criticality_score')}")
    print(f"  Affected Entities : {triage_payload.get('raw_nodes', [])}")
    print(f"  Impact Radius     : {triage_payload.get('spanner_impact_radius')} nodes")
    print(f"  Data Source       : {triage_payload.get('spanner_source')}")
    print("")
    print("  Node priority ranking (\u2192 Detective Agent A2A):")
    for r in ranking:
        print(f"    Rank {r['rank']} | {r['node_id']} | {r['priority_flag']} | "
              f"impact={r['impact_score']} | criticality={r['criticality_label']}")
    print(f"\n  Next Step: Detective Agent (Ericsson) via A2A API")
    print("=" * 65)

    return (
        f"PUBLISHED: reflex.triage.ready | "
        f"domains={triage_payload.get('affected_domains')} | "
        f"triage={triage_payload.get('domain_triage')} | "
        f"source={triage_payload.get('spanner_source')} | "
        f"priority={triage_payload.get('priority')}"
    )
