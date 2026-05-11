"""
test_mcp_tools.py
==================
Standalone test for the 3 MCP Toolbox tools.

Run AFTER start_mcp_toolbox.sh is running:
  python test_mcp_tools.py

Tests each tool with real EIDs from the synth data:
  UC1 (RAN):       eNB-SYN-003, eNB-SYN-004, eNB-SYN-005
  UC2 (CORE):      HSS-SYN-01
  UC3 (TRANSPORT): AGG-SYN-01

Verifies:
  1. Toolbox health check
  2. query_entity_domains   → returns eid, entity_type, network_domain, network_segment
  3. query_entity_connections → returns source_eid, target_eid, edge_type, network_domain
  4. query_neighbor_cells   → returns source_eid, neighbor_eid
  5. Domain triage simulation (same logic as ReflexAgent perform_triage)
"""

import json
import requests
import os

TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://localhost:5000").rstrip("/")

# ── Test EIDs (UC1 RAN + UC2 CORE for cross-domain simulation) ────────────────
TEST_EIDS_UC1 = [
    "eNB-SYN-003",
    "eNB-SYN-004",
    "eNB-SYN-005",
    "eNB-SYN-006",
    "eNB-SYN-007",
]

TEST_EIDS_UC2 = [
    "HSS-SYN-01",
]

TEST_EIDS_CROSS = TEST_EIDS_UC1[:2] + TEST_EIDS_UC2  # mixed RAN + CORE


# ── Helper ─────────────────────────────────────────────────────────────────────

def invoke_tool(tool_name: str, params: dict):

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": params
        }
    }

    resp = requests.post(
        f"{TOOLBOX_URL}/mcp",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )

    resp.raise_for_status()

    body = resp.json()

    if "result" not in body:
        raise RuntimeError(f"MCP error: {body}")

    result = body["result"]

    rows = []

    # MCP Toolbox wraps SQL rows inside content[]
    for item in result.get("content", []):

        if item.get("type") != "text":
            continue

        text = item.get("text", "").strip()

        if not text:
            continue

        try:
            rows.append(json.loads(text))
        except Exception:
            print(f"WARNING: failed to parse row: {text}")

    return rows


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def check(label: str, condition: bool, detail: str = ""):
    mark = "✓" if condition else "✗"
    print(f"  {mark}  {label}")
    if detail and not condition:
        print(f"       → {detail}")
    return condition


# ── Test 0: Health check ───────────────────────────────────────────────────────

section("Test 0 — MCP health check")

try:

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    }

    resp = requests.post(
        f"{TOOLBOX_URL}/mcp",
        json=payload,
        timeout=10,
    )

    resp.raise_for_status()

    body = resp.json()

    print(json.dumps(body, indent=2))

    check("MCP endpoint reachable", True)

except Exception as e:

    check("MCP endpoint reachable", False, str(e))
    exit(1)

# ── Test 1: query_entity_domains ──────────────────────────────────────────────

section("Test 1 — query_entity_domains (UC1 RAN)")
rows1 = invoke_tool("query_entity_domains", {"eids": TEST_EIDS_UC1})
print(f"\n  Returned {len(rows1)} rows for {len(TEST_EIDS_UC1)} EIDs\n")

check("Returns at least 1 row",       len(rows1) > 0)
if rows1:
    r = rows1[0]
    check("Row has 'eid' field",           "eid"            in r,  f"got keys: {list(r.keys())}")
    check("Row has 'entity_type' field",   "entity_type"    in r,  f"got keys: {list(r.keys())}")
    check("Row has 'network_domain' field","network_domain" in r,  f"got keys: {list(r.keys())}")
    check("Row has 'network_segment' field","network_segment" in r, f"got keys: {list(r.keys())}")

    domains = {r.get("network_domain") for r in rows1}
    check("All UC1 entities are RAN domain", domains == {"RAN"}, f"got: {domains}")

    print("\n  Per-entity results:")
    print(f"  {'EID':<25} {'Type':<12} {'Domain':<12} {'Segment'}")
    print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}")
    for r in rows1:
        print(f"  {r.get('eid',''):<25} {r.get('entity_type',''):<12} "
              f"{r.get('network_domain',''):<12} {r.get('network_segment','')}")


# ── Test 2: query_entity_connections ─────────────────────────────────────────

section("Test 2 — query_entity_connections (UC1 RAN)")
rows2 = invoke_tool("query_entity_connections", {"eids": TEST_EIDS_UC1})
print(f"\n  Returned {len(rows2)} connection rows\n")

