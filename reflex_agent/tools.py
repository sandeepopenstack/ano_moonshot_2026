import os
import json
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
from ran_healing_shared.remediation_config import infer_domain
from ran_healing_shared.gnn_inference_provider import prompt_gnn_engine

logging.basicConfig(level=logging.INFO)

_GCP_PROJECT        = os.environ.get("GOOGLE_CLOUD_PROJECT")
_SPANNER_INSTANCE   = os.environ.get("SPANNER_INSTANCE")
_SPANNER_DATABASE   = os.environ.get("SPANNER_DATABASE")
_TOOLBOX_URL        = os.environ.get("TOOLBOX_URL", "").rstrip("/")
GNN_INFERENCE_URL   = os.environ.get("GNN_INFERENCE_URL", "").rstrip("/")
DETECTIVE_AGENT_URL = os.environ.get("DETECTIVE_AGENT_URL", "").rstrip("/")


# ══════════════════════════════════════════════════════════════════════════════
# Natural language log helpers — MODULE LEVEL (not nested inside tool functions)
# Zero logic. Zero return values. Only logging.info() calls.
# Called by the tool functions below.
# ══════════════════════════════════════════════════════════════════════════════

def _log_2b_received(event_payload: dict) -> None:
    """Step 2B — Event received from Failure Injection / Event Trigger MS."""
    event_id     = event_payload.get("eventId", "unknown")
    event_type   = event_payload.get("eventType", event_payload.get("event_type", "unknown"))
    use_case     = event_payload.get("useCaseId", "unknown")
    domain       = event_payload.get("probableDomain", "unknown")
    source       = event_payload.get("sourceSystem", event_payload.get("source", "EventTriggerMS"))
    enodebs      = event_payload.get("affected_enodebs", [])
    core_elems   = event_payload.get("affected_core_elements", [])
    transport    = event_payload.get("affected_transport_elements", [])
    all_entities = enodebs + core_elems + transport

    uc_desc = {
        "uc1": "RAN coverage healing (antenna tilt misconfiguration)",
        "uc2": "core congestion healing (HSS saturation)",
        "uc3": "backhaul fiber cut (transport layer)",
    }.get(use_case.lower(), use_case)

    logging.info(
        f"[Step 2B] Alert received from {source}. "
        f"Event '{event_id}' — {event_type} — reports a fault on the {domain} domain. "
        f"Use case: {uc_desc}. "
        f"Affected entities: {all_entities}. "
        f"I need to call the GNN to understand what the graph looks like right now. "
        f"eventId={event_id} | eventType={event_type} | use_case={use_case} | domain={domain}"
    )
    logging.info(
        f"[Step 2B] FailureInjectionCreateEvent Payload | "
        f"{json.dumps(event_payload, default=str)}"
    )


def _log_4a_gnn_request(gnn_prompt: dict, gnn_url: str) -> None:
    """Step 4A — Calling GNN Inference Engine."""
    event_id = gnn_prompt.get("eventId", "unknown")
    use_case = gnn_prompt.get("use_case_id", "unknown")
    domain   = gnn_prompt.get("probable_domain", "unknown")
    entities = gnn_prompt.get("all_affected_entities", [])
    dest     = gnn_url if gnn_url else "local GNN provider (GNN_INFERENCE_URL not set)"

    domain_ask = {
        "RAN":       "which RAN cells are affected and whether this is a tilt or coverage issue",
        "CORE":      "whether the HSS saturation has cascaded across the core and how far",
        "TRANSPORT": "which AGG/CSR nodes are down and the full blast radius downstream",
    }.get(domain.upper(), "the anomalous subgraph and root cause nodes")

    logging.info(
        f"[Step 4A] Calling the GNN Inference Engine for event '{event_id}'. "
        f"Sending {len(entities)} affected {'entity' if len(entities) == 1 else 'entities'} "
        f"from the {domain} domain (use case {use_case}). "
        f"Asking it to traverse the Spanner graph and tell me {domain_ask}. "
        f"Destination: {dest} | "
        f"eventId={event_id} | use_case={use_case} | entities={len(entities)}"
    )
    logging.info(
        f"[Step 4A] GNN Request Payload | "
        f"{json.dumps(gnn_prompt, default=str)}"
    )


