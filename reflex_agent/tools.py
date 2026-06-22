#reflex agent tools.py
import os
import json
import uuid
import math
import logging
import requests
import google.auth
import google.auth.transport.requests
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
from reflex_agent.step_events import emit_step
from google.cloud import storage

logging.basicConfig(level=logging.INFO)


PROJECT_ID = os.environ.get("GNN_PROJECT_ID", "poc-z-in2300756")
LOCATION = os.environ.get("GNN_LOCATION", "us-central1")
ENDPOINT_ID = os.environ.get("GNN_ENDPOINT_ID", "5276222599917993984")

_GCP_PROJECT        = os.environ.get("GOOGLE_CLOUD_PROJECT","poc-z-in2300756")
_SPANNER_INSTANCE   = os.environ.get("SPANNER_INSTANCE","verizon-gnn")
_SPANNER_DATABASE   = os.environ.get("SPANNER_DATABASE","tmforum_xl")
_TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://localhost:5000").rstrip("/")

_GCS_BUCKET    = os.environ.get("GCS_BUCKET", "vz-tmforum-2026")
_GCS_BLOB_PATH = os.environ.get("GCS_BLOB_PATH", "agent_persistent_data/agent-execution-properties.json")

GNN_INFERENCE_URL = os.environ.get(
    "GNN_INFERENCE_URL",
    f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/endpoints/{ENDPOINT_ID}"
).rstrip("/")

DETECTIVE_AGENT_URL = os.environ.get("DETECTIVE_AGENT_URL", "http://10.63.4.22:8000").rstrip("/")

# MCP tool names-configured in MCP Toolbox to query tmforum_xl database
MCP_TOOL_NODE_ENTITY = "query_tmf_node_entity"      #tmf_node_entity_table -> domain,criticality_tier
MCP_TOOL_SUBSCRIBER  = "query_tmf_node_entity"   #tmf_node_entity_table -> subscriber count

# ══════════════════════════════════════════════════════════════════════════════
# Natural language log helpers — MODULE LEVEL
# ══════════════════════════════════════════════════════════════════════════════
def _log_2b_received(event_payload: dict) -> None:
    """Step 2B — Event received from Failure Injection / Event Trigger MS."""
    event_id     = event_payload.get("eventId", "unknown")
    event_type   = event_payload.get("eventType", event_payload.get("event_type", "unknown"))
    use_case = event_payload.get("use_case_id", event_payload.get("useCaseId", "unknown"))
    domain = event_payload.get("probable_domain", event_payload.get("probableDomain", event_payload.get("domain","unknown")))
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

def _extract_pred_date(event_payload: dict) -> str:
    """
    Extract pred_date in YYYYMMDD format from the 2b event payload
    return statement will only get triggered when event_time will not there in payload.
    In our case this will not happen
    """
    raw_pred = event_payload.get("pred_date", "")
    if raw_pred and len(str(raw_pred)) == 8 and str(raw_pred).isdigit():
        return str(raw_pred)

    event_time = event_payload.get("eventTime", "")
    if event_time:
        try:
            dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            return dt.strftime("%Y%m%d")
        except (ValueError, TypeError):
            pass

    return datetime.now(timezone.utc).strftime("%Y%m%d")
       
def _log_4a_gnn_request(event_payload: dict, pred_date: str, gnn_url: str) -> None:
    """Step 4A — Calling GNN Inference Engine."""
    event_id = event_payload.get("eventId", "unknown")
    use_case = event_payload.get("use_case_id", event_payload.get("useCaseId", "unknown"))
    domain = event_payload.get("probable_domain",
    event_payload.get("probableDomain", event_payload.get("domain", "unknown")))
    dest     = gnn_url if gnn_url else "local GNN provider (GNN_INFERENCE_URL not set)"

    domain_ask = {
        "RAN":       "which RAN cells are affected and whether this is a tilt or coverage issue",
        "CORE":      "whether the HSS saturation has cascaded across the core and how far",
        "TRANSPORT": "which AGG/CSR nodes are down and the full blast radius downstream",
    }.get(domain.upper(), "the anomalous subgraph and root cause nodes")

    logging.info(
        f"[Step 4A] Calling the GNN Inference Engine for event '{event_id}'. "
        f"Sending pred_date={pred_date} for use case {use_case} on the {domain} domain. "
        f"The model will return the origin entities and GNN score. "
        f"Destination: {dest} | "
        f"eventId={event_id} | use_case={use_case} | pred_date={pred_date}"
    )
    logging.info(
        "[Step 4A] GNN Request Payload | %s",
        json.dumps({"instances": [{"pred_date": pred_date}]})
    )

