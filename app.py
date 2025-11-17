# app.py
import os
import re
import json
import time
import base64
import requests
from urllib.parse import urljoin, urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

# Keep same secret used in the form (or set QUIZ_SECRET env var on Render)
SECRET = os.environ.get("QUIZ_SECRET", "TDS24f1000999-LLM-Quiz-2025!")

# Regexes / helpers
BASE64_RE = re.compile(r"([A-Za-z0-9+/=]{80,})")
CSV_LINK_RE = re.compile(r'href=["\']([^"\']+\.(csv|txt|json|zip))["\']', re.IGNORECASE)
PDF_LINK_RE = re.compile(r'href=["\']([^"\']+\.pdf)["\']', re.IGNORECASE)
SUBMIT_RE = re.compile(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", re.IGNORECASE)
JSON_BLOCK_RE = re.compile(r"(\{[\s\S]{1,4000}\})", re.DOTALL)

# timeouts & limits
REQ_TIMEOUT = 20
MAX_LOOP = 8

# --- small solvers for common task types ---
def try_decode_first_base64(text):
    for m in BASE64_RE.finditer(text):
        cand = m.group(1)
        try:
            dec = base64.b64decode(cand).decode("utf-8", errors="ignore")
        except Exception:
            continue
        # return dec if it contains "answer" or "submit" or looks like JSON
        if "answer" in dec or "submit" in dec or dec.strip().startswith("{"):
            return dec
    return None

def try_extract_json_from_text(text):
    m = JSON_BLOCK_RE.search(text)
    if not m:
        return None
    js = m.group(1)
    try:
        return json.loads(js)
    except Exception:
        return None

def sum_value_column_from_csv_text(csv_text, colname="value"):
    import csv, io
    rdr = csv.DictReader(io.StringIO(csv_text))
    total = 0.0
    found = False
    for r in rdr:
        if colname in r:
            found = True
            try:
                total += float(r[colname])
            except Exception:
                # try to strip commas
                try:
                    total += float(r[colname].replace(",", ""))
                except Exception:
                    pass
    if found:
        # if integer-like, return int
        if abs(total - round(total)) < 1e-9:
            return int(round(total))
        return total
    return None

def fetch_text(url):
    headers = {"User-Agent": "LLM-Quiz-Agent/1.0"}
    r = requests.get(url, headers=headers, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.text

def absolute_link(base, link):
    if link.startswith("http://") or link.startswith("https://"):
        return link
    return urljoin(base, link)

def find_first_submit_url(text):
    m = SUBMIT_RE.search(text)
    if m:
        return m.group(0)
    return None

def find_any_link_to_file(text, base_url):
    m = CSV_LINK_RE.search(text)
    if m:
        return absolute_link(base_url, m.group(1))
    m = PDF_LINK_RE.search(text)
    if m:
        return absolute_link(base_url, m.group(1))
    # also try plain links
    m = re.search(r'href=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        return absolute_link(base_url, m.group(1))
    return None

# a simple main heuristic solver for one page
def solve_from_page_text(page_text, page_url):
    """
    Return an 'answer' (primitive or str), and optionally debug info.
    """
    # 1) Try decode base64 embedded JSON payload (demo uses this)
    decoded = try_decode_first_base64(page_text)
    if decoded:
        # try parse JSON inside decoded
        parsed = try_extract_json_from_text(decoded)
        if parsed and "answer" in parsed:
            return parsed["answer"], {"method": "decoded_base64_json", "parsed": parsed}
        # sometimes decoded is itself plain text answer
        if decoded.strip() and len(decoded.strip()) < 500:
            return decoded.strip(), {"method": "decoded_base64_text", "decoded_preview": decoded[:200]}

    # 2) Try find JSON-like block on page
    parsed2 = try_extract_json_from_text(page_text)
    if parsed2 and "answer" in parsed2:
        return parsed2["answer"], {"method": "page_embedded_json", "parsed": parsed2}

    # 3) Look for explicit instructions: "sum of the "value" column in the table on page 2" etc.
    # If CSV link present, fetch and try to sum 'value' column
    file_link = find_any_link_to_file(page_text, page_url)
    if file_link and file_link.lower().endswith(".csv"):
        try:
            txt = fetch_text(file_link)
            s = sum_value_column_from_csv_text(txt, "value")
            if s is not None:
                return s, {"method": "csv_sum", "file": file_link}
        except Exception as e:
            pass

    # 4) If the page contains "Reverse" or "reverse" and a quoted string, attempt reverse
    m = re.search(r"reverse[^\\n]*['\"]([^'\"]+)['\"]", page_text, re.IGNORECASE)
    if m:
        s = m.group(1)[::-1]
        return s, {"method": "reverse_string", "input": m.group(1)}

    # 5) Common pattern: "What is the sum of the \"value\" column" (maybe with link to PDF)
    m = re.search(r"sum of the [\"']?value[\"']? column", page_text, re.IGNORECASE)
    if m:
        if file_link and file_link.lower().endswith(".csv"):
            try:
                txt = fetch_text(file_link)
                s = sum_value_column_from_csv_text(txt, "value")
                if s is not None:
                    return s, {"method": "csv_sum_detected", "file": file_link}
            except Exception:
                pass

    # 6) If nothing matched, try a fallback: look for any small number in the page likely to be answer
    mnum = re.search(r"answer[:\s]*([0-9]+(?:\.[0-9]+)?)", page_text, re.IGNORECASE)
    if mnum:
        val = mnum.group(1)
        try:
            if "." in val:
                return float(val), {"method": "found_number_in_text", "value": val}
            else:
                return int(val), {"method": "found_number_in_text", "value": val}
        except Exception:
            pass

    # 7) If absolutely nothing, return a minimal "I don't know" (the initial POST can be anything per forum)
    return "", {"method": "fallback_empty_answer"}

# --- main engine: loop over quiz endpoints ---
def post_to_submit(submit_url, payload):
    headers = {"Content-Type": "application/json"}
    r = requests.post(submit_url, json=payload, headers=headers, timeout=REQ_TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"text": r.text}

@app.route("/", methods=["GET"])
def home():
    return "LLM Quiz API running."

@app.route("/quiz", methods=["POST"])
def quiz_entrypoint():
    # 1) Validate JSON
    if not request.is_json:
        return jsonify({"error": "invalid json"}), 400
    data = request.get_json()

    # 2) Required fields
    email = data.get("email")
    secret = data.get("secret")
    start_url = data.get("url")
    if not email or not secret or not start_url:
        return jsonify({"error": "missing fields: email, secret, url required"}), 400

    # 3) Secret check
    if secret != SECRET:
        return jsonify({"error": "forbidden"}), 403

    timeline = []
    current_url = start_url
    # If the provided URL looks like demo, first submit should be to /submit
    if current_url.endswith("/demo") or current_url.endswith("/demo/"):
        submit_url = current_url.replace("/demo", "/submit")
    else:
        # try to guess submit url by replacing path end with /submit
        parsed = urlparse(current_url)
        submit_url = f"{parsed.scheme}://{parsed.netloc}/submit"

    # loop
    for i in range(MAX_LOOP):
        loop_item = {"iteration": i+1, "current_url": current_url, "submit_url": submit_url}
        try:
            # fetch the page (GET) to inspect content and attempt to solve
            page_text = ""
            try:
                page_text = fetch_text(current_url)
                loop_item["fetched_page"] = True
            except Exception as e:
                loop_item["fetched_page"] = False
                loop_item["fetch_error"] = str(e)

            # attempt to derive an answer from the page
            answer, debug = solve_from_page_text(page_text, current_url)
            loop_item["solver_debug"] = debug
            loop_item["answer"] = answer

            # build submission payload required by quiz
            submit_payload = {
                "email": email,
                "secret": secret,
                "url": current_url,
                "answer": answer
            }

            # POST to submit_url
            status, resp = post_to_submit(submit_url, submit_payload)
            loop_item["submit_http_status"] = status
            loop_item["submit_response"] = resp
            timeline.append(loop_item)

            # If resp contains a new URL to continue, use it; break if none
            next_url = None
            if isinstance(resp, dict):
                # many responses include "url" or "next" or "submit_url"
                next_url = resp.get("url") or resp.get("next") or resp.get("submit_url") or resp.get("endpoint")
            # fallback: see if resp text contains a URL
            if not next_url and isinstance(resp, dict):
                # check top-level text fields
                for v in resp.values():
                    if isinstance(v, str) and v.startswith("http"):
                        next_url = v
                        break

            if not next_url:
                # also try to parse from page_text if server returned a page (rare)
                m = re.search(r"https?://[^\s'\"<>]+/task[^\s'\"<>]*", json.dumps(resp) if not isinstance(resp, str) else resp, re.IGNORECASE)
                if m:
                    next_url = m.group(0)

            if not next_url:
                # no more tasks — finish
                return jsonify({"status": "done", "timeline": timeline}), 200

            # otherwise prepare for next iteration
            current_url = next_url
            # Derive submit_url for the next resource: prefer same domain /submit path
            parsed = urlparse(current_url)
            submit_url = f"{parsed.scheme}://{parsed.netloc}/submit"

            # tiny delay to behave politely
            time.sleep(0.3)
            continue

        except Exception as e:
            loop_item["error"] = str(e)
            timeline.append(loop_item)
            return jsonify({"status": "error", "timeline": timeline}), 500

    # If loop limit reached
    return jsonify({"status": "max_iterations_reached", "timeline": timeline}), 200

if __name__ == "__main__":
    # Local run (Render uses gunicorn)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