def _log_4b_gnn_response(
    event_payload:     dict,
    nodes:             list,
    edges:             list,
    ranked_list:       list,
    impact_score:      float,
    criticality_label: str,
    criticality_score: float,
    composite_score:   float,
    neighbor_enodebs:  list,
) -> None:
    """Step 4B — GNN response received."""
    event_id = event_payload.get("eventId", "unknown")
    top_node = (ranked_list[0].get("node_id") if ranked_list else nodes[0]) if nodes else "unknown"
    n_nodes  = len(nodes)
    n_ranked = len(ranked_list)

    sev_desc = {
        "CRITICAL": "this is critical — we need to act immediately",
        "MAJOR":    "significant degradation — high priority",
        "MINOR":    "minor degradation — medium priority",
    }.get(criticality_label.upper(), criticality_label)

    logging.info(
        f"[Step 4B] GNN responded for event '{event_id}'. "
        f"Anomalous subgraph has {n_nodes} {'node' if n_nodes == 1 else 'nodes'} "
        f"and {len(edges)} {'edge' if len(edges) == 1 else 'edges'}. "
        f"Highest-impact node: '{top_node}'. "
        f"Impact score {impact_score} — {sev_desc} (composite {composite_score}). "
        + (
            f"{len(neighbor_enodebs)} neighbouring "
            f"{'cell is' if len(neighbor_enodebs) == 1 else 'cells are'} "
            f"absorbing overflow — adding them to the subgraph. "
            if neighbor_enodebs else ""
        )
        + f"Ready to triage {n_ranked} ranked {'node' if n_ranked == 1 else 'nodes'}. "
        f"eventId={event_id} | impact_score={impact_score} | "
        f"criticality={criticality_label} | ranked_nodes={n_ranked} | subgraph_nodes={n_nodes}"
    )
    logging.info(
        f"[Step 4B] GNN Response Payload | "
        f"{json.dumps({'anomalousSubgraph':{'nodes':nodes,'edges':edges,'neighbor_nodes':neighbor_enodebs},'rankedList':ranked_list,'impact_score':impact_score,'criticality_score':criticality_score,'criticality_label':criticality_label}, default=str)}"
    )


def _log_5_mcp_start(
    failure_payload:  dict,
    toolbox_up:       bool,
    toolbox_url:      str,
    spanner_instance: str,
    spanner_database: str,
    query_eids:       list,
) -> None:
    """Step 5 — Starting MCP/Spanner domain lookup."""
    event_id = failure_payload.get("eventId", "unknown")

    if toolbox_up:
        path   = f"MCP Toolbox at {toolbox_url}"
        reason = "MCP Toolbox is running — using it as the primary path to Spanner."
    else:
        path   = f"Spanner direct ({spanner_instance}/{spanner_database})"
        reason = "MCP Toolbox is not running — falling back to direct Spanner client."

    logging.info(
        f"[Step 5] Time to figure out what domain these entities belong to. "
        f"{reason} "
        f"Querying entity domains, connection graph, and neighbour cells "
        f"for {len(query_eids)} {'entity' if len(query_eids) == 1 else 'entities'}: {query_eids}. "
        f"Source: {path} | eventId={event_id} | "
        f"toolbox_running={toolbox_up} | "
        f"spanner={spanner_instance}/{spanner_database} | "
        f"query_entities={query_eids}"
    )


def _log_5_mcp_result(spanner_data: dict, event_id: str) -> None:
    """Step 5 — MCP/Spanner result."""
    source    = spanner_data.get("source", "unknown")
    n_ents    = len(spanner_data.get("entity_details", {}))
    domains   = spanner_data.get("affected_domains", [])
    radius    = spanner_data.get("impact_radius", 0)
    connected = spanner_data.get("connected_nodes", [])
    neighbors = spanner_data.get("neighbor_cells", [])

    source_label = {
        "mcp_toolbox":   "MCP Toolbox",
        "spanner_direct":"Spanner direct",
        "spanner_mock":  "structural mock (no Spanner available)",
    }.get(source, source)

    logging.info(
        f"[Step 5] Graph data back from {source_label}. "
        f"Found {n_ents} {'entity' if n_ents == 1 else 'entities'} "
        f"in domain{'s' if len(domains) != 1 else ''} {domains}. "
        f"Impact radius: {radius} nodes "
        f"({len(connected)} connected node{'s' if len(connected) != 1 else ''}, "
        f"{len(neighbors)} neighbour cell{'s' if len(neighbors) != 1 else ''}). "
        f"eventId={event_id}"
    )


def _log_5_triage_complete(
    failure_payload:       dict,
    affected_domains_list: list,
    domain_triage:         str,
    overall_priority_flag: str,
    impact_score:          float,
    criticality_label:     str,
    entity_ids:            list,
) -> None:
    """Step 5 — Domain triage decision complete."""
    event_id = failure_payload.get("eventId", "unknown")
    cross    = len(affected_domains_list) > 1

    priority_human = {
        "P1": "P1 — critical, immediate action required",
        "P2": "P2 — high priority",
        "P3": "P3 — medium priority",
    }.get(overall_priority_flag, overall_priority_flag)

    domain_sentence = (
        f"This spans {len(affected_domains_list)} domains ({', '.join(affected_domains_list)}) "
        f"— treating as a cross-domain incident."
        if cross else
        f"Fault is contained to the {domain_triage} domain."
    )

    logging.info(
        f"[Step 5] Domain Triage Complete for event '{event_id}'. "
        f"{domain_sentence} "
        f"Impact score {impact_score} ({criticality_label}) puts this at {priority_human}. "
        f"Passing {len(entity_ids)} {'entity' if len(entity_ids) == 1 else 'entities'} "
        f"to the Detective Agent for full root cause analysis. "
        f"eventId={event_id} | domains={affected_domains_list} | "
        f"triage={domain_triage} | priority={overall_priority_flag} | "
        f"impact_score={impact_score} | criticality={criticality_label}"
    )


def _log_5_triage_payload(triage_payload: dict) -> None:
    logging.info(
        f"[Step 5] Triage Payload | "
        f"{json.dumps(triage_payload, default=str)}"
    )


def _log_5_out_calling_detective(triage_payload: dict, detective_url: str) -> None:
    """Step 5 OUT — Calling Detective Agent."""
    event_id = triage_payload.get("eventId", "unknown")
    domain   = triage_payload.get("domain_triage", "unknown")
    priority = triage_payload.get("priority", "unknown")
    entities = triage_payload.get("entity_ids", [])

    domain_ask = {
        "RAN":        "which antenna parameter changed and confirm the rollback target",
        "CORE":       "which HSS sessions are stale and confirm the clear action",
        "TRANSPORT":  "which AGG link is down and confirm the reroute path",
        "CROSS_DOMAIN":"the multi-domain fault chain and confirm per-domain remediation",
    }.get(domain.upper(), "root cause and confirm remediation actions")

    logging.info(
        f"[Step 5 OUT] Handing off to the Detective Agent for event '{event_id}'. "
        f"Asking it to run its 6-tool RCA pipeline on the {domain} domain "
        f"and tell me {domain_ask}. "
        f"Priority: {priority}. Entities: {entities}. "
        f"eventId={event_id} | url={detective_url} | "
        f"priority={priority} | domain={domain}"
    )


def _log_5_out_detective_request(detective_payload: dict) -> None:
    logging.info(
        f"[Step 5 OUT] Detective Request Payload | "
        f"{json.dumps(detective_payload, default=str)}"
    )


