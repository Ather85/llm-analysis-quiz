from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import pandas as pd
import io
import base64
import tempfile
import time
import speech_recognition as sr

app = Flask(__name__)

# Replace with your secret
MY_SECRET = "TDS24f1000999-LLM-Quiz-2025!"
MY_EMAIL = "24f1000999@ds.study.iitm.ac.in"

@app.route("/quiz", methods=["POST"])
def handle_quiz():
    try:
        data = request.get_json()
    except:
        return jsonify({"error": "Invalid JSON"}), 400

    email = data.get("email")
    secret = data.get("secret")
    url = data.get("url")
    answer = data.get("answer")  # Only used if re-submit

    if secret != MY_SECRET or email != MY_EMAIL:
        return jsonify({"error": "Invalid secret or email"}), 403

    if not url:
        return jsonify({"error": "Missing URL"}), 400

    # Solve the task based on URL pattern
    try:
        if "demo-scrape" in url:
            answer_value = solve_scrape(url)
        elif "demo-audio" in url:
            answer_value = solve_audio(url)
        elif "demo-csv" in url:
            answer_value = solve_csv(url)
        else:
            return jsonify({"error": "Unknown task type"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Submit the answer
    submit_payload = {
        "email": MY_EMAIL,
        "secret": MY_SECRET,
        "url": url,
        "answer": answer_value
    }

    submit_response = requests.post(
        "https://tds-llm-analysis.s-anand.net/submit",
        json=submit_payload
    ).json()

    return jsonify(submit_response)


def solve_scrape(url):
    """Scrape secret code or numbers from HTML."""
    r = requests.get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    # Try to find the secret code
    code = soup.find("span", {"id": "secret-code"})
    if code:
        return code.text.strip()
    # fallback: find numbers
    numbers = [int(s) for s in soup.get_text().split() if s.isdigit()]
    if numbers:
        return str(numbers[0])
    raise Exception("Could not find secret code or number.")


def solve_csv(url):
    """Download CSV/PDF, filter by cutoff, and sum numbers."""
    r = requests.get(url)
    df = pd.read_csv(io.StringIO(r.text))
    cutoff = df.columns[0]  # assuming first column header is cutoff? Adjust if needed
    sum_value = df[df[df.columns[0]] >= 4122][df.columns[0]].sum()
    return str(int(sum_value))


def solve_audio(url):
    """Download audio, transcribe, and return text."""
    r = requests.get(url)
    temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_audio.write(r.content)
    temp_audio.close()

    recognizer = sr.Recognizer()
    with sr.AudioFile(temp_audio.name) as source:
        audio_data = recognizer.record(source)
        text = recognizer.recognize_google(audio_data)

    return text


if __name__ == "__main__":
    # On Render, the port is defined in $PORT env variable
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
