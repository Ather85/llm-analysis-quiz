from flask import Flask, request, jsonify

app = Flask(__name__)

SECRET = "TDS24f1000999-LLM-Quiz-2025!"

@app.route("/", methods=["GET"])
def home():
    return "LLM Quiz API running."

@app.route("/quiz", methods=["POST"])
def quiz():
    data = request.json

    # Validate the secret
    if data.get("secret") != SECRET:
        return jsonify({"error": "invalid secret"}), 403

    task = data.get("task", "")

    # Minimal response required by IITM
    response = {
        "received_task": task,
        "response": "Processed"
    }

    return jsonify(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