def _log_5_out_detective_response(
    triage_payload:     dict,
    detective_response: dict,
) -> None:
    """Step 5 OUT — Detective Agent accepted the request."""
    event_id = triage_payload.get("eventId", "unknown")
    status   = detective_response.get("status", detective_response.get("state", "acknowledged"))

    logging.info(
        f"[Step 5 OUT] Detective Agent accepted the investigation for event '{event_id}'. "
        f"Status: {status}. "
        f"The 6-tool RCA pipeline is now running. "
        f"I'll wait for the investigation.rca.confirmed event to come back."
    )
    logging.info(
        f"[Step 5 OUT] Detective Response Payload | "
        f"{json.dumps(detective_response, default=str)}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# MCP Toolbox helpers
# ══════════════════════════════════════════════════════════════════════════════

def _is_toolbox_running() -> bool:
    """Health check: POST /mcp with JSON-RPC 2.0 initialize request."""
    if not _TOOLBOX_URL:
        return False
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
    """Call a single MCP Toolbox tool via JSON-RPC 2.0 over POST /mcp."""
    url = f"{_TOOLBOX_URL}/mcp"
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=rpc_payload,
            timeout=15,
        )
        if resp.status_code != 200:
            logging.warning(
                f"[MCP] {tool_name} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return []

        body = resp.json()
        if "error" in body:
            logging.warning(f"[MCP] {tool_name} RPC error: {body['error']}")
            return []

        rows: list[dict] = []
        for block in body.get("result", {}).get("content", []):
            if block.get("type") == "text":
                try:
                    parsed = json.loads(block["text"])
                    if isinstance(parsed, list):
                        rows.extend(parsed)
                    elif isinstance(parsed, dict):
                        rows.append(parsed)
                except json.JSONDecodeError:
                    logging.warning(
                        f"[MCP] {tool_name} non-JSON text block: {block['text'][:100]}"
                    )
        logging.info(f"[MCP] {tool_name} → {len(rows)} rows")
        return rows

    except requests.exceptions.ConnectionError:
        logging.warning(f"[MCP] {tool_name}: connection refused — toolbox stopped?")
        return []
    except requests.exceptions.Timeout:
        logging.warning(f"[MCP] {tool_name}: timeout after 15s")
        return []
    except Exception as e:
        logging.warning(f"[MCP] {tool_name} failed: {e}")
        return []


def _query_via_mcp_toolbox(eids: list[str]) -> dict:
    """Query Spanner via MCP Toolbox — 3 tools in sequence."""
    entity_details  = {}
    connected_nodes = []
    neighbor_cells  = []
    domains         = set()
    eid_set         = set(eids)

    for row in _invoke_tool("query_entity_domains", {"eids": eids}):
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

    for row in _invoke_tool("query_entity_connections", {"eids": eids}):
        target = row.get("target_eid", "")
        if target and target not in eid_set:
            connected_nodes.append({
                "source_eid":    row.get("source_eid", ""),
                "target_eid":    target,
                "edge_type":     row.get("edge_type", "connected"),
                "target_domain": row.get("network_domain", "UNKNOWN"),
            })

    for row in _invoke_tool("query_neighbor_cells", {"eids": eids}):
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
    """Direct Spanner client fallback."""
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
            "SELECT string_field_0, string_field_1, string_field_2, string_field_3 "
            "FROM entity WHERE string_field_0 IN UNNEST(@eids)",
            params={"eids": eids}, param_types={"eids": arr_type},
        ):
            eid_v, etype, dom, seg = row
            entity_details[eid_v] = {
                "eid": eid_v, "entity_type": etype or "UNKNOWN",
                "network_domain": dom or "UNKNOWN", "network_segment": seg or "UNKNOWN",
            }
            if dom and dom != "UNKNOWN":
                domains.add(dom)

    with db.snapshot() as snap:
        for row in snap.execute_sql(
            "SELECT eid, to_eid, edge_type, network_domain "
            "FROM edge_entitytoentity WHERE eid IN UNNEST(@eids)",
            params={"eids": eids}, param_types={"eids": arr_type},
        ):
            src, tgt, etype, dom = row
            if tgt and tgt not in eid_set:
                connected_nodes.append({
                    "source_eid": src, "target_eid": tgt,
                    "edge_type": etype or "connected", "target_domain": dom or "UNKNOWN",
                })

    with db.snapshot() as snap:
        for row in snap.execute_sql(
            "SELECT eid, to_eid FROM edge_entitytoneighbor WHERE eid IN UNNEST(@eids)",
            params={"eids": eids}, param_types={"eids": arr_type},
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
    """Structural mock — no hardcoded EIDs. Derives domain from EID prefix."""
    def _classify(eid: str) -> tuple[str, str, str]:
        u = eid.upper()
        if u.startswith("ENB") or "ENODEB" in u or "RAN_CELL" in u:
            return "RAN", "eNodeB", "LTE"
        if u.startswith("GNB") or "GNODEB" in u:
            return "RAN", "gNodeB", "NR"
        if "CELL" in u and "CORE" not in u:
            return "RAN", "eNodeB", "LTE"
        if "HSS" in u:   return "CORE", "HSS", "EPC"
        if "MME" in u:   return "CORE", "MME", "EPC"
        if "AMF" in u:   return "CORE", "AMF", "5GC"
        if "UPF" in u:   return "CORE", "UPF", "5GC"
        if "SMF" in u:   return "CORE", "SMF", "5GC"
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
    """Priority: MCP Toolbox → Spanner direct → structural mock."""
    if _is_toolbox_running():
        try:
            logging.info(f"[ReflexAgent] MCP Toolbox at {_TOOLBOX_URL}")
            result = _query_via_mcp_toolbox(eids)
            logging.info(
                f"[ReflexAgent] MCP OK: {len(result['entity_details'])} entities "
                f"| domains={result['affected_domains']}"
            )
            return result
        except Exception as e:
            logging.warning(f"[ReflexAgent] MCP Toolbox call failed: {e} → Spanner direct")
    else:
        logging.info(
            f"[ReflexAgent] MCP Toolbox not running at {_TOOLBOX_URL} "
            f"→ trying Spanner direct"
        )

    if _SPANNER_INSTANCE and _SPANNER_DATABASE:
        try:
            logging.info(
                f"[ReflexAgent] Spanner direct: {_SPANNER_INSTANCE}/{_SPANNER_DATABASE}"
            )
            result = _query_spanner_direct(eids)
            logging.info(
                f"[ReflexAgent] Spanner OK: {len(result['entity_details'])} entities "
                f"| domains={result['affected_domains']}"
            )
            return result
        except Exception as e:
            logging.warning(f"[ReflexAgent] Spanner direct failed: {e} → structural mock")

    logging.info("[ReflexAgent] Using structural mock (no Spanner/MCP available)")
    return _query_spanner_mock(eids)


def _resolve_eids_from_gnn(gnn_result: dict) -> list[str]:
    nodes = gnn_result.get("anomalousSubgraph", {}).get("nodes", [])
    return nodes if nodes else []


# ══════════════════════════════════════════════════════════════════════════════
# Tool 1: call_gnn_engine
# ══════════════════════════════════════════════════════════════════════════════

def call_gnn_engine(tool_context: ToolContext) -> str:
    """
    Tool 1 of 3 — NO arguments.
    Step 4a: tools call to GNN Inference Engine.
    Step 4b: GNN returns anomalousSubgraph, rankedList, impact_score, criticality.
    """
    state            = tool_context.state
    gnn_wrapper      = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    event_payload    = gnn_wrapper.get("payload", {})
    trigger_event_id = gnn_wrapper.get("event_id", "")

    if state.get("reflex_last_trigger_id") == trigger_event_id and trigger_event_id:
        return "GNN_SKIPPED: Already processed this trigger event"

    # ── Step 2B log ───────────────────────────────────────────────────────────
    _log_2b_received(event_payload)

    # ── Step 4A: build GNN prompt ─────────────────────────────────────────────
    gnn_prompt = {
        "eventId":        event_payload.get("eventId"),
        "prompt_type":    "ANOMALY_DETECTION_REQUEST",
        "source_agent":   "ReflexAgent",
        "request_id":     str(uuid.uuid4()),
        "request_time":   datetime.now(timezone.utc).isoformat(),
        "trigger_source": event_payload.get(
            "sourceSystem", event_payload.get("source", "FAILURE_INJECTION_MS")
        ),
        "trigger":                   event_payload.get("trigger", ""),
        "use_case_id":               event_payload.get("useCaseId", ""),
        "probable_domain":           event_payload.get("probableDomain", ""),
        "affected_enodebs":          event_payload.get("affected_enodebs", []),
        "affected_neighbor_enodebs": event_payload.get("affected_neighbor_enodebs", []),
        "core_elements":             event_payload.get("affected_core_elements", []),
        "transport_elements":        event_payload.get("affected_transport_elements", []),
        "affected_layers":           event_payload.get("affected_layers", []),
        # UC3 fix: include transport elements in all_affected_entities
        "all_affected_entities": (
            event_payload.get("affected_enodebs", [])
            + event_payload.get("affected_core_elements", [])
            + event_payload.get("affected_transport_elements", [])
        ),
        "failure_event_type": event_payload.get(
            "eventType", event_payload.get("event_type", "")
        ),
        "message": (
            "Anomaly alert received. Analyse the Spanner network graph "
            "and return: anomalous subgraph, per-node anomaly scores, "
            "composite impact ranking, ranked remediation branches, "
            "impact_score, and criticality."
        ),
    }

    # ── Step 4A log ───────────────────────────────────────────────────────────
    _log_4a_gnn_request(gnn_prompt, GNN_INFERENCE_URL)

    # ── Step 4A: call GNN ─────────────────────────────────────────────────────
    if GNN_INFERENCE_URL:
        gnn_url = f"{GNN_INFERENCE_URL}/analyze-anomaly"
        response = requests.post(gnn_url, json=gnn_prompt, timeout=60)
        response.raise_for_status()
        gnn_result = response.json()
    else:
        logging.info("[Step 4A] GNN_INFERENCE_URL not set — using local mock provider")
        gnn_result = prompt_gnn_engine(gnn_prompt)

    # ── Step 4B: parse GNN output ─────────────────────────────────────────────
    nodes             = gnn_result.get("anomalousSubgraph", {}).get("nodes", [])
    edges             = gnn_result.get("anomalousSubgraph", {}).get("edges", [])
    ranked_list       = gnn_result.get("rankedList", [])
    impact_score      = gnn_result.get("impact_score", 0.94)
    criticality_score = gnn_result.get("criticality_score", 1.0)
    criticality_label = gnn_result.get("criticality_label", "CRITICAL")

    # Augment edges with neighbour overflow connections
    neighbor_enodebs = event_payload.get("affected_neighbor_enodebs", [])
    if neighbor_enodebs and nodes:
        for i, nbr in enumerate(neighbor_enodebs):
            anchor_node = nodes[min(i, len(nodes) - 1)]
            edges = list(edges) + [[anchor_node, nbr]]

    composite_score = round(impact_score * 10, 1)

    # ── Step 4B log ───────────────────────────────────────────────────────────
    _log_4b_gnn_response(
        event_payload, nodes, edges, ranked_list,
        impact_score, criticality_label, criticality_score,
        composite_score, neighbor_enodebs,
    )

    # ── Store in state ────────────────────────────────────────────────────────
    state["latest_gnn_result"] = {
        "anomalousSubgraph": gnn_result.get("anomalousSubgraph", {}),
        "rankedList":         ranked_list,
        "impact_score":       impact_score,
        "criticality_score":  criticality_score,
        "criticality_label":  criticality_label,
        "anomalyScore": {
            "compositeScore": composite_score,
            "zScore":         composite_score,
            "confidence":     0.97,
        },
        "businessPriority": (
            "CRITICAL" if impact_score >= 0.6
            else "HIGH" if impact_score >= 0.3
            else "MEDIUM"
        ),
    }
    state["reflex_last_trigger_id"] = trigger_event_id
    state["pre_action_z_score"]     = composite_score
    state[NETWORK_STATUS_KEY]       = "ANOMALY_DETECTED"

    return (
        f"GNN_COMPLETE: priority=CRITICAL | "
        f"compositeScore={composite_score} | impact_score={impact_score} | "
        f"criticality={criticality_label} | nodes={nodes}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tool 2: perform_triage
# ══════════════════════════════════════════════════════════════════════════════

def perform_triage(tool_context: ToolContext) -> str:
    """
    Tool 2 of 3 — NO arguments.
    Step 5: MCP/Spanner domain lookup → domain triage → priority flag.
    """
    state      = tool_context.state
    gnn_result = state.get("latest_gnn_result", {})

    if not gnn_result or "anomalousSubgraph" not in gnn_result:
        return "TRIAGE_ERROR: GNN result not found. Run call_gnn_engine first."

    nodes             = gnn_result.get("anomalousSubgraph", {}).get("nodes", [])
    edges             = gnn_result.get("anomalousSubgraph", {}).get("edges", [])
    ranked_list       = gnn_result.get("rankedList", [])
    impact_score      = gnn_result.get("impact_score", 0.94)
    criticality_label = gnn_result.get("criticality_label", "CRITICAL")
    criticality_score = gnn_result.get("criticality_score", 1.0)

    anomaly_score   = gnn_result.get("anomalyScore", {})
    composite_score = float(
        anomaly_score.get("compositeScore")
        or anomaly_score.get("zScore")
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

    # ── Entity resolution from 2b payload ────────────────────────────────────
    failure_event   = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    failure_payload = failure_event.get("payload", {})

    affected_enodebs_2b   = failure_payload.get("affected_enodebs", [])
    core_elements_2b      = failure_payload.get("affected_core_elements", [])
    transport_elements_2b = failure_payload.get("affected_transport_elements", [])   # UC3 fix
    neighbor_enodebs_2b   = failure_payload.get("affected_neighbor_enodebs", [])
    all_entities_2b       = affected_enodebs_2b + core_elements_2b + transport_elements_2b

    query_eids = all_entities_2b if all_entities_2b else _resolve_eids_from_gnn(gnn_result)
    toolbox_up = _is_toolbox_running()

    # ── Step 5 log: MCP start ─────────────────────────────────────────────────
    _log_5_mcp_start(
        failure_payload, toolbox_up, _TOOLBOX_URL,
        _SPANNER_INSTANCE or "", _SPANNER_DATABASE or "",
        query_eids,
    )

    # ── Step 5: Spanner query ─────────────────────────────────────────────────
    spanner_data = _query_spanner_impact_radius(query_eids)

    # ── Step 5 log: MCP result ────────────────────────────────────────────────
    _log_5_mcp_result(spanner_data, failure_payload.get("eventId", "unknown"))

    # ── Build domain map ──────────────────────────────────────────────────────
    node_domains     = {}
    affected_domains = set()

    for node in nodes:
        entity_info = spanner_data["entity_details"].get(node, {})
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

    # ── Node priority ranking ─────────────────────────────────────────────────
    node_priority_ranking = []
    for item in ranked_list:
        rank    = item.get("rank", 0)
        node_id = item.get("node_id", nodes[rank - 1] if rank <= len(nodes) else "UNKNOWN")
        p_flag, p_ext = (
            ("P1", "CRITICAL") if rank == 1
            else ("P2", "HIGH") if rank == 2
            else ("P3", "MEDIUM")
        )
        node_priority_ranking.append({
            "rank":              rank,
            "node_id":           node_id,
            "priority_flag":     p_flag,
            "priority":          p_ext,
            "impact_score":      impact_score,
            "criticality_score": criticality_score,
            "criticality_label": criticality_label,
        })

    if not node_priority_ranking:
        for idx, node in enumerate(nodes):
            p_flag, p_ext = [("P1","CRITICAL"),("P2","HIGH"),("P3","MEDIUM")][min(idx, 2)]
            node_priority_ranking.append({
                "rank": idx + 1, "node_id": node,
                "priority_flag": p_flag, "priority": p_ext,
                "impact_score": impact_score, "criticality_label": criticality_label,
            })

    node_priority_ranking.sort(key=lambda x: x["rank"])

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

    triage_payload = {
        "entity_ids":               entity_ids,
        "entity_ids_with_priority": entity_ids_with_priority,
        "eventId":                  failure_payload.get("eventId"),
        "ranked_list":              node_priority_ranking,
        "domain_triage":            domain_triage,
        "priority_flag":            overall_priority_flag,
        "priority":                 priority_external,
        "reference_time":           datetime.now(timezone.utc).isoformat(),
        "impact_score":             impact_score,
        "criticality_score":        criticality_score,
        "criticality_label":        criticality_label,
        "internal_priority_flag":   priority_flag,
        "priority_external":        priority_external,
        "composite_score":          composite_score,
        "composite_anomaly_impact_score": composite_score,
        "scoring_factors":          {},
        "node_priority_ranking":    node_priority_ranking,
        "confidence":               anomaly_score.get("confidence", 0.0),
        "business_priority":        gnn_result.get("businessPriority"),
        "raw_nodes":                nodes,
        "raw_edges":                edges,
        "node_domains":             node_domains,
        "affected_domains":         affected_domains_list,
        "spanner_source":           spanner_data["source"],
        "spanner_tables":           spanner_data["tables_queried"],
        "spanner_impact_radius":    spanner_data["impact_radius"],
        "spanner_connected_nodes":  spanner_data["connected_nodes"],
        "spanner_neighbor_cells":   spanner_data["neighbor_cells"],
    }

    state["triage_result"] = triage_payload

    # ── Step 5 log: triage complete ───────────────────────────────────────────
    _log_5_triage_complete(
        failure_payload, affected_domains_list, domain_triage,
        overall_priority_flag, impact_score, criticality_label, entity_ids,
    )
    _log_5_triage_payload(triage_payload)

    return (
        f"TRIAGE_COMPLETE: domains={affected_domains_list} | "
        f"result={domain_triage} | source={spanner_data['source']} | "
        f"priority={overall_priority_flag} | score={composite_score} | "
        f"impact={impact_score} | entities={entity_ids}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tool 3: publish_triage
# ══════════════════════════════════════════════════════════════════════════════

def publish_triage(tool_context: ToolContext) -> str:
    """
    Tool 3 of 3 — NO arguments.
    Publishes reflex.triage.ready and POSTs to Detective Agent.
    """
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
        "event_id":      str(uuid.uuid4()),
        "eventId":       triage_payload.get("eventId"),
        "event_type":    EVT_REFLEX_TRIAGE_READY,
        "source":        "ReflexAgent",
        "event_time":    triage_payload["reference_time"],
        "network_status":"HEALING",
        "payload":       triage_payload,
    }
    publish_event(state, event)
    state["reflex_last_event_id"] = gnn_event_id
    state["reflex_output"]        = event
    state[NETWORK_STATUS_KEY]     = "HEALING"

    detective_payload = {
        "eventId":            triage_payload.get("eventId"),
        "entity_ids":         triage_payload.get("entity_ids"),
        "anomalous_subgraph": {
            "nodes": triage_payload.get("raw_nodes", []),
            "edges": triage_payload.get("raw_edges", []),
        },
        "ranked_list":        triage_payload.get("ranked_list"),
        "domain_triage":      triage_payload.get("domain_triage"),
        "priority_flag":      triage_payload.get("priority_flag"),
        "priority":           triage_payload.get("priority"),
        "impact_score":       triage_payload.get("impact_score"),
        "criticality_score":  triage_payload.get("criticality_score"),
        "criticality_label":  triage_payload.get("criticality_label"),
        "reference_time":     triage_payload.get("reference_time"),
    }

    detective_url = f"{DETECTIVE_AGENT_URL}/investigate"

    # ── Step 5 OUT log: calling Detective ─────────────────────────────────────
    _log_5_out_calling_detective(triage_payload, detective_url)
    _log_5_out_detective_request(detective_payload)

    try:
        response = requests.post(detective_url, json=detective_payload, timeout=120)
        response.raise_for_status()
        detective_response = response.json()
        state["detective_response"] = detective_response
    except Exception as e:
        logging.exception(
            f"[Step 5 OUT] DetectiveAgent call failed | "
            f"eventId={triage_payload.get('eventId')} | error={str(e)}"
        )
        return f"DETECTIVE_CALL_FAILED: {str(e)}"

    # ── Step 5 OUT log: Detective response ────────────────────────────────────
    _log_5_out_detective_response(triage_payload, detective_response)

    return (
        f"PUBLISHED: reflex.triage.ready | "
        f"domains={triage_payload.get('affected_domains')} | "
        f"triage={triage_payload.get('domain_triage')} | "
        f"source={triage_payload.get('spanner_source')} | "
        f"priority={triage_payload.get('priority')}"
    )