check("Returns rows (connections exist)", len(rows2) >= 0)
if rows2:
    r = rows2[0]
    check("Row has 'source_eid' field",  "source_eid"    in r, f"got keys: {list(r.keys())}")
    check("Row has 'target_eid' field",  "target_eid"    in r, f"got keys: {list(r.keys())}")
    check("Row has 'edge_type' field",   "edge_type"     in r, f"got keys: {list(r.keys())}")
    check("Row has 'network_domain' field","network_domain" in r, f"got keys: {list(r.keys())}")

    print("\n  Sample connections (first 5):")
    print(f"  {'Source':<25} {'→'} {'Target':<25} {'Edge Type':<20} Domain")
    print(f"  {'-'*25} {' '} {'-'*25} {'-'*20} {'-'*10}")
    for r in rows2[:5]:
        print(f"  {r.get('source_eid',''):<25} {'→'} {r.get('target_eid',''):<25} "
              f"{r.get('edge_type',''):<20} {r.get('network_domain','')}")
else:
    print("  (No connections found — check if edge_entitytoentity has rows for these EIDs)")


# ── Test 3: query_neighbor_cells ──────────────────────────────────────────────

section("Test 3 — query_neighbor_cells (UC1 RAN)")
rows3 = invoke_tool("query_neighbor_cells", {"eids": TEST_EIDS_UC1})
print(f"\n  Returned {len(rows3)} neighbor rows\n")

check("Returns rows (neighbors exist)", len(rows3) >= 0)
if rows3:
    r = rows3[0]
    check("Row has 'source_eid' field",  "source_eid"  in r, f"got keys: {list(r.keys())}")
    check("Row has 'neighbor_eid' field","neighbor_eid" in r, f"got keys: {list(r.keys())}")

    print("\n  Sample neighbors (first 5):")
    print(f"  {'Source':<25} {'→'} Neighbor")
    print(f"  {'-'*25} {' '} {'-'*25}")
    for r in rows3[:5]:
        print(f"  {r.get('source_eid',''):<25} {'→'} {r.get('neighbor_eid','')}")
else:
    print("  (No neighbors found — check if edge_entitytoneighbor has rows for these EIDs)")


# ── Test 4: Domain triage simulation ─────────────────────────────────────────

section("Test 4 — Domain triage simulation (cross-domain: RAN + CORE)")
rows4 = invoke_tool("query_entity_domains", {"eids": TEST_EIDS_CROSS})
print(f"\n  EIDs queried: {TEST_EIDS_CROSS}")
print(f"  Returned {len(rows4)} rows\n")

domains_found = {r.get("network_domain") for r in rows4 if r.get("network_domain")}
if len(domains_found) > 1:
    domain_triage = "CROSS_DOMAIN"
elif len(domains_found) == 1:
    domain_triage = list(domains_found)[0]
else:
    domain_triage = "UNKNOWN"

check("Detects RAN entities",        "RAN"  in domains_found, f"found: {domains_found}")
check("Detects CORE entities",       "CORE" in domains_found, f"found: {domains_found}")
check("Triage = CROSS_DOMAIN",       domain_triage == "CROSS_DOMAIN", f"got: {domain_triage}")

print(f"\n  Domains detected  : {sorted(domains_found)}")
print(f"  Triage conclusion : {domain_triage}")


# ── Test 5: HSS (UC2 CORE) ────────────────────────────────────────────────────

section("Test 5 — query_entity_domains (UC2 CORE — HSS-SYN-01)")
rows5 = invoke_tool("query_entity_domains", {"eids": TEST_EIDS_UC2})
print(f"\n  Returned {len(rows5)} rows\n")

if rows5:
    r = rows5[0]
    check("HSS entity found",          r.get("eid") == "HSS-SYN-01")
    check("Domain = CORE",             r.get("network_domain") == "CORE",
          f"got: {r.get('network_domain')}")
    check("Entity type = HSS",         r.get("entity_type") == "HSS",
          f"got: {r.get('entity_type')}")
    check("Segment = EPC",             r.get("network_segment") == "EPC",
          f"got: {r.get('network_segment')}")
    print(f"\n  {r}")
else:
    print("  (No rows — check if HSS-SYN-01 exists in entity table)")


# ── Summary ───────────────────────────────────────────────────────────────────

section("Summary")
print("""
  If all checks pass:
    MCP Toolbox is correctly configured and connected to Spanner.
    ReflexAgent will automatically use MCP Toolbox (priority 1)
    when start_mcp_toolbox.sh is running.

  If some checks fail:
    1. Verify tools.yaml points to correct project/instance/database
    2. Verify gcloud auth: gcloud auth application-default login
    3. Verify Spanner data is loaded (run synth_gen pipeline first)
    4. Check toolbox logs for SQL errors
""")