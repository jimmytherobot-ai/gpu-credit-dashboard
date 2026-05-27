#!/usr/bin/env python3
"""
GPU.Credit / USD.AI Dashboard Data Fetcher
Fetches fresh data from USD.AI API, CoinGecko, DeFiLlama, and Allium,
then rewrites JS variable blocks in each HTML chart file.

Run locally: python scripts/fetch_data.py
Run in CI:   ALLIUM_API_KEY=<secret> python scripts/fetch_data.py
"""

import json
import os
import re
import sys
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_DIR   = SCRIPT_DIR.parent
DATA_DIR   = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CACHE_TTL_HOURS = 23

USDAI_API_BASE  = "https://api.usd.ai"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
DEFILLAMA_BASE  = "https://api.llama.fi"
ALLIUM_BASE     = "https://api.allium.so/api/v1/explorer"


# ── Utilities ─────────────────────────────────────────────────────────────────

def cache_path(name):
    return DATA_DIR / f"{name}.json"

def cache_is_fresh(name):
    p = cache_path(name)
    if not p.exists():
        return False
    age_hours = (time.time() - p.stat().st_mtime) / 3600
    return age_hours < CACHE_TTL_HOURS

def load_cache(name):
    p = cache_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return None

def save_cache(name, data):
    cache_path(name).write_text(json.dumps(data, indent=2))
    print(f"  ✓ cached {name}.json")

def fetch_json(url, headers=None, delay=0):
    import urllib.request, urllib.error
    if delay:
        time.sleep(delay)
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "USD.AI-Dashboard/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ✗ GET {url}: {e}")
        return None

def post_json(url, payload, headers=None):
    import urllib.request
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    req.add_header("User-Agent", "USD.AI-Dashboard/1.0")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ✗ POST {url}: {e}")
        return None

def fmt_js_array(arr):
    return json.dumps(arr)

def fmt_js_matrix(rows):
    lines = [json.dumps(row) for row in rows]
    return "[\n" + ",\n".join(lines) + "\n]"

def replace_js_var(html, var_name, new_value):
    """Replace const VAR = [...]; in HTML (handles multiline arrays)."""
    pattern = rf'(const {re.escape(var_name)}\s*=\s*)(\[[\s\S]*?\];)'
    replacement = rf'\g<1>{new_value};'
    result, n = re.subn(pattern, replacement, html)
    if n == 0:
        print(f"    ⚠ Could not find/replace: {var_name}")
    return result

def replace_js_var_loose(html, var_name, new_value):
    """Like replace_js_var but allows trailing spaces in var name."""
    pattern = rf'(const {re.escape(var_name)}\s*=\s*)(\[[\s\S]*?\];)'
    result, n = re.subn(pattern, lambda m: m.group(1) + new_value + ';', html)
    if n == 0:
        print(f"    ⚠ Could not find/replace: {var_name} (loose)")
    return result


# ── Allium ────────────────────────────────────────────────────────────────────

def get_allium_key():
    key = os.environ.get("ALLIUM_API_KEY")
    if key:
        return key
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "jimmy", "-s", "allium-api-key", "-w"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def allium_query(sql, name, api_key):
    """
    Allium Explorer API (v1):
    1. POST /queries with {title, sql, config:{sql, limit}, parameters} → {query_id}
    2. POST /queries/{query_id}/run → returns data directly (synchronous)
    """
    headers = {"X-Api-Key": api_key}
    print(f"  → Creating Allium query: {name}")
    payload = {
        "sql": sql,
        "title": name,
        "name": name,
        "config": {"sql": sql, "limit": 100000},
        "parameters": []
    }
    res = post_json(f"{ALLIUM_BASE}/queries", payload, headers)
    if not res or "query_id" not in res:
        print(f"  ✗ Failed to create query: {res}")
        return None
    query_id = res["query_id"]
    print(f"  → Running query {query_id} (may take up to 2 min)...")
    # Run is synchronous — returns data directly (may be slow)
    run_res = post_json(f"{ALLIUM_BASE}/queries/{query_id}/run", {}, headers)
    if not run_res:
        print(f"  ✗ Run failed")
        return None
    # Synchronous response: {sql, data, meta, queried_at}
    if "data" in run_res:
        return run_res["data"]
    # Async fallback: {run_id} → poll
    run_id = run_res.get("run_id")
    if not run_id:
        print(f"  ✗ Unexpected run response: {list(run_res.keys())}")
        return None
    for attempt in range(60):
        time.sleep(5)
        sr = fetch_json(f"{ALLIUM_BASE}/queries/{query_id}/runs/{run_id}", headers)
        if not sr:
            continue
        status = sr.get("status")
        print(f"  … status={status} (attempt {attempt+1})")
        if status == "success":
            break
        elif status in ("failed", "error", "cancelled"):
            print(f"  ✗ Query {status}")
            return None
    else:
        print("  ✗ Query timed out")
        return None
    results = fetch_json(f"{ALLIUM_BASE}/queries/{query_id}/runs/{run_id}/results", headers)
    if not results or "data" not in results:
        return None
    return results["data"]


# ── Data Fetch Functions ──────────────────────────────────────────────────────

def fetch_usdai_api():
    if cache_is_fresh("usdai_api"):
        print("  ↩ usdai_api cache fresh")
        return load_cache("usdai_api")
    print("Fetching USD.AI API...")
    tvl = fetch_json(f"{USDAI_API_BASE}/usdai/dashboard/tvl")
    apy = fetch_json(f"{USDAI_API_BASE}/usdai/dashboard/current-apy")
    exp_apy = fetch_json(f"{USDAI_API_BASE}/usdai/dashboard/expected-apy")
    data = {
        "tvl": tvl or {},
        "current_apy": (apy or {}).get("result", 7.0),
        "expected_apy": exp_apy or {},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    save_cache("usdai_api", data)
    return data


def fetch_defillama():
    if cache_is_fresh("defillama"):
        print("  ↩ defillama cache fresh")
        return load_cache("defillama")
    print("Fetching DeFiLlama...")
    raw = fetch_json(f"{DEFILLAMA_BASE}/protocol/usd-ai")
    if not raw:
        return load_cache("defillama")
    result = {"chainTvls": {}, "tvl": [], "fetched_at": datetime.now(timezone.utc).isoformat()}
    if "chainTvls" in raw:
        for chain, cd in raw["chainTvls"].items():
            if "tvl" in cd:
                result["chainTvls"][chain] = [
                    {"date": datetime.utcfromtimestamp(p["date"]).strftime("%Y-%m-%d"),
                     "value": p["totalLiquidityUSD"]}
                    for p in cd["tvl"]
                ]
    if "tvl" in raw:
        result["tvl"] = [
            {"date": datetime.utcfromtimestamp(p["date"]).strftime("%Y-%m-%d"),
             "totalLiquidityUSD": p["totalLiquidityUSD"]}
            for p in raw["tvl"]
        ]
    save_cache("defillama", result)
    return result


def fetch_coingecko():
    if cache_is_fresh("coingecko"):
        print("  ↩ coingecko cache fresh")
        return load_cache("coingecko")
    print("Fetching CoinGecko...")
    result = {"usdai": {}, "susdai": {}, "fetched_at": datetime.now(timezone.utc).isoformat()}
    for coin_id in ["usdai", "susdai"]:
        url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart?vs_currency=usd&days=365"
        data = fetch_json(url, delay=2)
        if data:
            result[coin_id] = data
            print(f"  ✓ {coin_id}: {len(data.get('prices', []))} price points")
        else:
            print(f"  ✗ {coin_id} failed")
        time.sleep(2)
    save_cache("coingecko", result)
    return result


def fetch_allium_usdai_holders():
    if cache_is_fresh("allium_usdai_holders"):
        print("  ↩ allium_usdai_holders cache fresh")
        return load_cache("allium_usdai_holders")
    api_key = get_allium_key()
    if not api_key:
        print("  ⚠ No Allium key — skipping usdai holders")
        return load_cache("allium_usdai_holders")
    print("Fetching Allium USDai holders...")
    sql = """
SELECT 
  DATE_TRUNC('day', block_timestamp) AS date,
  CASE 
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x5edcbc20cac67adc2e724d4348ff85132b085b82',
      '0x30ccf4bbee313fcd19f3e295b3ba2920a24e2f62'
    ) THEN 'Pendle'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x1b4ec865915872aec7a30423fda2584c9fa894c5',
      '0xb98eea7132f1de6ec24d4ee4afbdf4d63ef1a9f0',
      '0x8fb5c0896c70b0056a09249ecef7e7ee01f037af',
      '0x75580d4be33c61700969583fdaec566ca84e5b69'
    ) THEN 'Fluid'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x7d9790403fa53ef3e3a3389c259d244bdc61b785',
      '0xaabb9cbac15a3d646dcdc6574bcfcfb989e1fdd8'
    ) THEN 'Euler'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x6c247b1f6182318877311737bac0844baa518f5e'
    ) THEN 'Morpho'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0xf52472420ae96a028863fdc51313693854475581',
      '0xa7cf5543a27badc3a74d51ea0a02e84799140e4e'
    ) THEN 'Curve'
    ELSE 'Individual'
  END AS protocol,
  COUNT(DISTINCT LOWER(CONCAT('0x', SUBSTR(topic1, 27)))) AS unique_holders
FROM arbitrum.raw.logs
WHERE address = '0x0a1a1a107e45b7ced86833863f482bc5f4ed82ef'
  AND topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
  AND block_timestamp >= '2025-05-01'
GROUP BY 1, 2
ORDER BY 1, 2
"""
    data = allium_query(sql, "usdai_holders_by_protocol", api_key)
    if data:
        save_cache("allium_usdai_holders", data)
    return data or load_cache("allium_usdai_holders")


def fetch_allium_susdai_holders():
    if cache_is_fresh("allium_susdai_holders"):
        print("  ↩ allium_susdai_holders cache fresh")
        return load_cache("allium_susdai_holders")
    api_key = get_allium_key()
    if not api_key:
        print("  ⚠ No Allium key — skipping susdai holders")
        return load_cache("allium_susdai_holders")
    print("Fetching Allium sUSDai holders...")
    sql = """
SELECT 
  DATE_TRUNC('day', block_timestamp) AS date,
  CASE 
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x5edcbc20cac67adc2e724d4348ff85132b085b82',
      '0x30ccf4bbee313fcd19f3e295b3ba2920a24e2f62'
    ) THEN 'Pendle'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x1b4ec865915872aec7a30423fda2584c9fa894c5',
      '0xb98eea7132f1de6ec24d4ee4afbdf4d63ef1a9f0',
      '0x8fb5c0896c70b0056a09249ecef7e7ee01f037af',
      '0x75580d4be33c61700969583fdaec566ca84e5b69'
    ) THEN 'Fluid'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x7d9790403fa53ef3e3a3389c259d244bdc61b785',
      '0xaabb9cbac15a3d646dcdc6574bcfcfb989e1fdd8'
    ) THEN 'Euler'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0x6c247b1f6182318877311737bac0844baa518f5e'
    ) THEN 'Morpho'
    WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) IN (
      '0xf52472420ae96a028863fdc51313693854475581',
      '0xa7cf5543a27badc3a74d51ea0a02e84799140e4e'
    ) THEN 'Curve'
    ELSE 'Individual'
  END AS protocol,
  COUNT(DISTINCT LOWER(CONCAT('0x', SUBSTR(topic1, 27)))) AS unique_holders
FROM arbitrum.raw.logs
WHERE address = '0x0b2b2b2076d95dda7817e785989fe353fe955ef9'
  AND topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
  AND block_timestamp >= '2025-05-01'
GROUP BY 1, 2
ORDER BY 1, 2
"""
    data = allium_query(sql, "susdai_holders_by_protocol", api_key)
    if data:
        save_cache("allium_susdai_holders", data)
    return data or load_cache("allium_susdai_holders")


def fetch_allium_mint_burn():
    if cache_is_fresh("allium_mint_burn"):
        print("  ↩ allium_mint_burn cache fresh")
        return load_cache("allium_mint_burn")
    api_key = get_allium_key()
    if not api_key:
        print("  ⚠ No Allium key — skipping mint/burn")
        return load_cache("allium_mint_burn")
    print("Fetching Allium mint/burn...")
    sql = """
SELECT
  DATE_TRUNC('day', block_timestamp) AS date,
  SUM(CASE WHEN LOWER(CONCAT('0x', SUBSTR(topic1, 27))) = '0x0000000000000000000000000000000000000000' 
      THEN TO_NUMBER(RIGHT(SUBSTR(data, 3), 32), 'XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX') / 1e24 ELSE 0 END) AS mints_m,
  SUM(CASE WHEN LOWER(CONCAT('0x', SUBSTR(topic2, 27))) = '0x0000000000000000000000000000000000000000'
      THEN TO_NUMBER(RIGHT(SUBSTR(data, 3), 32), 'XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX') / 1e24 ELSE 0 END) AS burns_m
FROM arbitrum.raw.logs
WHERE address = '0x0a1a1a107e45b7ced86833863f482bc5f4ed82ef'
  AND topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
  AND block_timestamp >= '2026-01-01'
GROUP BY 1
ORDER BY 1
"""
    data = allium_query(sql, "usdai_mint_burn_daily", api_key)
    if data:
        save_cache("allium_mint_burn", data)
    return data or load_cache("allium_mint_burn")


# ── Build Holders Series ──────────────────────────────────────────────────────

PROTOCOL_ORDER = ["Pendle", "Fluid", "Euler", "Morpho", "Curve", "Individual"]
PROTOCOL_COLORS = {
    "Pendle":     "#2AB5A6",
    "Fluid":      "#A75B4D",
    "Euler":      "#4DA2E8",
    "Morpho":     "#8C7AE6",
    "Curve":      "#E85D88",
    "Individual": "#9B9488",
}

def build_holders_series(allium_data):
    """Convert flat Allium rows into DATES + SERIES."""
    if not allium_data:
        return None, None
    # Group by date, then protocol
    by_date = {}
    for row in allium_data:
        d = str(row.get("date", ""))[:10]
        proto = row.get("protocol", "Individual")
        count = int(row.get("unique_holders", 0))
        if d not in by_date:
            by_date[d] = {}
        by_date[d][proto] = by_date[d].get(proto, 0) + count

    dates = sorted(by_date.keys())
    all_protocols = set()
    for day_data in by_date.values():
        all_protocols.update(day_data.keys())

    ordered_protocols = [p for p in PROTOCOL_ORDER if p in all_protocols]
    for p in sorted(all_protocols):
        if p not in ordered_protocols:
            ordered_protocols.append(p)

    series = []
    for proto in ordered_protocols:
        data_arr = [by_date[d].get(proto, 0) for d in dates]
        series.append({
            "label": proto,
            "color": PROTOCOL_COLORS.get(proto, "#888888"),
            "data": data_arr
        })
    return dates, series


# ── HTML Update Functions ─────────────────────────────────────────────────────

def update_tvl_chart(usdai_api, defillama):
    file_path = REPO_DIR / "tvl-chart.html"
    print(f"\nUpdating {file_path.name}...")
    html = file_path.read_text()

    chain_tvls = (defillama or {}).get("chainTvls", {})
    proto_tvl  = (defillama or {}).get("tvl", [])
    current_tvl = (usdai_api or {}).get("tvl", {})

    usdai_by_date  = {}
    susdai_by_date = {}

    for chain_name, chain_data in chain_tvls.items():
        cn = chain_name.lower()
        for point in chain_data:
            d, v = point["date"], point["value"]
            if "susdai" in cn or "susd" in cn:
                susdai_by_date[d] = susdai_by_date.get(d, 0) + v
            else:
                usdai_by_date[d]  = usdai_by_date.get(d, 0) + v

    if not usdai_by_date and proto_tvl:
        total_now  = max(current_tvl.get("mintedUsdai", 1), 1)
        usdai_now  = current_tvl.get("usdaiTvl", 0)
        susdai_now = current_tvl.get("sUsdaiTvl", 0)
        ratio_u = usdai_now / total_now
        ratio_s = susdai_now / total_now
        for point in proto_tvl:
            d, t = point["date"], point["totalLiquidityUSD"]
            usdai_by_date[d]  = round(t * ratio_u)
            susdai_by_date[d] = round(t * ratio_s)

    all_dates = sorted(set(usdai_by_date) | set(susdai_by_date))

    # Append today's snapshot if newer
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if current_tvl and (not all_dates or today > all_dates[-1]):
        all_dates.append(today)
        usdai_by_date[today]  = current_tvl.get("usdaiTvl", 0)
        susdai_by_date[today] = current_tvl.get("sUsdaiTvl", 0)

    labels      = all_dates
    usdai_vals  = [usdai_by_date.get(d, 0) for d in all_dates]
    susdai_vals = [susdai_by_date.get(d, 0) for d in all_dates]

    html = replace_js_var(html, "labels", fmt_js_array(labels))
    html = replace_js_var_loose(html, "usdai ", fmt_js_array(usdai_vals))
    html = replace_js_var(html, "susdai", fmt_js_array(susdai_vals))
    file_path.write_text(html)
    print(f"  ✓ {len(labels)} data points")


def update_tvl_by_chain(defillama, usdai_api):
    for fname in ["usdai-tvl-by-chain.html", "usdai-tvl-by-chain-relative.html"]:
        file_path = REPO_DIR / fname
        print(f"\nUpdating {fname}...")
        html = file_path.read_text()

        chain_tvls = (defillama or {}).get("chainTvls", {})
        current_tvl = (usdai_api or {}).get("tvl", {})

        # Map DeFiLlama chain keys to column indices
        col_order = [
            ("usdai_arb",   lambda cn: "arbitrum" in cn and "susdai" not in cn and "susd" not in cn),
            ("usdai_base",  lambda cn: "base" in cn and "susdai" not in cn and "susd" not in cn),
            ("usdai_plas",  lambda cn: "plasma" in cn and "susdai" not in cn and "susd" not in cn),
            ("susdai_arb",  lambda cn: "arbitrum" in cn and ("susdai" in cn or "susd" in cn)),
            ("susdai_base", lambda cn: "base" in cn and ("susdai" in cn or "susd" in cn)),
            ("susdai_plas", lambda cn: "plasma" in cn and ("susdai" in cn or "susd" in cn)),
        ]

        series_by_col = {col[0]: {} for col in col_order}
        all_dates_set = set()

        for chain_name, chain_data in chain_tvls.items():
            cn = chain_name.lower()
            for col_name, matcher in col_order:
                if matcher(cn):
                    for point in chain_data:
                        d, v = point["date"], point["value"]
                        all_dates_set.add(d)
                        series_by_col[col_name][d] = series_by_col[col_name].get(d, 0) + v
                    break

        if not all_dates_set and current_tvl:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            all_dates_set = {today}
            series_by_col["usdai_arb"][today]  = current_tvl.get("usdaiTvl", 0)
            series_by_col["susdai_arb"][today] = current_tvl.get("sUsdaiTvl", 0)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if current_tvl and (not all_dates_set or today > max(all_dates_set)):
            all_dates_set.add(today)

        all_dates = sorted(all_dates_set)

        raw_rows = []
        for d in all_dates:
            row = [d] + [series_by_col[col[0]].get(d, 0) for col in col_order] + [0]
            raw_rows.append(row)

        html = replace_js_var(html, "raw", fmt_js_matrix(raw_rows))
        file_path.write_text(html)
        print(f"  ✓ {len(raw_rows)} rows")


def update_proof_of_reserves(usdai_api, defillama):
    file_path = REPO_DIR / "usdai-proof-of-reserves.html"
    print(f"\nUpdating {file_path.name}...")
    html = file_path.read_text()

    current_tvl = (usdai_api or {}).get("tvl", {})
    proto_tvl   = (defillama or {}).get("tvl", [])

    stable_now = current_tvl.get("stablecoinReserves", 0)
    loans_now  = current_tvl.get("loansReserves", 0)
    total_now  = max(stable_now + loans_now, 1)

    # Build historical rows from DeFiLlama + current ratios
    ratio_stable = stable_now / total_now
    ratio_loans  = loans_now / total_now

    rows = []
    for point in proto_tvl:
        d = point["date"]
        t = point["totalLiquidityUSD"]
        stable_B = round(t * ratio_stable / 1e9, 4)
        loans_B  = round(t * ratio_loans  / 1e9, 4)
        total_B  = round(t / 1e9, 4)
        rows.append([d, stable_B, loans_B, total_B])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not rows or today > rows[-1][0]:
        rows.append([
            today,
            round(stable_now / 1e9, 4),
            round(loans_now  / 1e9, 4),
            round(total_now  / 1e9, 4),
        ])

    html = replace_js_var(html, "rawRows", fmt_js_matrix(rows))
    file_path.write_text(html)
    print(f"  ✓ {len(rows)} rows")


def update_collateral_mix(usdai_api, defillama):
    file_path = REPO_DIR / "usdai-collateral-mix.html"
    print(f"\nUpdating {file_path.name}...")
    html = file_path.read_text()

    current_tvl = (usdai_api or {}).get("tvl", {})
    proto_tvl   = (defillama or {}).get("tvl", [])

    stable_now = current_tvl.get("stablecoinReserves", 0)
    loans_now  = current_tvl.get("loansReserves", 0)
    total_now  = max(stable_now + loans_now, 1)
    ratio_s = stable_now / total_now
    ratio_l = loans_now  / total_now

    rows = []
    for point in proto_tvl:
        d = point["date"]
        t = point["totalLiquidityUSD"]
        stable_B = round(t * ratio_s / 1e9, 4)
        loans_B  = round(t * ratio_l / 1e9, 4)
        rows.append([d, stable_B, loans_B, 0])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not rows or today > rows[-1][0]:
        rows.append([
            today,
            round(stable_now / 1e9, 4),
            round(loans_now  / 1e9, 4),
            0,
        ])

    html = replace_js_var(html, "rawRows", fmt_js_matrix(rows))
    file_path.write_text(html)
    print(f"  ✓ {len(rows)} rows")


def update_holders_over_time_ex_protocol(allium_usdai):
    file_path = REPO_DIR / "usdai-holders-over-time-ex-protocol.html"
    print(f"\nUpdating {file_path.name}...")
    if not allium_usdai:
        print("  ⚠ No Allium data — skipping")
        return
    html = file_path.read_text()
    dates, series = build_holders_series(allium_usdai)
    if not dates:
        print("  ⚠ Empty dates — skipping")
        return
    # Format SERIES as JS array of objects
    series_js_items = []
    for s in series:
        series_js_items.append(json.dumps({
            "label": s["label"],
            "color": s["color"],
            "data": s["data"]
        }))
    series_js = "[\n" + ",\n".join(series_js_items) + "\n]"
    html = replace_js_var(html, "DATES", fmt_js_array(dates))
    html = replace_js_var(html, "SERIES", series_js)
    file_path.write_text(html)
    print(f"  ✓ {len(dates)} dates, {len(series)} series")


def update_susdai_holders_over_time(allium_susdai):
    for fname in ["susdai-holders-over-time.html", "susdai-holders-over-time-relative.html"]:
        file_path = REPO_DIR / fname
        print(f"\nUpdating {fname}...")
        if not allium_susdai:
            print("  ⚠ No Allium data — skipping")
            continue
        html = file_path.read_text()
        dates, series = build_holders_series(allium_susdai)
        if not dates:
            print("  ⚠ Empty dates — skipping")
            continue
        series_js_items = []
        for s in series:
            series_js_items.append(json.dumps({
                "label": s["label"],
                "color": s["color"],
                "data": s["data"]
            }))
        series_js = "[\n" + ",\n".join(series_js_items) + "\n]"
        html = replace_js_var(html, "DATES", fmt_js_array(dates))
        html = replace_js_var(html, "SERIES", series_js)
        file_path.write_text(html)
        print(f"  ✓ {len(dates)} dates, {len(series)} series")


def update_cumulative_supply(allium_mint_burn):
    file_path = REPO_DIR / "usdai-cumulative-supply.html"
    print(f"\nUpdating {file_path.name}...")
    if not allium_mint_burn:
        print("  ⚠ No Allium data — skipping")
        return
    html = file_path.read_text()
    rows = []
    for row in allium_mint_burn:
        d = str(row.get("date", ""))[:10]
        mints = round(float(row.get("mints_m", 0)), 4)
        burns = round(float(row.get("burns_m", 0)), 4)
        rows.append([d, mints, burns])
    if rows:
        html = replace_js_var(html, "raw", fmt_js_matrix(rows))
        file_path.write_text(html)
        print(f"  ✓ {len(rows)} rows")


def update_mint_burn(allium_mint_burn):
    file_path = REPO_DIR / "usdai-mint-burn.html"
    print(f"\nUpdating {file_path.name}...")
    if not allium_mint_burn:
        print("  ⚠ No Allium data — skipping")
        return
    html = file_path.read_text()
    rows = []
    for row in allium_mint_burn:
        d = str(row.get("date", ""))[:10]
        mints = round(float(row.get("mints_m", 0)), 4)
        burns = round(float(row.get("burns_m", 0)), 4)
        net   = round(mints - burns, 4)
        rows.append([d, mints, burns, net])
    if rows:
        html = replace_js_var(html, "raw", fmt_js_matrix(rows))
        file_path.write_text(html)
        print(f"  ✓ {len(rows)} rows")


def update_usdai_peg_deviation(coingecko):
    file_path = REPO_DIR / "usdai-peg-deviation.html"
    print(f"\nUpdating {file_path.name}...")
    cg_data = (coingecko or {}).get("usdai", {})
    prices  = cg_data.get("prices", [])
    volumes = cg_data.get("total_volumes", [])
    if not prices:
        print("  ⚠ No CoinGecko USDai data — skipping")
        return
    html = file_path.read_text()
    # Aggregate to daily (CoinGecko returns hourly for recent data)
    daily = {}
    for p in prices:
        d = datetime.utcfromtimestamp(p[0] / 1000).strftime("%Y-%m-%d")
        daily.setdefault(d, []).append(p[1])
    daily_vol = {}
    for v in volumes:
        d = datetime.utcfromtimestamp(v[0] / 1000).strftime("%Y-%m-%d")
        daily_vol.setdefault(d, []).append(v[1])

    rows = []
    for d in sorted(daily.keys()):
        avg_price = sum(daily[d]) / len(daily[d])
        dev = round((avg_price - 1.0) * 100, 4)
        vol = round(sum(daily_vol.get(d, [0])), 2)
        rows.append([d, dev, vol])

    html = replace_js_var(html, "raw", fmt_js_matrix(rows))
    file_path.write_text(html)
    print(f"  ✓ {len(rows)} rows")


def update_susdai_apy(coingecko, usdai_api):
    file_path = REPO_DIR / "susdai-apy.html"
    print(f"\nUpdating {file_path.name}...")
    html = file_path.read_text()

    cg_data = (coingecko or {}).get("susdai", {})
    prices  = cg_data.get("prices", [])

    current_apy = (usdai_api or {}).get("current_apy", 7.0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not prices:
        print("  ⚠ No CoinGecko sUSDai data — using current APY only")
        rows = [[today, round(float(current_apy), 2)]]
    else:
        # Aggregate to daily
        daily = {}
        for p in prices:
            d = datetime.utcfromtimestamp(p[0] / 1000).strftime("%Y-%m-%d")
            daily.setdefault(d, []).append(p[1])
        dates_sorted = sorted(daily.keys())
        rows = []
        prev_price = None
        for d in dates_sorted:
            avg_price = sum(daily[d]) / len(daily[d])
            if prev_price is not None and prev_price > 0:
                daily_return = (avg_price / prev_price) - 1
                annualized_apy = round(daily_return * 365 * 100, 2)
                # Clamp to reasonable range
                annualized_apy = max(-100, min(200, annualized_apy))
                rows.append([d, annualized_apy])
            prev_price = avg_price

        # Append today with current APY if newer
        if not rows or today > rows[-1][0]:
            rows.append([today, round(float(current_apy), 2)])

    html = replace_js_var(html, "raw", fmt_js_matrix(rows))
    file_path.write_text(html)
    print(f"  ✓ {len(rows)} rows, current APY={current_apy}%")


def update_susdai_peg_deviation(coingecko):
    file_path = REPO_DIR / "susdai-peg-deviation.html"
    print(f"\nUpdating {file_path.name}...")
    cg_data = (coingecko or {}).get("susdai", {})
    prices  = cg_data.get("prices", [])
    volumes = cg_data.get("total_volumes", [])
    if not prices:
        print("  ⚠ No CoinGecko sUSDai data — skipping")
        return
    html = file_path.read_text()

    # Aggregate to daily
    daily = {}
    for p in prices:
        d = datetime.utcfromtimestamp(p[0] / 1000).strftime("%Y-%m-%d")
        daily.setdefault(d, []).append(p[1])
    daily_vol = {}
    for v in volumes:
        d = datetime.utcfromtimestamp(v[0] / 1000).strftime("%Y-%m-%d")
        daily_vol.setdefault(d, []).append(v[1])

    dates_sorted = sorted(daily.keys())
    if not dates_sorted:
        return

    # sUSDai expected price: grows with APY (~7% annualized)
    # Deviation = (actual - expected) / expected * 100
    # We use the first price as baseline and project forward
    first_price = sum(daily[dates_sorted[0]]) / len(daily[dates_sorted[0]])
    base_date   = datetime.strptime(dates_sorted[0], "%Y-%m-%d")
    ASSUMED_APY = 0.07  # 7% annual as baseline

    rows = []
    for d in dates_sorted:
        avg_price = sum(daily[d]) / len(daily[d])
        dt = datetime.strptime(d, "%Y-%m-%d")
        days_elapsed = (dt - base_date).days
        expected_price = first_price * ((1 + ASSUMED_APY) ** (days_elapsed / 365))
        if expected_price > 0:
            dev = round((avg_price - expected_price) / expected_price * 100, 4)
        else:
            dev = 0.0
        vol = round(sum(daily_vol.get(d, [0])), 2)
        rows.append([d, dev, vol])

    html = replace_js_var(html, "raw", fmt_js_matrix(rows))
    file_path.write_text(html)
    print(f"  ✓ {len(rows)} rows")


# usdai-holder-distribution.html: keep existing hardcoded data (complex to recompute)
def skip_holder_distribution():
    print("\nSkipping usdai-holder-distribution.html (hardcoded, requires complex recomputation)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("USD.AI Dashboard Data Fetcher")
    print(f"Run time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # 1. Fetch all data sources
    print("\n── Fetching data ──")
    usdai_api     = fetch_usdai_api()
    defillama     = fetch_defillama()
    coingecko     = fetch_coingecko()
    allium_usdai  = fetch_allium_usdai_holders()
    allium_susdai = fetch_allium_susdai_holders()
    allium_mb     = fetch_allium_mint_burn()

    # 2. Update all HTML files
    print("\n── Updating HTML files ──")
    update_tvl_chart(usdai_api, defillama)
    update_tvl_by_chain(defillama, usdai_api)
    update_proof_of_reserves(usdai_api, defillama)
    update_collateral_mix(usdai_api, defillama)
    update_holders_over_time_ex_protocol(allium_usdai)
    skip_holder_distribution()
    update_cumulative_supply(allium_mb)
    update_mint_burn(allium_mb)
    update_usdai_peg_deviation(coingecko)
    update_susdai_apy(coingecko, usdai_api)
    update_susdai_holders_over_time(allium_susdai)
    update_susdai_peg_deviation(coingecko)

    print("\n" + "=" * 60)
    print("✓ Dashboard update complete")

    # Report which charts need Allium
    allium_key = get_allium_key()
    if not allium_key:
        print("\n⚠ Charts that need Allium (add ALLIUM_API_KEY secret to GitHub):")
        print("  - usdai-holders-over-time-ex-protocol.html")
        print("  - susdai-holders-over-time.html")
        print("  - susdai-holders-over-time-relative.html")
        print("  - usdai-cumulative-supply.html")
        print("  - usdai-mint-burn.html")
    print("=" * 60)


if __name__ == "__main__":
    main()