def _log_4b_gnn_response(
    event_payload: dict,
    origin_entity_id: list,
    gnn_score: float,
    anomaly_id: str,
    pred_date: str,
) -> None:
    event_id = event_payload.get("eventId", "unknown")
    top_node = origin_entity_id[0] if origin_entity_id else "unknown"
    n_entities = len(origin_entity_id)

    logging.info(
        f"[Step 4B] GNN prediction returned for event '{event_id}'. "
        f"Predicted {n_entities} origin {'entity' if n_entities == 1 else 'entities'} "
        f"for pred_date={pred_date}. "
        f"Highest-impact entity: '{top_node}'. "
        f"GNN score max_impact_score: {gnn_score}. "
        f"Anomaly ID: {anomaly_id}. "
        f"eventId={event_id} | gnn_score={gnn_score} | "
        f"origin_entities={n_entities} | anomaly_id={anomaly_id}"
    )

    logging.info(
        "[Step 4B] GNN Response Payload | %s",
        json.dumps({
                "pred_date": pred_date,
                "origin_entity_id": origin_entity_id,
                "gnn_score": gnn_score,
                "anomaly_id": anomaly_id,
            },
            default=str,
        ),
    )
    
def _log_5_mcp_start(
    failure_payload:  dict,
    toolbox_up:       bool,
    toolbox_url:      str,
    spanner_instance: str,
    spanner_database: str,
    query_eids:       list,
) -> None:
    """Step 5 — Starting MCP/Spanner domain + subscriber lookup."""
    event_id = failure_payload.get("eventId", "unknown")

    if toolbox_up:
        path   = f"MCP Toolbox at {toolbox_url}"
        reason = "MCP Toolbox is running — using it as the primary path to Spanner."
    else:
        path   = f"Spanner direct ({spanner_instance}/{spanner_database})"
        reason = "MCP Toolbox is not running — falling back to direct Spanner client."

    logging.info(
        f"[Step 5] Querying tmf_node_entity for domain + criticality. "
        f"and subscriber table for subscriber counts. "
        f"{reason} "
        f"Querying {len(query_eids)} {'entity' if len(query_eids) == 1 else 'entities'}: {query_eids}. "
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

    source_label = {
        "mcp_toolbox":   "MCP Toolbox",
        "spanner_direct":"Spanner direct",
        "spanner_mock":  "structural mock (no Spanner available)",
    }.get(source, source)

    logging.info(
        f"[Step 5] Graph data back from {source_label}. "
        f"Found {n_ents} {'entity' if n_ents == 1 else 'entities'} "
        f"in domain{'s' if len(domains) != 1 else ''} {domains}. "
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
    
    domain_sentence = (
        f"This spans {len(affected_domains_list)} domains ({', '.join(affected_domains_list)}) "
        f"— treating as a cross-domain incident."
        if cross else
        f"Fault is contained to the {domain_triage} domain."
    )

    logging.info(
        f"[Step 5] Domain Triage Complete for event '{event_id}'. "
        f"{domain_sentence} "
        f"Anomaly impact ranking (impact_score) = {impact_score}  "
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
    priority = triage_payload.get("priority_flag", "unknown")
    entities = triage_payload.get("entity_ids", [])

    domain_ask = {
        "RAN":         "which antenna parameter changed and confirm the rollback target",
        "CORE":        "which HSS sessions are stale and confirm the clear action",
        "TRANSPORT":   "which AGG link is down and confirm the reroute path",
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
# Criticality tier mapping
# ══════════════════════════════════════════════════════════════════════════════
#tier 1 is most critical -> highest score
#score = (4 - tier + 1) / 4
_CRITICALITY_TIER_MAP = {
    1: {"label": "CRITICAL" , "score": 1.0}, #4/4
    2: {"label": "MAJOR" , "score": 0.75}, #3/4
    3: {"label": "MINOR" , "score": 0.50}, #2/4
    4: {"label": "NORMAL" , "score": 0.25}, #1/4
}  

def _map_criticality_tier(tier:int)-> tuple[str, float]:
    """Map criticality_tier (1-4) -> (label,score)."""
    entry = _CRITICALITY_TIER_MAP.get(tier, _CRITICALITY_TIER_MAP[4])
    return entry["label"] , entry["score"]

def _priority_from_impact(impact_score: float) -> str:
    "MAP anomaly_impact_ranking (impact_score) -> P1/P2/P3."""
    if impact_score >= 0.85:
        return "P1"
    if impact_score >= 0.6:
        return "P2"
    return "P3"
    
# ══════════════════════════════════════════════════════════════════════════════
# MCP Toolbox helpers
# ══════════════════════════════════════════════════════════════════════════════

def _is_toolbox_running() -> bool:
    """Health check MCP Toolbox using tools/list."""
    if not _TOOLBOX_URL:
        logging.warning("[MCP] TOOLBOX_URL is empty")
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
                "[MCP] Toolbox running at %s/mcp | status=%s | tools=%s",
                _TOOLBOX_URL,
                resp.status_code,
                tool_names,
            )

            if MCP_TOOL_NODE_ENTITY not in tool_names:
                logging.warning(
                    "[MCP] Toolbox is reachable but required tool '%s' is missing. Available tools=%s",
                    MCP_TOOL_NODE_ENTITY,
                    tool_names,
                )
                return False

            return True

        logging.warning(
            "[MCP] Toolbox health check returned HTTP %s | body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return False

    except requests.exceptions.Timeout as e:
        logging.warning(
            "[MCP] Toolbox health check timed out after 30s | url=%s/mcp | error=%s",
            _TOOLBOX_URL,
            str(e),
        )
        return False

    except requests.exceptions.ConnectionError as e:
        logging.warning(
            "[MCP] Toolbox connection failed | url=%s/mcp | error=%s",
            _TOOLBOX_URL,
            str(e),
        )
        return False

    except Exception as e:
        logging.warning(
            "[MCP] Toolbox health check failed | url=%s/mcp | error=%s",
            _TOOLBOX_URL,
            str(e),
        )
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
            timeout=30,
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
        logging.warning(f"[MCP] {tool_name}: timeout after 30s")
        return []
    except Exception as e:
        logging.warning(f"[MCP] {tool_name} failed: {e}")
        return []


def _query_via_mcp_toolbox(eids: list[str]) -> dict:
    """Query tmf_node_entity via MCP Toolbox."""
    entity_details = {}
    subscriber_data = {}
    domains = set()

    for row in _invoke_tool(MCP_TOOL_NODE_ENTITY, {"eids": eids}):
        eid = row.get("eid", row.get("entity_id", ""))
        dom = row.get("network_domain", "UNKNOWN")
        tier = int(row.get("criticality_tier", 4))

        if not eid:
            continue

        crit_label, crit_score = _map_criticality_tier(tier)

        entity_details[eid] = {
            "eid": eid,
            "entity_type": row.get("entity_type", "UNKNOWN"),
            "network_domain": dom,
            "network_segment": row.get("network_segment", "UNKNOWN"),
            "criticality_tier": tier,
            "criticality_label": crit_label,
            "criticality_score": crit_score,
        }

        subscriber_data[eid] = {
            "subscriber_count": float(row.get("subscriber_count", 0) or 0),
            "premium_subscriber_count": float(row.get("premium_subscriber_count", 0) or 0),
        }

        if dom and dom != "UNKNOWN":
            domains.add(dom)

    return {
        "source": "mcp_toolbox",
        "tables_queried": ["tmf_node_entity"],
        "query_eids": eids,
        "entity_details": entity_details,
        "subscriber_data": subscriber_data,
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
    subscriber_data = {}
    domains         = set()

    # ------- Query 1: tmf_node_entity ->domain _ criticality_tier ----------------
    with db.snapshot() as snap:
        for row in snap.execute_sql(
            "SELECT eid,entity_type,network_domain,network_segment,criticality_tier "
            "FROM tmf_node_entity WHERE eid IN UNNEST(@eids)",
            params={"eids": eids}, param_types={"eids": arr_type},
        ):
            eid_v, etype, dom, seg, tier = row
            tier = int(tier) if tier else 4
            crit_label, crit_score = _map_criticality_tier(tier)
            entity_details[eid_v] = {
                "eid": eid_v, "entity_type": etype or "UNKNOWN",
                "network_domain": dom or "UNKNOWN", "network_segment": seg or "UNKNOWN",
                "criticality_tier": tier,
                "criticality_label": crit_label,
                "criticality_score": crit_score,
            }
            if dom and dom != "UNKNOWN":
                domains.add(dom)
    # -------Query 2: subscriber table -> subscritber_count---------------------
    try:
        with db.snapshot() as snap:
            for row in snap.execute_sql(
                "SELECT eid, subscriber_count, premium_subscriber_count "
                "FROM tmf_node_entity WHERE eid IN UNNEST(@eids)",
                params={"eids": eids},
                param_types={"eids": arr_type},
            ):
                eid_v, subscriber_count, premium_subscriber_count = row
            
                if eid_v:
                    subscriber_data[eid_v] = {
                        "subscriber_count": float(subscriber_count) if subscriber_count else 0.0,
                        "premium_subscriber_count": float(premium_subscriber_count) if premium_subscriber_count else 0.0,
                    }
    except Exception as e:
        logging.warning(f"[Spanner] subscriber query failed: {e}")

    return {
        "source": "spanner_direct",
        "tables_queried":   ["tmf_node_entity"],
        "query_eids":       eids,
        "entity_details":   entity_details,
        "subscriber_data":  subscriber_data,
        "affected_domains": sorted(domains),
    }

def _query_spanner_mock(eids: list[str]) -> dict:
    """Structural mock — no hardcoded EIDs. Derives domain from EID prefix."""
    def _classify(eid: str) -> tuple[str, str, str]:
        u = eid.upper()
        if u.startswith("ENB") or "ENODEB" in u or "RAN_CELL" in u:
            return "RAN", "eNodeB", "LTE", 2
        if u.startswith("GNB") or "GNODEB" in u:
            return "RAN", "gNodeB", "NR", 2
        if "CELL" in u and "CORE" not in u:
            return "RAN", "eNodeB", "LTE", 3
        if "HSS" in u:   return "CORE", "HSS", "EPC", 1
        if "MME" in u:   return "CORE", "MME", "EPC", 1
        if "AMF" in u:   return "CORE", "AMF", "5GC", 1
        if "UPF" in u:   return "CORE", "UPF", "5GC", 2
        if "SMF" in u:   return "CORE", "SMF", "5GC", 2
        if "CSR" in u or "TRANSPORT_LINK" in u:
            return "TRANSPORT", "CSR", "IP_BACKHAUL", 2
        if "AGG" in u or "AGG_NODE" in u:
            return "TRANSPORT", "AGG", "IP_BACKHAUL", 2
        return "UNKNOWN", "UNKNOWN", "UNKNOWN", 4

    entity_details = {}
    subscriber_data = {}
    domains        = set()
    for eid in eids:
        dom, etype, seg, tier = _classify(eid)
        crit_label, crit_score = _map_criticality_tier(tier)
        entity_details[eid] = {
            "eid": eid, "entity_type": etype,
            "network_domain": dom, "network_segment": seg,
            "criticality_tier": tier,
            "criticality_label": crit_label,
            "criticality_score": crit_score,
        }
        mock_sub_counts = {"HSS": 500, "MME": 400, "AMF": 350, "UPF": 200,
                           "eNodeB": 150, "gNodeB": 100, "CSR": 80, "AGG": 60}
        mock_count = float(mock_sub_counts.get(etype, 100))
        subscriber_data[eid] = {
            "subscriber_count": mock_count,
            "premium_subscriber_count": round(mock_count * 0.2, 2),
        }
        if dom != "UNKNOWN":
            domains.add(dom)

    return {
        "source": "spanner_mock",
        "tables_queried":   ["tmf_node_entity"],
        "query_eids":       eids,
        "entity_details":   entity_details,
        "subscriber_data":  subscriber_data,
        "affected_domains": sorted(domains),
    }

# toolbox_up parameter added — prevents _is_toolbox_running() being called twice
def _query_spanner_impact_radius(eids: list[str], toolbox_up: bool | None = None) -> dict:
    """Priority: MCP Toolbox → Spanner direct → structural mock."""
    if toolbox_up is None:
        toolbox_up = _is_toolbox_running()

    if toolbox_up:
        try:
            logging.info(f"[ReflexAgent] MCP Toolbox at {_TOOLBOX_URL}")
            result = _query_via_mcp_toolbox(eids)

            entity_count = len(result.get("entity_details", {}))
            subscriber_count = len(result.get("subscriber_data", {}))

            if entity_count > 0 and subscriber_count > 0:
                logging.info(
                    f"[ReflexAgent] MCP OK: {entity_count} entities "
                    f"| subscribers={subscriber_count} "
                    f"| domains={result['affected_domains']}"
                )
                return result

            logging.warning(
                "[ReflexAgent] MCP returned incomplete data "
                f"(entities={entity_count}, subscribers={subscriber_count}) "
                "→ falling back to Spanner direct"
            )

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
    return gnn_result.get("origin_entity_id", [])


def _upload_to_gcs(data: dict) -> None:
    try:
        client = storage.Client()
        bucket = client.bucket(_GCS_BUCKET)
        blob = bucket.blob(_GCS_BLOB_PATH)
        blob.upload_from_string(
            json.dumps(data, indent=2, default=str),
            content_type="application/json",
        )
        logging.info(f"[GCS] saved to gs://{_GCS_BUCKET}/{_GCS_BLOB_PATH}")
    except Exception as e:
        logging.warning(f"[GCS] upload failed for gs://{_GCS_BUCKET}/{_GCS_BLOB_PATH} — {e}")


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
    _entities_2b = (
        event_payload.get("affected_enodebs", [])
        + event_payload.get("affected_core_elements", [])
        + event_payload.get("affected_transport_elements", [])
    )
    use_case_for_step = event_payload.get("use_case_id", event_payload.get("useCaseId", "?"))
    domain_for_step = event_payload.get(
        "probable_domain",
        event_payload.get("probableDomain", event_payload.get("domain", "?"))
    )
    
    emit_step(
        event_payload.get("eventId", ""),
        "event_received",
        "done",
        meta=(f"{use_case_for_step.upper()} · {domain_for_step} · entities={_entities_2b}"),
        payload={
            "eventId": event_payload.get("eventId"),
            "domain": domain_for_step,
            "entities": _entities_2b,
        },
    )

    # ── Step 4A: extract pred_date and align persisted event_time ─────────────
    pred_date = _extract_pred_date(event_payload)
    
    # Keep the original Failure Injection eventTime as the canonical event time
    # in the GCS-persisted wrapper.
    if event_payload.get("eventTime"):
        gnn_wrapper["event_time"] = event_payload.get("eventTime")
    
    # ── Upload full failure notification to GCS ───────────────────────────────
    _upload_to_gcs(gnn_wrapper)
    
    gnn_request_body = {"instances": [{"pred_date": pred_date}]}

    # ── Step 4A log ───────────────────────────────────────────────────────────
    _log_4a_gnn_request(event_payload, pred_date, GNN_INFERENCE_URL)
    emit_step(
        event_payload.get("eventId", ""),
        "gnn_request", "running",
        meta=(f"pred_date={pred_date} · {domain_for_step} · {use_case_for_step}"),
    )

    # ── Step 4A: call GNN endpoint ─────────────────────────────────────────────────────
    if GNN_INFERENCE_URL:
        gnn_url = f"{GNN_INFERENCE_URL}:predict"
    
        # ── Vertex AI Service Account Authentication ─────────────────────────────
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_request = google.auth.transport.requests.Request()
        credentials.refresh(auth_request)
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }
        logging.info(f"[Step 4A] Calling Vertex AI GNN endpoint | url={gnn_url} | pred_date={pred_date}")
        response = requests.post(
            gnn_url,
            json=gnn_request_body,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        gnn_raw = response.json()
    else:
        logging.info("[Step 4A] GNN_INFERENCE_URL not set — using local mock provider")
        gnn_raw = {
            "predictions": [{
                    "pred_date": pred_date,
                    "max_impact_score": 0.94,
                    "origin_entity_id": _entities_2b if _entities_2b else ["enB-SYN-001"],
                    "anomaly_id": f"ANOM-SYN-{pred_date}-ORIGIN",
                }]
        }

    # ── Step 4B: parse GNN prediction output ─────────────────────────────────
    prediction = gnn_raw.get("predictions", [{}])[0]
    origin_entity_id = (prediction.get("origin_entity_id") or prediction.get("origin_entity_ids")or [])
    if isinstance(origin_entity_id, str):
        origin_entity_id = [origin_entity_id]

    model_max_impact_score = float(prediction.get("max_impact_score", 0.0))

    # Fixed GNN score for normalized impact ranking
    gnn_score = 1.0

    anomaly_id = (prediction.get("origin_anomaly_id") or prediction.get("anomaly_id") or "")
    pred_date_resp = prediction.get("pred_date", pred_date)

    # ── Step 4B log ───────────────────────────────────────────────────────────
    _log_4b_gnn_response(
        event_payload,
        origin_entity_id,
        gnn_score,
        anomaly_id,
        pred_date_resp,
    )

    emit_step(
        event_payload.get("eventId", ""),
        "gnn_response",
        "done",
        meta=(
            f"gnn_score={gnn_score} · "
            f"model_max_impact_score={model_max_impact_score} · "
            f"origin_entities={origin_entity_id} · "
            f"anomaly_id={anomaly_id}"
        ),
        payload={
            "origin_entity_id": origin_entity_id,
            "gnn_score": gnn_score,
            "model_max_impact_score": model_max_impact_score,
            "anomaly_id": anomaly_id,
            "pred_date": pred_date_resp,
        },
    )

    # ── Store in state ────────────────────────────────────────────────────────
    state["latest_gnn_result"] = {
        "origin_entity_id": origin_entity_id,
        "gnn_score": gnn_score,
        "model_max_impact_score": model_max_impact_score,
        "anomaly_id": anomaly_id,
        "pred_date": pred_date_resp,
        "raw_prediction": prediction,
        "raw_gnn_response": gnn_raw,
    }

    state["reflex_last_trigger_id"] = trigger_event_id
    state[NETWORK_STATUS_KEY] = "ANOMALY_DETECTED"

    return (
        f"GNN_COMPLETE: gnn_score={gnn_score} | "
        f"model_max_impact_score={model_max_impact_score} | "
        f"origin_entities={origin_entity_id} | "
        f"anomaly_id={anomaly_id} | "
        f"pred_date={pred_date_resp}"
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

    if not gnn_result or "origin_entity_id" not in gnn_result:
        return "TRIAGE_ERROR: GNN result not found. Run call_gnn_engine first."

    origin_entity_id = gnn_result.get("origin_entity_id", [])
    gnn_score = float(gnn_result.get("gnn_score", 0.0))
    anomaly_id = gnn_result.get("anomaly_id", "")

    failure_event = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    failure_payload = failure_event.get("payload", {})

    # Use origin_entity_id from GNN as the entity list to query
    query_eids = list(origin_entity_id) if origin_entity_id else []

    # Fallback: if GNN returned empty, try 2b payload entites
    if not query_eids:
            query_eids = (
                failure_payload.get("affected_enodebs", [])
                + failure_payload.get("affected_core_elements", [])
                + failure_payload.get("affected_transport_elements", [])
            )
    if not query_eids:
            return "TRIAGE_ERROR: No entites to query - GNN returned empty origin_entity_id"
    toolbox_up = _is_toolbox_running()   # called ONCE here

    # ── Step 5 log: MCP start ─────────────────────────────────────────────────
    _log_5_mcp_start(
        failure_payload, toolbox_up, _TOOLBOX_URL,
        _SPANNER_INSTANCE or "", _SPANNER_DATABASE or "",
        query_eids,
    )
    emit_step(
        failure_payload.get("eventId", ""),
        "mcp_query", "running",
        meta=(f"{'MCP Toolbox' if toolbox_up else 'Spanner direct'} · "
              f"querying {len(query_eids)} "
              f"{'entity' if len(query_eids)==1 else 'entities'}: {query_eids}"),
    )

    # ── Step 5: Spanner query — (tmf_node_entity + subscriber table) ──
    spanner_data = _query_spanner_impact_radius(query_eids, toolbox_up=toolbox_up)

    # ── Step 5 log: MCP result ────────────────────────────────────────────────
    _log_5_mcp_result(spanner_data, failure_payload.get("eventId", "unknown"))
    emit_step(failure_payload.get("eventId", ""),"mcp_query","done",
        meta=(
            f"{spanner_data['source']} · "
            f"domains={spanner_data['affected_domains']}"
        ),
        payload={
            "source": spanner_data["source"],
            "affected_domains": spanner_data["affected_domains"],
        },
    )

    # ── Calculate anomaly_impact_ranking and resolve criticality per entity ──────────
    entity_details = spanner_data.get("entity_details", {})
    subscriber_data = spanner_data.get("subscriber_data", {})
    node_domains     = {}
    affected_domains = set()
    entity_scores  =[]

    # Step 5: Calculate raw anomaly impact ranking first.
    # raw_anomaly_impact_ranking = gnn_score * subscriber_count
    # Then normalize raw_anomaly_impact_ranking to calculate final impact_score.
    raw_rankings = {}
    log_rankings = {}
    
    for eid in query_eids:
        sub_info = subscriber_data.get(eid, {})
    
        subscriber_count = float(sub_info.get("subscriber_count", 0.0))
        premium_subscriber_count = float(sub_info.get("premium_subscriber_count", 0.0))
    
        total_subscribers = ((0.5 * subscriber_count)+ (1.5 * premium_subscriber_count))
        raw_value = round(gnn_score * total_subscribers, 6)
    
        raw_rankings[eid] = raw_value
        log_rankings[eid] = math.log1p(raw_value)
    
    max_log_ranking = max(log_rankings.values()) if log_rankings else 0.0
    
    if max_log_ranking <= 0:
        max_log_ranking = 1.0
    
    logging.info(
        "[Step 5] Impact normalization debug | raw_rankings=%s | log_rankings=%s | max_log_ranking=%s",
        json.dumps(raw_rankings, default=str),
        json.dumps(log_rankings, default=str),
        max_log_ranking,
    )
    
    for eid in query_eids:
        info = entity_details.get(eid, {})
    
        # Domain
        domain = info.get("network_domain", "UNKNOWN")
        if domain == "UNKNOWN":
            domain = infer_domain([eid])
    
        node_domains[eid] = domain
    
        if domain != "UNKNOWN":
            affected_domains.add(domain)
    
        # Criticality from tmf_node_entity.criticality_tier
        crit_tier = int(info.get("criticality_tier", 4))
        crit_label = info.get("criticality_label")
        crit_score = info.get("criticality_score")
    
        if crit_label is None or crit_score is None:
            crit_label, crit_score = _map_criticality_tier(crit_tier)
    
        sub_info = subscriber_data.get(eid, {})
        subscriber_count = float(sub_info.get("subscriber_count", 0.0))
        premium_subscriber_count = float(sub_info.get("premium_subscriber_count", 0.0))
        
        # Weighted subscriber impact
        total_subscribers = (0.5 * subscriber_count) + (1.5 * premium_subscriber_count)

        raw_anomaly_impact_ranking = raw_rankings.get(eid, 0.0)

        # Final event-local log-normalized impact_score
        impact_score_normalized = round(log_rankings.get(eid, 0.0) / max_log_ranking, 6,)
    
        entity_scores.append({
            "entity_id": eid,
            "domain": domain,
            "gnn_score": gnn_score,
        
            "subscriber_count": subscriber_count,
            "premium_subscriber_count": premium_subscriber_count,
            "total_subscribers": total_subscribers,
        
            "raw_anomaly_impact_ranking": raw_anomaly_impact_ranking,
            "anomaly_impact_ranking": impact_score_normalized,
            "impact_score": impact_score_normalized,
        
            "criticality_tier": crit_tier,
            "criticality_score": crit_score,
            "criticality_label": crit_label,
        })
    # Sort by anomaly_impact_ranking descending - highest impact first
    entity_scores.sort(key=lambda e: e["impact_score"], reverse=True)

    # Overall impact_score = highest anomaly_impact_ranking across entites
    impact_score = entity_scores[0]["impact_score"] if entity_scores else 0.0

    # Overall criticality = most critical entity(lowest tier = highest crticality)
    min_tier_entity = min(entity_scores, key=lambda e: e["criticality_tier"]) if entity_scores else {}
    criticality_label = min_tier_entity.get("criticality_label", "Normal")
    criticality_score = min_tier_entity.get("criticality_score", 0.0)

    # ------ Domain traige --------------------------------
    entity_ids = list(query_eids)
    affected_domains_list = sorted(affected_domains)
                          
    if len(affected_domains_list) > 1:
        domain_triage = "CROSS_DOMAIN"
    elif len(affected_domains_list) == 1:
        domain_triage = affected_domains_list[0]
    else:
        domain_triage = infer_domain(query_eids)

    # build node priority ranking from sorted entity_scores
    node_priority_ranking = []

    for idx, es in enumerate(entity_scores):
        p_flag = _priority_from_impact(es["impact_score"])
    
        node_priority_ranking.append({
            "rank": idx + 1,
            "entity_id": es["entity_id"],
            "domain": es["domain"],
            "priority_flag": p_flag,
            "impact_score": es["impact_score"],
            "criticality_score": es["criticality_score"],
            "criticality_label": es["criticality_label"],
        })
    overall_priority_flag = node_priority_ranking[0]["priority_flag"] if node_priority_ranking else "P3"
    entity_ids_with_priority = [
        {
            "entity_id": r["entity_id"],
            "rank": r["rank"],
            "domain": r["domain"],
            "priority_flag": r["priority_flag"],
            "impact_score": r["impact_score"],
            "criticality_score": r["criticality_score"],
            "criticality_label": r["criticality_label"],
        }
        for r in node_priority_ranking
    ]
    
    failure_event = state.get(latest_key(EVT_FAILURE_NOTIFICATION), {})
    failure_payload = failure_event.get("payload", {})
    
    event_time = (
        failure_event.get("event_time")
        or failure_payload.get("eventTime")
    )


    triage_payload = {
        "entity_ids":               entity_ids,
        "entity_ids_with_priority": entity_ids_with_priority,
        "eventId":                  failure_payload.get("eventId"),
        "ranked_list":              node_priority_ranking,
        "domain_triage":            domain_triage,
        "priority_flag":            overall_priority_flag,
        "reference_time":           event_time,
        "impact_score":             impact_score,
        "gnn_score":                gnn_score,
        "impact_score_type": "event_local_log_normalized_raw_anomaly_impact_ranking",
        "impact_score_formula": "log1p(gnn_score * ((0.5 * subscriber_count) + (1.5 * premium_subscriber_count))) / max_log1p_raw_ranking_for_current_event, with gnn_score fixed to 1.0",
        "criticality_score":        criticality_score,
        "criticality_label":        criticality_label,
        "anomaly_id":                anomaly_id,
        "node_priority_ranking":    node_priority_ranking,
        "entity_scores":            entity_scores,
        "business_priority":        criticality_label,
        "origin_entity_id":        origin_entity_id,
        "node_domains":             node_domains,
        "affected_domains":         affected_domains_list,
        "spanner_source":           spanner_data["source"],
        "spanner_tables":           spanner_data["tables_queried"],
    }

    state["triage_result"] = triage_payload

    # ── Step 5 log: triage complete ───────────────────────────────────────────
    _log_5_triage_complete(
        failure_payload, affected_domains_list, domain_triage,
        overall_priority_flag, impact_score, criticality_label, entity_ids,
    )
    _log_5_triage_payload(triage_payload)
    emit_step(
        failure_payload.get("eventId", ""),
        "domain_triage", "done",
        meta=(f"domain={domain_triage} · "
              f"{len(entity_ids)} {'entity' if len(entity_ids)==1 else 'entities'} · "
              f"source={spanner_data['source']}"),
        payload={"domain_triage": domain_triage,
                 "entity_ids": entity_ids,
                 "affected_domains": affected_domains_list},
    )
    emit_step(
        failure_payload.get("eventId", ""),
        "priority_flag",
        "done",
        meta=(
            f"{overall_priority_flag} · "
            f"impact={impact_score} · "
            f"criticality={criticality_label} · "
            f"gnn_score={gnn_score}"
        ),
        payload={
            "priority_flag": overall_priority_flag,
            "impact_score": impact_score,
            "criticality_label": criticality_label,
            "gnn_score": gnn_score,
        },
    )

    return (
        f"TRIAGE_COMPLETE: domains={affected_domains_list} | "
        f"result={domain_triage} | source={spanner_data['source']} | "
        f"priority={overall_priority_flag} | "
        f"impact_score={impact_score} | "
        f"gnn_score={gnn_score} | "
        f"criticality={criticality_label} | "
        f"entities={entity_ids}"
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
        "eventId": triage_payload.get("eventId"),
        "entity_ids": triage_payload.get("entity_ids"),
        "reference_time": triage_payload.get("reference_time"),
        "ranked_list": triage_payload.get("ranked_list"),
    }

    detective_url = f"{DETECTIVE_AGENT_URL}/investigate"

    # ── Step 5 OUT log: calling Detective ─────────────────────────────────────
    _log_5_out_calling_detective(triage_payload, detective_url)
    _log_5_out_detective_request(detective_payload)
    emit_step(
        triage_payload.get("eventId", ""),
        "detective_called", "running",
        meta=f"POST {detective_url} · priority={triage_payload.get('priority_flag')} · domain={triage_payload.get('domain_triage')}",
    )

    try:
        response = requests.post(detective_url, json=detective_payload, timeout=300)
        response.raise_for_status()
        detective_response = response.json()
        state["detective_response"] = detective_response
    except Exception as e:
        logging.exception(
            f"[Step 5 OUT] DetectiveAgent call failed | "
            f"eventId={triage_payload.get('eventId')} | error={str(e)}"
        )
        emit_step(
            triage_payload.get("eventId", ""),
            "detective_called", "error",
            meta=f"FAILED: {str(e)}",
        )
        return f"DETECTIVE_CALL_FAILED: {str(e)}"

    # ── Step 5 OUT log: Detective response ────────────────────────────────────
    _log_5_out_detective_response(triage_payload, detective_response)
    emit_step(
        triage_payload.get("eventId", ""),
        "detective_called", "done",
        meta=(f"status={detective_response.get('status', detective_response.get('state','acknowledged'))} · "
              f"root_cause={detective_response.get('root_cause','pending')}"),
        payload={"eventId": triage_payload.get("eventId"),
                 "status": detective_response.get("status",
                            detective_response.get("state","acknowledged")),
                 "root_cause": detective_response.get("root_cause","")},
    )

    return (
        f"PUBLISHED: reflex.triage.ready | "
        f"domains={triage_payload.get('affected_domains')} | "
        f"triage={triage_payload.get('domain_triage')} | "
        f"source={triage_payload.get('spanner_source')} | "
        f"priority={triage_payload.get('priority_flag')}"
    )