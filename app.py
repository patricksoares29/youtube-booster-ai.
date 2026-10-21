
import os
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

    if tokens.get("expires_at", 0) > time.time() + 60:
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
            "Falha ao renovar token OAuth: HTTP %s",
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
        params={"part": "snippet,statistics", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )

    if not response.ok:
        app.logger.error(
            "Consulta ao canal falhou: HTTP %s",
            response.status_code,
        )
        return None, response.status_code

    canais = response.json().get("items", [])
    if not canais:
        return None, 404

    return canais[0], 200


def consultar_videos(access_token, max_results=20):
    headers = {"Authorization": f"Bearer {access_token}"}

    response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "contentDetails", "mine": "true"},
        headers=headers,
        timeout=30,
    )

    if not response.ok:
        app.logger.error(
            "Falha ao buscar playlist: HTTP %s",
            response.status_code,
        )
        return None, response.status_code

    canais = response.json().get("items", [])
    if not canais:
        return [], 200

    uploads_id = (
        canais[0]
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads_id:
        return [], 200

    response = requests.get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        params={
            "part": "snippet,contentDetails",
            "playlistId": uploads_id,
            "maxResults": max(1, min(int(max_results), 50)),
        },
        headers=headers,
        timeout=30,
    )

    if not response.ok:
        app.logger.error(
            "Falha ao listar vídeos: HTTP %s",
            response.status_code,
        )
        return None, response.status_code

    itens = response.json().get("items", [])
    ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in itens
    ]
    ids = [video_id for video_id in ids if video_id]

    if not ids:
        return [], 200

    response = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(ids),
        },
        headers=headers,
        timeout=30,
    )

    if not response.ok:
        app.logger.error(
            "Falha ao consultar estatísticas: HTTP %s",
            response.status_code,
        )
        return None, response.status_code

    videos_api = {
        video["id"]: video
        for video in response.json().get("items", [])
    }

    videos = []
    for item in itens:
        video_id = item.get("contentDetails", {}).get("videoId")
        video = videos_api.get(video_id)
        if not video:
            continue

        snippet = video.get("snippet", {})
        stats = video.get("statistics", {})
        details = video.get("contentDetails", {})

        videos.append({
            "id": video_id,
            "titulo": snippet.get("title", ""),
            "publicado_em": snippet.get("publishedAt", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "miniatura": (
                snippet.get("thumbnails", {})
                .get("medium", {})
                .get("url", "")
            ),
            "duracao": details.get("duration", ""),
            "visualizacoes": int(stats.get("viewCount", 0)),
            "curtidas": (
                int(stats["likeCount"])
                if "likeCount" in stats else None
            ),
            "comentarios": (
                int(stats["commentCount"])
                if "commentCount" in stats else None
            ),
        })

    return videos, 200


@app.route("/")
def home():
    return jsonify({
        "app": "YouTube Booster AI",
        "status": "Servidor funcionando",
        "recursos": [
            "Conexão com o YouTube",
            "Leitura de dados do canal",
            "Armazenamento de tokens no PostgreSQL",
            "Consulta de vídeos e estatísticas",
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


@app.route("/api/videos")
def dados_videos():
    connection_id = session.get("connection_id")

    try:
        tokens = obter_tokens_validos(connection_id)
        if not tokens:
            return jsonify({
                "erro": "Conecte novamente sua conta do YouTube.",
                "login": "/login",
            }), 401

        videos, status = consultar_videos(
            tokens["access_token"],
            max_results=20,
        )

        if videos is None:
            return jsonify({
                "erro": "Não foi possível consultar os vídeos."
            }), status

        return jsonify({
            "quantidade": len(videos),
            "videos": videos,
        })

    except requests.RequestException:
        app.logger.exception(
            "Falha de comunicação ao consultar os vídeos"
        )
        return jsonify({
            "erro": "Falha de comunicação com o YouTube."
        }), 502

    except (psycopg2.Error, RuntimeError):
        app.logger.exception(
            "Falha ao recuperar tokens para consultar vídeos"
        )
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

from urllib.parse import urlparse, parse_qs


@app.route("/api/live/verificar", methods=["POST"])
def verificar_live():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url:
        return jsonify({
            "erro": "Informe o link da live do YouTube."
        }), 400

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        video_id = ""

        if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith("/live/") or parsed.path.startswith("/shorts/"):
                video_id = parsed.path.split("/")[2]
        elif host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]

        if not video_id or not all(
            char.isalnum() or char in "_-" for char in video_id
        ):
            return jsonify({
                "erro": "Link inválido. Envie um link de vídeo ou live do YouTube."
            }), 400

        return jsonify({
            "status": "link_validado",
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "mensagem": (
                "Formato do link validado. "
                "A disponibilidade e o acesso ao vídeo ainda precisam ser verificados."
            )
        }), 200

    except Exception:
        app.logger.exception("Falha ao validar link da live")
        return jsonify({
            "erro": "Não foi possível validar o link."
        }), 500

@app.route("/api/live/acessar", methods=["POST"])
def acessar_live():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url:
        return jsonify({"erro": "Informe o link da live."}), 400

    # Identifica o ID do vídeo pelo link informado.
    import re
    from urllib.parse import urlparse, parse_qs

    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        video_id = ""

        if host == "youtu.be" or host.endswith(".youtu.be"):
            video_id = parsed.path.strip("/").split("/")[0]
        elif host == "youtube.com" or host.endswith(".youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            else:
                partes = parsed.path.strip("/").split("/")
                if len(partes) >= 2 and partes[0] in (
                    "live", "shorts", "embed"
                ):
                    video_id = partes[1]

        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return jsonify({
                "erro": "Link inválido. Informe um link válido do YouTube."
            }), 400

        # Reutiliza a sessão autenticada do painel.
        connection_id = session.get("connection_id")
        if not connection_id:
            return jsonify({
                "erro": "Conecte novamente sua conta do YouTube pelo painel."
            }), 401

        tokens = obter_tokens_validos(connection_id)
        if not tokens:
            return jsonify({
                "erro": "Não foi possível obter um token válido. Reconecte o YouTube."
            }), 401

        if isinstance(tokens, dict):
            access_token = tokens.get("access_token")
        else:
            access_token = tokens

        if not access_token:
            return jsonify({
                "erro": "Token de acesso indisponível. Reconecte o YouTube."
            }), 401

        # Consulta os metadados pela API oficial do YouTube.
        resposta = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,contentDetails",
                "id": video_id,
            },
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            timeout=20,
        )

        if resposta.status_code == 401:
            return jsonify({
                "erro": "A autorização expirou. Reconecte sua conta do YouTube."
            }), 401

        if resposta.status_code == 403:
            app.logger.error(
                "API do YouTube retornou 403: %s",
                resposta.text[:1000]
            )
            return jsonify({
                "erro": "A API do YouTube recusou a consulta. Verifique as credenciais e a cota do projeto."
            }), 502

        resposta.raise_for_status()
        itens = resposta.json().get("items", [])

        if not itens:
            return jsonify({
                "erro": "A API não encontrou esse vídeo ou não disponibilizou seus dados."
            }), 404

        video = itens[0]
        snippet = video.get("snippet", {})
        duracao_iso = video.get("contentDetails", {}).get("duration", "")

        # Converte a duração ISO 8601 (ex.: PT1H2M3S) para segundos.
        correspondencia = re.fullmatch(
            r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
            duracao_iso
        )

        duracao_segundos = None
        if correspondencia:
            horas, minutos, segundos = (
                int(valor or 0) for valor in correspondencia.groups()
            )
            duracao_segundos = horas * 3600 + minutos * 60 + segundos

        return jsonify({
            "status": "acesso_confirmado",
            "video_id": video_id,
            "titulo": snippet.get("title"),
            "duracao_segundos": duracao_segundos,
            "mensagem": "Metadados consultados pela API oficial do YouTube. O vídeo ainda não foi baixado."
        }), 200

    except requests.RequestException:
        app.logger.exception("Falha na consulta à API oficial do YouTube")
        return jsonify({
            "erro": "Não foi possível consultar a API do YouTube. Verifique os logs do servidor."
        }), 502
    except Exception:
        app.logger.exception("Falha ao acessar metadados da live")
        return jsonify({
            "erro": "Ocorreu um erro ao consultar os metadados da live."
        }), 500

@app.route("/api/shorts/preparar", methods=["POST"])
def preparar_shorts():
    data = request.get_json(silent=True) or {}
    video_id = str(data.get("video_id", "")).strip()

    if not video_id or not all(
        c.isalnum() or c in "_-" for c in video_id
    ):
        return jsonify({
            "erro": "Informe um ID válido de vídeo do YouTube."
        }), 400

    return jsonify({
        "status": "pedido_validado",
        "video_id": video_id,
        "quantidade_planejada": 10,
        "formato": "vertical 9:16",
        "mensagem": (
            "Pedido validado. O download, a seleção dos momentos "
            "e a geração dos Shorts ainda não foram executados."
        )
    }), 200
if __name__ == "__main__":
    if not app.secret_key:
        raise RuntimeError(
            "Configure FLASK_SECRET_KEY nas variáveis do Render."
        )

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
