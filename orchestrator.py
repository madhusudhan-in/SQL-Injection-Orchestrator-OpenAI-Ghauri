import requests
import subprocess
import openai
import yaml
import pandas as pd
import time
from concurrent.futures import ThreadPoolExecutor

# --- CONFIGURATION ---

OPENAI_API_KEY = "sk-..."  # Set your OpenAI API key
openai.api_key = OPENAI_API_KEY

ENDPOINTS_FILE = "endpoints.yaml"  # List of endpoints to scan
WAF_LOG = "waf.log"
DB_LOG = "db_audit.log"

# --- LOAD ENDPOINTS ---
def load_endpoints(file_path):
    with open(file_path, "r") as f:
        return yaml.safe_load(f)

# --- LLM PAYLOAD GENERATION ---
def get_smart_payload(endpoint, response_text):
    prompt = (
        f"You are a security expert. Given the following API endpoint and its response, generate 3 advanced SQL injection payloads that could be used to test for vulnerabilities. "
        f"Make them context-aware and try to bypass common WAFs. "
        f"Endpoint: {endpoint}\n"
        f"Response: {response_text[:500]}\n"
        f"Payloads:"
    )
    try:
        completion = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        # Parse payloads from the response (one per line)
        return [line.strip() for line in completion.choices[0].message['content'].split('\n') if line.strip()]
    except Exception as e:
        print(f"[!] LLM error for {endpoint}: {e}")
        return []

# --- RUN GHAURI ---
def run_ghauri(url, payload):
    cmd = ["ghauri", "-u", url, "--data", payload]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.stdout
    except Exception as e:
        return f"Error: {e}"

# --- SCAN FUNCTION ---
def scan_endpoint(endpoint):
    url = endpoint["url"]
    params = endpoint.get("params", "")
    method = endpoint.get("method", "GET").upper()
    print(f"[*] Probing {url} ...")
    # 1. Probe endpoint for baseline response
    try:
        if method == "POST":
            resp = requests.post(url, data=params, timeout=10)
        else:
            resp = requests.get(url, params=params, timeout=10)
        response_text = resp.text[:1000]  # Truncate for prompt
    except Exception as e:
        return {"url": url, "error": str(e)}
    # 2. Get LLM-generated payloads
    payloads = get_smart_payload(url, response_text)
    results = []
    for payload in payloads:
        print(f"    [+] Testing payload: {payload}")
        output = run_ghauri(url, payload)
        results.append({"payload": payload, "output": output})
    return {"url": url, "results": results}

# --- PARALLEL SCANNING ---
def run_scans(endpoints):
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(scan_endpoint, ep) for ep in endpoints]
        return [f.result() for f in futures]

# --- TELEMETRY CORRELATION ---
def correlate_logs(scan_results, waf_log_path, db_log_path):
    try:
        waf = pd.read_csv(waf_log_path)
    except Exception as e:
        print(f"[!] Could not read WAF log: {e}")
        waf = pd.DataFrame()
    try:
        db = pd.read_csv(db_log_path)
    except Exception as e:
        print(f"[!] Could not read DB log: {e}")
        db = pd.DataFrame()
    for result in scan_results:
        url = result.get('url')
        print(f"\n[+] Results for {url}:")
        for r in result.get("results", []):
            payload = r.get('payload')
            print(f"Payload: {payload}")
            print(f"Output: {r.get('output', r.get('error'))}\n")
            # Find WAF log entries for this payload
            if not waf.empty and 'request' in waf.columns:
                waf_matches = waf[waf['request'].astype(str).str.contains(str(payload), na=False)]
                if not waf_matches.empty:
                    print("  [!] WAF log matches:")
                    print(waf_matches)
            # Find DB log entries for this payload
            if not db.empty and 'query' in db.columns:
                db_matches = db[db['query'].astype(str).str.contains(str(payload), na=False)]
                if not db_matches.empty:
                    print("  [!] DB log matches:")
                    print(db_matches)

# --- MAIN ---
if __name__ == "__main__":
    print("[=] Loading endpoints...")
    endpoints = load_endpoints(ENDPOINTS_FILE)
    print(f"[=] Loaded {len(endpoints)} endpoints.")
    scan_results = run_scans(endpoints)
    print("[=] Correlating scan results with WAF and DB logs...")
    correlate_logs(scan_results, WAF_LOG, DB_LOG)
    print("[=] Done.")