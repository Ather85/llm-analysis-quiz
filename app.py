# app.py
import os
import time
import json
import requests
from urllib.parse import urlparse, urljoin
from flask import Flask, request, jsonify

app = Flask(__name__)

# Use env var on Render for safety, fallback to the value you used in the form
QUIZ_SECRET = os.environ.get("QUIZ_SECRET", "TDS24f1000999-LLM-Quiz-2025!")
BASE_SUBMIT_HOST = "tds-llm-analysis.s-anand.net"
REQ_TIMEOUT = 20
MAX_ITER = 8

def is_valid_quiz_domain(url):
    try:
        p = urlparse(url)
        return p.netloc.endswith(BASE_SUBMIT_HOST)
    except:
        return False

def post_submit(submit_payload):
    try:
        r = requests.post(f"https://{BASE_SUBMIT_HOST}/submit", json=submit_payload, timeout=REQ_TIMEOUT)
        # try to parse JSON, otherwise return text
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"text": r.text}
    except Exception as e:
        return None, {"error": "submit_failed", "details": str(e)}

def fetch_text(url):
    r = requests.get(url, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.text

def compute_csv_cutoff_sum(csv_text):
    # CSV: first row contains cutoff, rest are numeric rows in first column
    # tolerant parsing
    import csv, io
    s = 0
    f = io.StringIO(csv_text)
    rdr = csv.reader(f)
    try:
        first = next(rdr)
        cutoff = int(first[0])
    except Exception:
        return None, "invalid_csv_or_cutoff"
    total = 0
    for row in rdr:
        if not row:
            continue
        try:
            val = int(row[0])
        except Exception:
            try:
                val = int(float(row[0]))
            except Exception:
                continue
        if val >= cutoff:
            total += val
    return total, None

def derive_data_url(task_url, from_type):
    """
    Convert:
    - .../demo-scrape?... -> .../demo-scrape-data?...
    - .../demo-audio?... -> .../demo-audio-data?...
    """
    if "demo-scrape" in task_url and "demo-scrape-data" not in task_url:
        return task_url.replace("demo-scrape", "demo-scrape-data")
    if "demo-audio" in task_url and "demo-audio-data" not in task_url:
        return task_url.replace("demo-audio", "demo-audio-data")
    return None

@app.route("/", methods=["GET"])
def home():
    return "LLM Quiz API running."

@app.route("/quiz", methods=["POST"])
def quiz_entry():
    # Validate JSON
    if not request.is_json:
        return jsonify({"error": "invalid json"}), 400
    data = request.get_json()

    # Required fields
    email = data.get("email")
    secret = data.get("secret")
    start_url = data.get("url")
    if not email or not secret or not start_url:
        return jsonify({"error": "missing fields: email, secret, url required"}), 400

    # Check secret
    if secret != QUIZ_SECRET:
        return jsonify({"error": "forbidden"}), 403

    # The grader will send you a start URL (usually /demo). We'll follow the multi-step loop.
    timeline = []
    current_url = start_url

    # For the first submission the grader expects you to send result to /submit (server side)
    # We'll create submit payloads and post to https://tds-llm-analysis.s-anand.net/submit
    for i in range(MAX_ITER):
        step = {"iteration": i+1, "current_url": current_url}
        # build the payload to send to the grader submit endpoint
        submit_payload = {"email": email, "secret": secret, "url": current_url}

        # determine what to send as "answer"
        answer = ""
        try:
            # If this is the start (/demo) we can send a default "start" answer (like manual flow)
            if current_url.endswith("/demo") or current_url.endswith("/demo/") or "/demo" in current_url:
                answer = "start"
                step["reason"] = "start-step"
            else:
                # Inspect the current URL to decide how to find the answer
                # If it's a scrape page, fetch its associated data page
                if "demo-scrape" in current_url:
                    data_url = derive_data_url(current_url, "scrape")
                    if data_url:
                        txt = fetch_text(data_url)
                        # scraped secret code is page raw text
                        secret_code = txt.strip()
                        answer = secret_code
                        step["method"] = "scrape-data"
                        step["data_url"] = data_url
                    else:
                        # fallback: fetch page and try to find JSON / small secret
                        txt = fetch_text(current_url)
                        answer = txt.strip()[:200]
                        step["method"] = "scrape-fallback"
                elif "demo-audio" in current_url:
                    # fetch CSV-like data page: demo-audio-data
                    data_url = derive_data_url(current_url, "audio")
                    if not data_url:
                        # fallback: try direct fetch
                        data_url = current_url
                    txt = fetch_text(data_url)
                    total, err = compute_csv_cutoff_sum(txt)
                    if err:
                        step["error"] = err
                        answer = ""
                    else:
                        answer = str(total)
                        step["method"] = "audio-csv-sum"
                        step["computed_sum"] = total
                        step["data_url"] = data_url
                else:
                    # Generic: attempt to fetch page and look for embedded JSON answer
                    txt = fetch_text(current_url)
                    # quick heuristic: find digits or an "answer" JSON
                    # try parse JSON block
                    import re
                    m = re.search(r"\{[\s\S]{0,2000}\}", txt)
                    if m:
                        try:
                            j = json.loads(m.group(0))
                            if "answer" in j:
                                answer = j["answer"]
                                step["method"] = "embedded-json"
                            else:
                                # fallback to first small string
                                answer = str(m.group(0))[:200]
                        except Exception:
                            # fallback: first number found
                            mm = re.search(r"([0-9]{3,})", txt)
                            answer = mm.group(1) if mm else ""
                    else:
                        mm = re.search(r"([0-9]{3,})", txt)
                        answer = mm.group(1) if mm else ""
                        step["method"] = "heuristic-number"
            # attach answer to payload
            submit_payload["answer"] = answer
            step["answer_sent"] = answer

            # POST to the grader submit endpoint
            status, resp = post_submit(submit_payload)
            step["submit_status"] = status
            step["submit_response"] = resp
            timeline.append(step)

            # check response for next URL or finish
            next_url = None
            if isinstance(resp, dict):
                next_url = resp.get("url") or resp.get("next") or resp.get("endpoint")
            # fallback: if text contains a url
            if not next_url and isinstance(resp, dict):
                # scan values
                for v in resp.values():
                    if isinstance(v, str) and v.startswith("http"):
                        next_url = v
                        break

            if not next_url:
                # finished
                return jsonify({"status": "done", "timeline": timeline}), 200

            # prepare for next iteration
            current_url = next_url
            # small polite pause
            time.sleep(0.2)
            continue

        except requests.HTTPError as he:
            step["error"] = f"fetch_http_error: {str(he)}"
            timeline.append(step)
            return jsonify({"status": "error", "timeline": timeline}), 500
        except Exception as e:
            step["error"] = str(e)
            timeline.append(step)
            return jsonify({"status": "error", "timeline": timeline}), 500

    # max iterations reached
    return jsonify({"status": "max_iterations_reached", "timeline": timeline}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
