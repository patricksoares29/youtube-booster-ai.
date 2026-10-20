
import os
import json
import time
import secrets
from urllib.parse import urlencode

import requests
import psycopg2
from psycopg2.extras import Json
from flask import Flask, jsonify, redirect, request, session
from flask_cors import CORS


app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY")

app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None",
)

CORS(
    app,
    origins=["https://patricksoares29.github.io"],
    supports_credentials=True,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly"
]


def conectar_banco():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada.")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        sslmode="require",
    )


def garantir_tabela():
    conn = conectar_banco()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS youtube_tokens (
                    connection_id TEXT PRIMARY KEY,
                    tokens JSONB NOT NULL,
                    criado_em TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def salvar_tokens(connection_id, tokens):
    garantir_tabela()

    conn = conectar_banco()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO youtube_tokens (connection_id, tokens)
                VALUES (%s, %s)
                ON CONFLICT (connection_id)
                DO UPDATE SET
                    tokens = EXCLUDED.tokens,
                    criado_em = NOW()
                """,
                (connection_id, Json(tokens)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def buscar_tokens(connection_id):
    if not connection_id:
        return None

    garantir_tabela()

    conn = conectar_banco()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tokens
                FROM youtube_tokens
                WHERE connection_id = %s
                """,
                (connection_id,),
            )
            row = cur.fetchone()

        return row[0] if row else None
    finally:
        conn.close()


def obter_tokens_validos(connection_id):
    tokens = buscar_tokens(connection_id)

    if not tokens:
        return None

    expiracao = tokens.get("expires_at", 0)

    if expiracao > time.time() + 60:
        return tokens

    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        return None

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )

    if not response.ok:
        app.logger.error(
            "Não foi possível renovar o token OAuth: HTTP %s",
            response.status_code,
        )
        return None

    novos_tokens = response.json()
    tokens.update(novos_tokens)
    tokens["expires_at"] = (
        time.time() + int(novos_tokens.get("expires_in", 3600))
    )

    salvar_tokens(connection_id, tokens)
    return tokens


def consultar_canal(access_token):
    response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={
            "part": "snippet,statistics",
            "mine": "true",
        },
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        timeout=30,
    )

    if not response.ok:
        app.logger.error(
            "Consulta ao YouTube falhou: HTTP %s",
            response.status_code,
        )
        return None, response.status_code

    canais = response.json().get("items", [])

    if not canais:
        return None, 404

    return canais[0], 200


@app.route("/")
def home():
    return jsonify({
        "app": "YouTube Booster AI",
        "status": "Servidor funcionando",
        "recursos": [
            "Conexão com o YouTube",
            "Leitura de dados do canal",
            "Armazenamento de tokens no PostgreSQL",
            "Preparação de publicações",
        ],
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
        "state": state,
    }

    return redirect(
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(params)
    )


@app.route("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        return jsonify({
            "erro": "O Google não autorizou a conexão."
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
                "grant_type": "authorization_code",
            },
            timeout=30,
        )

        if not response.ok:
            app.logger.error(
                "Troca do código OAuth falhou: HTTP %s",
                response.status_code,
            )
            return jsonify({
                "erro": "Não foi possível concluir a autorização."
            }), 400

        tokens = response.json()
        access_token = tokens.get("access_token")

        if not access_token:
            return jsonify({
                "erro": "O Google não forneceu um token de acesso."
            }), 400

        tokens["expires_at"] = (
            time.time() + int(tokens.get("expires_in", 3600))
        )

        channel, status = consultar_canal(access_token)

        if channel is None:
            return jsonify({
                "erro": "Não foi possível consultar o canal do YouTube."
            }), status

        connection_id = secrets.token_urlsafe(32)
        salvar_tokens(connection_id, tokens)

        session["connection_id"] = connection_id
        session["youtube_connected"] = True

        return jsonify({
            "mensagem": "Autorização concluída.",
            "canal": channel["snippet"]["title"],
            "estatisticas": channel.get("statistics", {}),
            "observacao": (
                "Leitura do canal autorizada. "
                "A publicação ainda não está habilitada."
            ),
        })

    except requests.RequestException:
        app.logger.exception("Falha de comunicação com o Google")
        return jsonify({
            "erro": "Falha de comunicação. Tente novamente."
        }), 502

    except (psycopg2.Error, RuntimeError):
        app.logger.exception("Falha ao salvar a conexão no banco")
        return jsonify({
            "erro": "Não foi possível salvar a conexão no banco."
        }), 503


@app.route("/api/canal")
def dados_canal():
    connection_id = session.get("connection_id")

    try:
        tokens = obter_tokens_validos(connection_id)

        if not tokens:
            return jsonify({
                "erro": "Conecte novamente sua conta do YouTube.",
                "login": "/login",
            }), 401

        channel, status = consultar_canal(tokens["access_token"])

        if channel is None:
            return jsonify({
                "erro": "Não foi possível consultar o canal."
            }), status

        session["youtube_connected"] = True

        return jsonify({
            "canal": channel["snippet"]["title"],
            "estatisticas": channel.get("statistics", {}),
        })

    except requests.RequestException:
        app.logger.exception("Falha ao consultar o YouTube")
        return jsonify({
            "erro": "Falha de comunicação com o YouTube."
        }), 502

    except (psycopg2.Error, RuntimeError):
        app.logger.exception("Falha ao recuperar tokens do banco")
        return jsonify({
            "erro": "Não foi possível acessar a conexão salva."
        }), 503


@app.route("/api/status-conexao")
def status_conexao():
    connection_id = session.get("connection_id")

    try:
        tokens = buscar_tokens(connection_id)

        return jsonify({
            "sessao_ativa": bool(connection_id),
            "token_disponivel": bool(tokens),
            "youtube_connected": bool(tokens),
        })

    except (psycopg2.Error, RuntimeError):
        app.logger.exception("Falha ao verificar o banco")
        return jsonify({
            "erro": "Não foi possível verificar a conexão."
        }), 503


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
        return jsonify({
            "erro": "Tipo de conteúdo inválido."
        }), 400

    return jsonify({
        "status": "rascunho",
        "tipo": tipo,
        "titulo": titulo,
        "descricao": descricao,
        "publicado": False,
        "mensagem": (
            "Rascunho preparado. Nenhum conteúdo foi publicado."
        ),
    }), 201


if __name__ == "__main__":
    if not app.secret_key:
        raise RuntimeError(
            "Configure FLASK_SECRET_KEY nas variáveis do Render."
        )

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )

