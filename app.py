# app.py
import os
import re
import json
import base64
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# keep same secret you used in the form
SECRET = os.environ.get("QUIZ_SECRET", "TDS24f1000999-LLM-Quiz-2025!")

# Regex to find long base64 blocks (the demo embeds one)
BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{80,}")

@app.route("/", methods=["GET"])
def home():
    return "LLM Quiz API running."

@app.route("/quiz", methods=["POST"])
def quiz():
    # 1) Validate JSON
    if not request.is_json:
        return jsonify({"error": "invalid json"}), 400
    payload = request.get_json()

    # 2) Required fields
    email = payload.get("email")
    secret = payload.get("secret")
    url = payload.get("url")
    if email is None or secret is None or url is None:
        return jsonify({"error": "missing fields: email, secret, url required"}), 400

    # 3) Check secret
    if secret != SECRET:
        return jsonify({"error": "forbidden"}), 403

    # 4) Fetch the quiz page
    try:
        r = requests.get(url, timeout=20)
        page_text = r.text
    except Exception as e:
        return jsonify({"error": "failed_fetch", "details": str(e)}), 500

    # 5) Try to find base64-encoded embedded payloads (common in demo)
    def try_decode_first_base64(text):
        for m in BASE64_RE.finditer(text):
            candidate = m.group(0)
            # Some candidates may include garbage; try to decode
            try:
                decoded = base64.b64decode(candidate).decode("utf-8", errors="ignore")
                # If decoded looks like JSON, return it
                stripped = decoded.strip()
                if (stripped.startswith("{") and stripped.endswith("}")) or ("\"answer\"" in stripped):
                    return stripped
                # also return if decoded contains 'submit' or 'answer' strings
                if "submit" in stripped or "answer" in stripped or "url" in stripped:
                    return stripped
            except Exception:
                continue
        return None

    decoded_payload_text = try_decode_first_base64(page_text)

    # 6) If we decoded something that looks like JSON, parse it
    parsed = None
    if decoded_payload_text:
        try:
            parsed = json.loads(decoded_payload_text)
        except Exception:
            # Sometimes the decoded text contains extra HTML; try to extract JSON inside
            jmatch = re.search(r"(\{[\s\S]*\})", decoded_payload_text)
            if jmatch:
                try:
                    parsed = json.loads(jmatch.group(1))
                except Exception:
                    parsed = None

    # 7) If parsed and contains 'answer', submit it
    submit_result = None
    if parsed and "answer" in parsed:
        # Find submit URL (prefers provided submit_url in parsed JSON, else find on page)
        submit_url = parsed.get("submit") or parsed.get("submit_url") or parsed.get("submitUrl")
        if not submit_url:
            m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", page_text, re.IGNORECASE)
            if m:
                submit_url = m.group(0)
        if not submit_url:
            # Might be 'https://example.com/submit' pattern
            m = re.search(r"https?://[^\s'\"<>]+/submit", page_text, re.IGNORECASE)
            if m:
                submit_url = m.group(0)

        # build payload
        sub = {
            "email": email,
            "secret": secret,
            "url": url,
            "answer": parsed.get("answer")
        }
        if submit_url:
            try:
                resp = requests.post(submit_url, json=sub, timeout=20)
                try:
                    submit_result = {"http_status": resp.status_code, "response_json": resp.json()}
                except Exception:
                    submit_result = {"http_status": resp.status_code, "response_text": resp.text}
            except Exception as e:
                submit_result = {"error": "submit_failed", "details": str(e)}
        else:
            submit_result = {"error": "no_submit_url_found", "parsed": parsed}

    # 8) If no parsed 'answer', try to locate an 'answer' in the page itself (simple heuristics)
    if submit_result is None:
        # try to extract text instructions near "Post your answer" or "answer"
        answer_heuristic = None
        # look for JSON-like payload embedded in page text (the demo uses a JSON block inside base64)
        jmatch = re.search(r"\{\s*\"email\"\s*:\s*\"[^\"]+\"[\s\S]{0,400}\}", page_text)
        if jmatch:
            try:
                j = json.loads(jmatch.group(0))
                if "answer" in j:
                    answer_heuristic = j["answer"]
            except Exception:
                pass

        if answer_heuristic is not None:
            # try to submit similarly
            m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", page_text, re.IGNORECASE)
            submit_url = m.group(0) if m else None
            if submit_url:
                sub = {"email": email, "secret": secret, "url": url, "answer": answer_heuristic}
                try:
                    resp = requests.post(submit_url, json=sub, timeout=20)
                    try:
                        submit_result = {"http_status": resp.status_code, "response_json": resp.json()}
                    except Exception:
                        submit_result = {"http_status": resp.status_code, "response_text": resp.text}
                except Exception as e:
                    submit_result = {"error": "submit_failed", "details": str(e)}
            else:
                submit_result = {"error": "no_submit_url_found_but_answer_heuristic", "answer": answer_heuristic}

    # 9) Final return: if we submitted, return submission result; otherwise return debug info
    if submit_result is not None:
        return jsonify({"status": "submitted_or_attempted", "submit_result": submit_result, "decoded_payload_present": bool(parsed)})

    # Nothing found — return the page snippet (small) for debugging
    snippet = page_text[:2000]
    return jsonify({"status": "no_answer_found", "snippet": snippet}), 200


if __name__ == "__main__":
    # For local run only; Render uses gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
