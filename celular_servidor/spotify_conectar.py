"""
Conecta a sua conta do Spotify ao servidor. Rode uma vez, na pasta celular_servidor:

    python spotify_conectar.py           abre o navegador deste computador e espera o Spotify voltar
    python spotify_conectar.py --colar   para quando o navegador está em outro aparelho: você cola o endereço

Usa o fluxo PKCE, sem client secret, e grava o spotify_token.json. A autorização dura 6 meses; depois disso,
rode de novo. Se o servidor roda em outro aparelho, copie o spotify_token.json para a pasta celular_servidor dele.
"""
import base64
import errno
import hashlib
import html
import json
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    import requests
except ImportError as e:
    sys.exit(f"Falta a biblioteca {e.name}. Na pasta celular_servidor, rode: pip install -r requirements.txt\n"
             "No PC, ative antes o .venv.")

import spotify

ARQUIVO_CONFIG = Path(__file__).parent / "config_servidor.json"
CLIENT_ID_DE_EXEMPLO = "CLIENT-ID-DO-APP-SPOTIFY"
PORTA = urlsplit(spotify.REDIRECT_URI).port  # 8888: tem de ser a mesma registrada no app
TEMPO_DE_ESPERA = 300  # segundos esperando o Spotify voltar para o /callback

COMO_CRIAR_O_APP = (
    "Para conseguir o client_id, crie um app no painel de desenvolvedor do Spotify (a conta precisa ser Premium):\n"
    "  1. Entre em https://developer.spotify.com/dashboard com a sua conta do Spotify e clique em \"Create app\".\n"
    f"  2. Preencha o nome e a descrição. Em \"Redirect URIs\", escreva {spotify.REDIRECT_URI} e clique em \"Add\".\n"
    "  3. Marque só \"Web API\", aceite os termos e salve.\n"
    "  4. Em \"Settings\", copie o \"Client ID\" (o Client Secret não é preciso) e cole no config_servidor.json,\n"
    "     na pasta celular_servidor:\n"
    "       \"spotify\": {\"client_id\": \"COLE-AQUI-O-CLIENT-ID\"}\n"
    "  5. Rode de novo: python spotify_conectar.py\n"
    "Para usar uma conta que não é a dona do app, adicione-a antes em Settings > Users Management."
)
CODIGO_RECUSADO = ("o Spotify recusou o código de autorização (ele vale poucos minutos e só pode ser usado uma "
                   "vez): rode python spotify_conectar.py de novo")
USO = "Uso: python spotify_conectar.py [--colar]"
# O erro de configuração mais comum aparece na página do Spotify, não aqui: o script ficaria esperando à toa.
REDIRECT_ERRADA = ('Se a página do Spotify mostrar "INVALID_CLIENT: Invalid redirect URI", a Redirect URI do app no '
                   f"painel não é exatamente {spotify.REDIRECT_URI}: corrija em Settings, salve e rode de novo.")

PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Spotify</title></head>
<body style="font-family: sans-serif; margin: 3em; font-size: 1.2em"><p>{}</p></body></html>
"""


class ErroAoConectar(Exception):
    """Falha na conexão: a mensagem é em português; `detalhe` é o técnico, para o terminal."""

    def __init__(self, mensagem: str, detalhe: str = ""):
        super().__init__(mensagem)
        self.detalhe = detalhe


def ler_client_id(caminho: Path = ARQUIVO_CONFIG) -> str:
    """O client_id do bloco "spotify" do config_servidor.json, ou encerra explicando como conseguir um."""
    try:
        texto = Path(caminho).read_text(encoding="utf-8-sig")  # -sig aceita o BOM do Bloco de Notas
    except FileNotFoundError:
        sys.exit(f"Não achei o {caminho}. Copie config_servidor.example.json para config_servidor.json, "
                 f"na mesma pasta, e preencha.\n{COMO_CRIAR_O_APP}")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"Não consegui ler o {caminho}: salve-o com a codificação UTF-8 (detalhe técnico: {e}).")
    try:
        config = json.loads(texto)
    except json.JSONDecodeError as e:
        sys.exit(f"Erro de JSON em {caminho}, linha {e.lineno}, coluna {e.colno}: confira vírgulas e aspas "
                 "(o python servidor.py explica o que corrigir).")
    bloco = config.get("spotify") if isinstance(config, dict) else None
    client_id = bloco.get("client_id") if isinstance(bloco, dict) else None
    client_id = client_id.strip() if isinstance(client_id, str) else ""
    if client_id in ("", CLIENT_ID_DE_EXEMPLO):
        sys.exit(f'Falta o "client_id" do bloco "spotify" no {caminho}.\n{COMO_CRIAR_O_APP}')
    if not client_id.isascii() or any(c.isspace() for c in client_id):
        sys.exit(f'O "client_id" do bloco "spotify" no {caminho} tem espaço ou acento: copie de novo o Client ID '
                 "em Settings, no painel do app.")
    return client_id


# ---------- PKCE ----------

def gerar_verifier() -> str:
    """86 caracteres de A-Z, a-z, 0-9, "-" e "_", dentro dos 43 a 128 que o PKCE pede."""
    return secrets.token_urlsafe(64)


def calcular_challenge(verifier: str) -> str:
    """base64url do SHA-256 do verifier, sem o "=" do fim (RFC 7636, método S256)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def montar_url(client_id: str, challenge: str, state: str, contas_url: str = spotify.CONTAS_URL) -> str:
    """Endereço da página do Spotify onde você autoriza o app."""
    return f"{contas_url}/authorize?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": spotify.REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": challenge, "scope": spotify.ESCOPOS, "state": state})


def extrair_codigo(endereco: str, state: str) -> str:
    """
    Tira o code do endereço para onde o Spotify redirecionou (http://127.0.0.1:8888/callback?code=...&state=...),
    conferindo o state. Levanta ErroAoConectar se você recusou, se o state não confere ou se falta o code.
    """
    endereco = (endereco or "").strip().strip("\"'<>")
    consulta = urlsplit(endereco).query if "?" in endereco else endereco  # aceita só "code=...&state=..."
    campos = {nome: valores[0] for nome, valores in parse_qs(consulta).items()}
    erro = campos.get("error")
    if not campos.get("code") and not erro:
        raise ErroAoConectar(f"não achei o código no endereço: copie o endereço completo, começando com "
                             f"{spotify.REDIRECT_URI}?code=")
    # O state vem antes do erro: um endereço de outra tentativa (ou de outro site) não vale nem para recusar.
    if not secrets.compare_digest(campos.get("state", "").encode(), state.encode()):
        raise ErroAoConectar("o endereço não é desta tentativa de conexão (o state não confere): rode "
                             "python spotify_conectar.py de novo e use o endereço novo",
                             f"state={campos.get('state')!r}")
    if erro == "access_denied":
        raise ErroAoConectar('você não autorizou o acesso na página do Spotify: rode de novo e clique em "Concordo" '
                             '(ou "Agree")', erro)
    if erro:
        raise ErroAoConectar(f"o Spotify devolveu o erro {erro}", erro)
    return campos["code"]


# ---------- Receber o código ----------

