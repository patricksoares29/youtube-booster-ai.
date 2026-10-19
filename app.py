
import os
import secrets
from urllib.parse import urlencode

import requests
from flask import Flask, redirect, request, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly"
]

@app.route("/")
def home():
    return jsonify({
        "app": "YouTube Booster AI",
        "status": "Servidor funcionando",
        "youtube_connected": bool(session.get("youtube_connected"))
    })

@app.route("/login")
def login():
    if not CLIENT_ID or not REDIRECT_URI:
        return jsonify({
            "erro": "Configure GOOGLE_CLIENT_ID e GOOGLE_REDIRECT_URI no servidor."
        }), 500

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state
    }

    return redirect(
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(params)
    )

@app.route("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        return jsonify({"erro": request.args["error"]}), 400

    state = request.args.get("state", "")
    if not state or state != session.pop("oauth_state", None):
        return jsonify({"erro": "Estado OAuth inválido."}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"erro": "Código de autorização ausente."}), 400

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        },
        timeout=30
    )

    if not response.ok:
        return jsonify({
            "erro": "Não foi possível concluir a autorização.",
            "detalhe": response.text[:300]
        }), 400

    tokens = response.json()
    access_token = tokens.get("access_token")

    channel_response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30
    )

    if not channel_response.ok:
        return jsonify({
            "erro": "Autorização concluída, mas não foi possível ler o canal."
        }), 400

    channel_data = channel_response.json()
    channels = channel_data.get("items", [])

    session["youtube_connected"] = bool(channels)

    return jsonify({
        "mensagem": "Autorização concluída.",
        "canal": channels[0]["snippet"]["title"] if channels else None,
        "observacao": "Este teste não publica vídeos."
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
