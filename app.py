
import os
import secrets
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, request, session

app = Flask(__name__)
from flask_cors import CORS

CORS(
    app,
    origins=["https://patricksoares29.github.io"],
    supports_credentials=True
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

# Tokens mantidos apenas na memória do servidor.
# Uma reinicialização exige nova autorização.
TOKENS = {}

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly"
]


@app.route("/")
def home():
    return jsonify({
        "app": "YouTube Booster AI",
        "status": "Servidor funcionando",
        "youtube_connected": bool(
            session.get("youtube_connected")
        ),
        "recursos": [
            "Conexão com o YouTube",
            "Leitura de dados do canal",
            "Preparação de publicações"
        ]
    })


@app.route("/login")
def login():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return jsonify({
            "erro": "Configure as variáveis do Google no Render."
        }), 500

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state
    }

    return redirect(
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(params)
    )


@app.route("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        return jsonify({
            "erro": request.args["error"]
        }), 400

    state = request.args.get("state", "")
    expected = session.pop("oauth_state", None)

    if not state or state != expected:
        return jsonify({
            "erro": "Estado OAuth inválido. Tente conectar novamente."
        }), 400

    code = request.args.get("code")
    if not code:
        return jsonify({
            "erro": "Código de autorização ausente."
        }), 400

    try:
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
            app.logger.error(
                "Falha na troca do código OAuth: %s",
                response.status_code
            )
            return jsonify({
                "erro": "Não foi possível concluir a autorização."
            }), 400

        tokens = response.json()
        access_token = tokens.get("access_token")

        channel_response = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={
                "part": "snippet,statistics",
                "mine": "true"
            },
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            timeout=30
        )

        if not channel_response.ok:
            return jsonify({
                "erro": "Conectado, mas não foi possível ler o canal."
            }), 400

        channels = channel_response.json().get("items", [])

        if not channels:
            return jsonify({
                "erro": "Nenhum canal do YouTube foi encontrado."
            }), 404

        channel = channels[0]

        # Identificador aleatório: nenhum token vai para o navegador.
        connection_id = secrets.token_urlsafe(32)
        TOKENS[connection_id] = tokens
        session["connection_id"] = connection_id
        session["youtube_connected"] = True

        return jsonify({
            "mensagem": "Autorização concluída.",
            "canal": channel["snippet"]["title"],
            "estatisticas": channel.get("statistics", {}),
            "observacao": (
                "Leitura do canal autorizada. "
                "A publicação ainda não está habilitada."
            )
        })

    except requests.RequestException:
        app.logger.exception("Erro de comunicação com o Google")
        return jsonify({
            "erro": "Falha de comunicação. Tente novamente."
        }), 502


@app.route("/api/canal")
def dados_canal():
    connection_id = session.get("connection_id")
    tokens = TOKENS.get(connection_id)

    if not tokens:
        return jsonify({
            "erro": "Conecte novamente sua conta do YouTube.",
            "login": "/login"
        }), 401

    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={
                "part": "snippet,statistics",
                "mine": "true"
            },
            headers={
                "Authorization": f"Bearer {tokens['access_token']}"
            },
            timeout=30
        )

        if not response.ok:
            return jsonify({
                "erro": "Não foi possível consultar o canal."
            }), response.status_code

        channels = response.json().get("items", [])
        if not channels:
            return jsonify({"erro": "Canal não encontrado."}), 404

        channel = channels[0]

        return jsonify({
            "canal": channel["snippet"]["title"],
            "estatisticas": channel.get("statistics", {})
        })

    except requests.RequestException:
        return jsonify({
            "erro": "Falha de comunicação com o YouTube."
        }), 502


@app.route("/api/publicacao/rascunho", methods=["POST"])
def criar_rascunho():
    data = request.get_json(silent=True) or {}

    titulo = str(data.get("titulo", "")).strip()
    descricao = str(data.get("descricao", "")).strip()
    tipo = str(data.get("tipo", "video")).strip().lower()

    if not titulo or not descricao:
        return jsonify({
            "erro": "Informe título e descrição."
        }), 400

    if tipo not in ("video", "short", "live", "post"):
        return jsonify({"erro": "Tipo de conteúdo inválido."}), 400

    return jsonify({
        "status": "rascunho",
        "tipo": tipo,
        "titulo": titulo,
        "descricao": descricao,
        "publicado": False,
        "mensagem": (
            "Rascunho preparado. Nenhum conteúdo foi publicado."
        )
    }), 201


if __name__ == "__main__":
    if not app.secret_key:
        raise RuntimeError(
            "Configure FLASK_SECRET_KEY nas variáveis do Render."
        )

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