def abrir_no_navegador(url: str) -> bool:
    """webbrowser.open, menos num Linux sem tela, onde ele abriria um navegador de texto no próprio terminal."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return webbrowser.open(url)


def duracao(segundos: float) -> str:
    return f"{round(segundos / 60)} minutos" if segundos >= 120 else f"{segundos:g} segundos"


def esperar_callback(url: str, state: str, porta: int = PORTA, tempo_limite: float = TEMPO_DE_ESPERA,
                     abrir_navegador=abrir_no_navegador) -> str:
    """
    Sobe um servidor em 127.0.0.1:`porta`, abre o navegador em `url` e espera o Spotify voltar para o
    /callback. Devolve o code; levanta ErroAoConectar se der errado ou se passar o `tempo_limite`.
    """
    resultados = queue.Queue()

    class Callback(BaseHTTPRequestHandler):
        timeout = 30  # conexão aberta sem pedido (o navegador às vezes abre antes) não fica presa para sempre

        def do_GET(self):
            if urlsplit(self.path).path != urlsplit(spotify.REDIRECT_URI).path:
                return self._pagina(404, "Página não encontrada.")
            try:
                codigo = extrair_codigo(self.path, state)
            except ErroAoConectar as e:
                self._pagina(400, f"Não deu para conectar o Spotify: {e}. Veja o terminal.")
                resultados.put(e)
            else:
                # O código ainda vai ser trocado pelo token: o resultado aparece no terminal.
                self._pagina(200, "Autorização recebida. Volte ao terminal para ver se a conexão terminou.")
                resultados.put(codigo)

        def _pagina(self, status: int, texto: str) -> None:
            corpo = PAGINA.format(html.escape(texto)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, formato, *args):
            pass

    class Servidor(ThreadingHTTPServer):
        # No Windows, o SO_REUSEADDR deixaria abrir a porta mesmo com outro programa escutando nela.
        allow_reuse_address = sys.platform != "win32"
        allow_reuse_port = False
        block_on_close = False  # não espera uma conexão parada do navegador para encerrar

    try:
        servidor = Servidor(("127.0.0.1", porta), Callback)
    except OSError as e:
        # No Windows, uma porta reservada pelo sistema (Hyper-V, WSL) dá PermissionError.
        ocupada = isinstance(e, PermissionError) or e.errno in (errno.EADDRINUSE,
                                                                  getattr(errno, "WSAEADDRINUSE", None))
        motivo = f"a porta {porta} já está em uso" if ocupada else f"não consegui usar a porta {porta}"
        raise ErroAoConectar(f"{motivo}: feche o programa que usa a porta ou rode python spotify_conectar.py "
                             "--colar", repr(e)) from e
    thread = threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        print("Abrindo o navegador para você autorizar o acesso. Se ele não abrir, copie este endereço num "
              "navegador deste computador:\n")
        print(f"  {url}\n", flush=True)
        try:
            aberto = abrir_navegador(url)
        except Exception as e:  # abrir o navegador é só uma ajuda: o endereço já está na tela
            print(f"(detalhe técnico ao abrir o navegador: {e!r})", file=sys.stderr)
            aberto = False
        if not aberto:
            print("Não consegui abrir o navegador: copie o endereço acima. Se o navegador está em outro aparelho, "
                  "aperte Ctrl+C e rode python spotify_conectar.py --colar", flush=True)
        print(REDIRECT_ERRADA)
        print(f"Esperando o Spotify voltar para {spotify.REDIRECT_URI} (até {duracao(tempo_limite)})...", flush=True)
        limite = time.monotonic() + tempo_limite
        while True:
            try:
                # Em pedaços curtos: no Windows, uma espera longa não deixa o Ctrl+C interromper.
                resultado = resultados.get(timeout=max(0.0, min(0.5, limite - time.monotonic())))
                break
            except queue.Empty:
                if time.monotonic() >= limite:
                    raise ErroAoConectar("o Spotify não voltou a tempo: rode python spotify_conectar.py "
                                         "de novo") from None
    finally:
        servidor.shutdown()
        servidor.server_close()
        thread.join(timeout=5)
    if isinstance(resultado, ErroAoConectar):
        raise resultado
    return resultado


def colar_endereco(url: str, state: str, ler=input) -> str:
    """Modo --colar: mostra o endereço, pede o endereço para onde o Spotify redirecionou e tira o code dele."""
    print("Abra este endereço num navegador (pode ser no celular) e autorize o acesso:\n")
    print(f"  {url}\n")
    print(f"Depois de autorizar, o navegador vai para um endereço que começa com {spotify.REDIRECT_URI}?code= "
          "e mostra um erro de página que não abre: é assim mesmo. Copie o endereço completo da barra e cole aqui.")
    print(REDIRECT_ERRADA, flush=True)
    try:
        endereco = ler("Endereço: ")
    except EOFError:
        raise ErroAoConectar("nenhum endereço foi colado") from None
    return extrair_codigo(endereco, state)


def trocar_codigo(client_id: str, codigo: str, verifier: str, sessao=requests, contas_url: str = spotify.CONTAS_URL,
                  relogio=time.time) -> dict:
    """Troca o code pelos tokens e devolve o conteúdo do spotify_token.json."""
    agora = relogio()
    formulario = {"grant_type": "authorization_code", "code": codigo, "redirect_uri": spotify.REDIRECT_URI,
                  "client_id": client_id, "code_verifier": verifier}
    try:
        dados = spotify.pedir_token(formulario, sessao, contas_url, recusado=CODIGO_RECUSADO)
    except spotify.ErroNoSpotify as e:
        raise ErroAoConectar(str(e), e.detalhe) from e
    token = spotify.token_de_resposta(dados, agora)
    if not spotify.texto_preenchido(token["refresh_token"]):
        raise ErroAoConectar("o Spotify não mandou o refresh token: rode python spotify_conectar.py de novo",
                             f"campos da resposta: {sorted(dados)}")
    return token


def main(argumentos: list[str] | None = None, config: Path = ARQUIVO_CONFIG,
         arquivo_token: Path = spotify.ARQUIVO_TOKEN, sessao=requests, abrir_navegador=abrir_no_navegador,
         ler=input, porta: int = PORTA, tempo_limite: float = TEMPO_DE_ESPERA,
         contas_url: str = spotify.CONTAS_URL, relogio=time.time) -> None:
    argumentos = sys.argv[1:] if argumentos is None else argumentos
    if any(a in ("-h", "--help", "/?") for a in argumentos):
        print(__doc__.strip())
        return
    if any(a != "--colar" for a in argumentos):
        sys.exit(f"Não entendi {' '.join(argumentos)}.\n{USO}")
    client_id = ler_client_id(config)
    verifier = gerar_verifier()
    state = secrets.token_urlsafe(16)
    url = montar_url(client_id, calcular_challenge(verifier), state, contas_url)
    try:
        if "--colar" in argumentos:
            codigo = colar_endereco(url, state, ler)
        else:
            codigo = esperar_callback(url, state, porta, tempo_limite, abrir_navegador)
        token = trocar_codigo(client_id, codigo, verifier, sessao, contas_url, relogio)
        try:
            spotify.gravar_token(arquivo_token, token)
        except OSError as e:
            raise ErroAoConectar(f"não consegui gravar o {arquivo_token}: confira se dá para gravar na pasta",
                                 repr(e)) from e
    except ErroAoConectar as e:
        print(f"Não deu para conectar o Spotify: {e}.", file=sys.stderr)
        if e.detalhe:
            print(f"(detalhe técnico: {e.detalhe})", file=sys.stderr)
        sys.exit(1)
    print("\nSpotify conectado! O servidor já pode tocar música.")
    print("A autorização dura 6 meses: depois disso, rode python spotify_conectar.py de novo.")
    print(f"O arquivo {Path(arquivo_token).name} dá acesso à sua conta do Spotify: não mande para ninguém (ele já "
          "está no .gitignore). Se o servidor roda em outro aparelho, copie esse arquivo para a pasta "
          "celular_servidor de lá.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelado.")
