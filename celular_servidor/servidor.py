"""
Servidor do celular (roda no Termux ou, durante o desenvolvimento, no próprio PC).

Fluxo: áudio -> Whisper (Groq) -> LLM com ferramentas -> agente do PC -> resposta em texto.

Rodar:  python servidor.py   e abrir http://localhost:8000 no navegador do mesmo aparelho.
"""
import json
import re
import sys
import traceback
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE = Path(__file__).parent
ARQUIVO_CONFIG = BASE / "config_servidor.json"
TIPOS = {str: "um texto entre aspas", dict: "um objeto entre chaves { }"}


def carregar_config(caminho: Path, obrigatorias: dict) -> dict:
    """
    Lê o JSON de configuração ou encerra explicando o que fazer. `obrigatorias` diz o tipo
    de cada chave que precisa existir (None aceita qualquer tipo).
    """
    exemplo = caminho.with_name(f"{caminho.stem}.example.json")
    try:
        texto = caminho.read_text(encoding="utf-8-sig")  # -sig aceita o BOM do Bloco de Notas
    except FileNotFoundError:
        sys.exit(
            f"Arquivo de configuração não encontrado: {caminho}\n"
            f"Copie {exemplo.name} para {caminho.name}, na mesma pasta, e preencha os seus dados."
        )
    except UnicodeDecodeError:
        sys.exit(f"{caminho} não está em UTF-8. Abra no editor e salve com a codificação UTF-8.")
    try:
        config = json.loads(texto)
    except json.JSONDecodeError as e:
        sys.exit(
            f"Erro de JSON em {caminho}, linha {e.lineno}, coluna {e.colno}: {e.msg}\n"
            "Confira vírgulas e aspas nos valores."
        )
    if not isinstance(config, dict):
        sys.exit(f"{caminho} precisa ser um objeto JSON {{...}}, como em {exemplo.name}.")
    faltando = [chave for chave in obrigatorias if chave not in config]
    if faltando:
        sys.exit(f"Faltam chaves em {caminho}: {', '.join(faltando)}. Compare com {exemplo.name}.")
    for chave, tipo in obrigatorias.items():
        if tipo is not None and not isinstance(config[chave], tipo):
            sys.exit(f'Em {caminho}, "{chave}" precisa ser {TIPOS[tipo]}. Compare com {exemplo.name}.')
    return config


def ler_porta(config: dict, caminho: Path, padrao: int) -> int:
    """A porta pode vir como número ou texto ("8000"), mas precisa estar entre 0 e 65535."""
    try:
        porta = int(config.get("porta", padrao))
    except (TypeError, ValueError):
        porta = -1
    if not 0 <= porta <= 65535:
        sys.exit(f'Em {caminho}, "porta" precisa ser um número entre 0 e 65535, como {padrao}.')
    return porta


CFG = carregar_config(ARQUIVO_CONFIG, dict.fromkeys((
    "groq_api_key", "stt_model", "llm_base_url", "llm_api_key", "llm_model", "pc_url", "pc_token"), str))
for _chave in ("pc_url", "llm_base_url"):
    if not CFG[_chave].startswith(("http://", "https://")):
        sys.exit(f'Em {ARQUIVO_CONFIG}, "{_chave}" precisa começar com http:// ou https://, '
                 "como em config_servidor.example.json.")
# Chaves e token vão em cabeçalho HTTP, que só aceita latin-1: aspas curvas ou espaços invisíveis
# colados junto derrubariam cada pedido com UnicodeEncodeError.
for _chave in ("groq_api_key", "llm_api_key", "pc_token"):
    CFG[_chave] = CFG[_chave].strip()
    if not CFG[_chave]:
        sys.exit(f'Em {ARQUIVO_CONFIG}, "{_chave}" está vazio. Preencha como explica o README.')
    if not CFG[_chave].isascii():
        sys.exit(f'Em {ARQUIVO_CONFIG}, "{_chave}" tem um caractere que não pode ir numa chave: '
                 "acento, aspas curvas (“ ”) ou espaço invisível. Apague o valor e cole de novo da fonte original.")
HOST = CFG.get("host", "127.0.0.1")
if not isinstance(HOST, str) or not HOST:
    sys.exit(f'Em {ARQUIVO_CONFIG}, "host" precisa ser um texto, como "127.0.0.1".')
PORTA = ler_porta(CFG, ARQUIVO_CONFIG, 8000)

GROQ_URL = "https://api.groq.com/openai/v1"
PC_URL = CFG["pc_url"].rstrip("/")
PC_HEADERS = {"Authorization": f"Bearer {CFG['pc_token']}"}
LLM_URL = CFG["llm_base_url"].rstrip("/")
LLM_HEADERS = {"Authorization": f"Bearer {CFG['llm_api_key']}"}

PROMPT_SISTEMA = (
    "Você é uma assistente de voz doméstica em português do Brasil. "
    "Suas respostas serão lidas em voz alta: responda em uma ou duas frases curtas, "
    "sem markdown, listas ou emojis. Use as ferramentas apenas quando o usuário pedir "
    "uma ação. Se ele pedir um programa que não está na lista, diga quais estão disponíveis."
)

app = Flask(__name__, static_folder="static")


# ---------- Transcrição ----------

def transcrever(audio: bytes, mime: str) -> str:
    mime = (mime or "audio/webm").split(";")[0]
    extensao = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
                "audio/wav": "wav", "audio/mpeg": "mp3"}.get(mime, "webm")
    r = requests.post(
        f"{GROQ_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {CFG['groq_api_key']}"},
        files={"file": (f"audio.{extensao}", audio, mime)},
        data={"model": CFG["stt_model"], "language": "pt", "response_format": "json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["text"].strip()


# ---------- Ferramentas ----------

def programas_disponiveis() -> list[str]:
    """Pergunta ao PC quais programas estão liberados. Lista vazia se o PC estiver fora."""
    try:
        r = requests.get(f"{PC_URL}/programas", headers=PC_HEADERS, timeout=3)
        r.raise_for_status()
        return r.json()["programas"]
    except requests.RequestException as e:
        if getattr(e.response, "status_code", None) == 401:
            print("[servidor] o agente do PC recusou o pc_token: ele precisa ser igual ao token do agente.",
                  file=sys.stderr)
        else:
            print(f"[servidor] não consegui falar com o agente do PC em {PC_URL} (detalhe técnico: {e})",
                  file=sys.stderr)
        return []


def montar_ferramentas(programas: list[str]) -> list[dict]:
    if not programas:
        return []
    return [{
        "type": "function",
        "function": {
            "name": "abrir_programa",
            "description": "Abre um programa no computador do usuário.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "enum": programas,
                             "description": "Nome do programa a abrir."}
                },
                "required": ["nome"],
            },
        },
    }]


def ler_argumentos(bruto) -> dict:
    """Os argumentos vêm do LLM: aceita objeto ou texto JSON de objeto e descarta o resto."""
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto or "{}")
        except json.JSONDecodeError:
            return {}
    return bruto if isinstance(bruto, dict) else {}


def executar_ferramenta(nome: str, args: dict) -> dict:
    if nome == "abrir_programa":
        try:
            r = requests.post(f"{PC_URL}/abrir", headers=PC_HEADERS,
                              json={"programa": args.get("nome", "")}, timeout=5)
            return r.json()
        except requests.RequestException as e:
            print(f"[servidor] não consegui falar com o agente do PC em {PC_URL} (detalhe técnico: {e})",
                  file=sys.stderr)
            return {"erro": "não consegui falar com o computador"}
    return {"erro": f"ferramenta desconhecida: {nome}"}


# ---------- LLM ----------

def limpar(texto: str) -> str:
    """Remove o raciocínio <think>...</think> que alguns modelos Qwen devolvem."""
    # Um <think> sem fechamento (resposta cortada) vai até o fim do texto.
    texto = re.sub(r"<think>.*?(?:</think>|$)", "", texto or "", flags=re.DOTALL)
    # Quando o template do modelo já abre o <think>, só o fechamento aparece: descarta até ele.
    texto = re.sub(r"^.*?</think>", "", texto, flags=re.DOTALL)
    return texto.strip()


def resumir(acoes: list[dict]) -> str:
    """Resposta de reserva quando o modelo não fecha com um texto: conta o que foi feito."""
    abertos = [a["resultado"]["programa"] for a in acoes
               if isinstance(a["resultado"], dict) and a["resultado"].get("ok")]
    if abertos:
        return f"Pronto, abri {' e '.join(abertos)}."
    return "Fiz o que consegui, mas algo não saiu como esperado."


def conversar(texto_usuario: str) -> tuple[str, list[dict]]:
    programas = programas_disponiveis()
    ferramentas = montar_ferramentas(programas)
    sistema = PROMPT_SISTEMA
    if not programas:
        sistema += " O computador está desligado ou inacessível agora; avise se pedirem um programa."

    mensagens = [{"role": "system", "content": sistema},
                 {"role": "user", "content": texto_usuario}]
    acoes = []
    executadas = {}  # (ferramenta, argumentos) -> resultado

    for rodada in range(1, 4):  # no máximo 3 rodadas por comando
        ultima = rodada == 3
        payload = {"model": CFG["llm_model"], "messages": mensagens, "temperature": 0.3}
        if ferramentas:
            # Na última rodada o modelo só pode responder em texto: uma ferramenta pedida ali
            # seria executada sem que o resultado voltasse para ele.
            payload.update(tools=ferramentas, tool_choice="none" if ultima else "auto")

        r = requests.post(f"{LLM_URL}/chat/completions", headers=LLM_HEADERS,
                          json=payload, timeout=60)
        if ultima and r.status_code == 400:
            # Alguns modelos insistem na ferramenta mesmo com tool_choice="none", e o Groq responde 400.
            print(f"[servidor] o LLM recusou a última rodada (detalhe técnico: {r.text[:500]})", file=sys.stderr)
            return resumir(acoes), acoes
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        chamadas = msg.get("tool_calls") or []

        if not chamadas:
            return limpar(msg.get("content")), acoes
        if ultima:  # backend que ignora tool_choice="none" (o Ollama, por exemplo): não executa nada
            return limpar(msg.get("content")) or resumir(acoes), acoes

        mensagens.append({"role": "assistant", "content": msg.get("content") or "",
                          "tool_calls": chamadas})
        for chamada in chamadas:
            nome = chamada["function"]["name"]
            args = ler_argumentos(chamada["function"].get("arguments"))
            chave = (nome, json.dumps(args, sort_keys=True))
            if chave not in executadas:  # modelos pequenos repetem a mesma chamada
                executadas[chave] = executar_ferramenta(nome, args)
                acoes.append({"ferramenta": nome, "argumentos": args, "resultado": executadas[chave]})
            mensagens.append({"role": "tool", "tool_call_id": chamada["id"],
                              "content": json.dumps(executadas[chave], ensure_ascii=False)})


# ---------- Erros das APIs ----------

def explicar_erro(e: requests.RequestException, servico: str) -> str:
    """
    Traduz a falha de uma API externa numa frase em português para a página. O detalhe técnico
    (em inglês, do jeito que a API mandou) fica só no terminal.
    """
    servico_maiusculo = servico[0].upper() + servico[1:]
    resposta = e.response
    if resposta is None:
        print(f"[servidor] falha ao falar com {servico} (detalhe técnico: {e})", file=sys.stderr)
        if isinstance(e, requests.exceptions.JSONDecodeError):
            return f"{servico_maiusculo} mandou uma resposta que o servidor não entendeu. Tente de novo."
        if isinstance(e, requests.Timeout) and not isinstance(e, requests.ConnectionError):
            return f"{servico_maiusculo} demorou demais para responder. Tente de novo."
        return f"Sem conexão com {servico}. Confira a internet do celular e tente de novo."

    status, corpo = resposta.status_code, resposta.text[:500]
    print(f"[servidor] {servico} respondeu {status} (detalhe técnico: {corpo})", file=sys.stderr)
    if status == 401:
        return f"{servico_maiusculo} recusou a chave. Confira a chave no config_servidor.json."
    if status == 404:
        return f"{servico_maiusculo} não encontrou o modelo ou o endereço. Confira o modelo e a URL no config."
    if status == 413:
        return f"{servico_maiusculo} achou o pedido grande demais. Tente um comando mais curto."
    if status == 429:
        return f"{servico_maiusculo} avisou que o limite de uso acabou por enquanto. Espere um pouco e tente de novo."
    if "decommissioned" in corpo:
        return f"{servico_maiusculo} avisou que o modelo configurado saiu do ar. Escolha outro na lista de modelos."
    if "tool_use_failed" in corpo:
        return "O modelo se confundiu ao usar a ferramenta. Tente de novo."
    if status >= 500:
        return f"{servico_maiusculo} está com problemas agora. Tente de novo em instantes."
    return f"{servico_maiusculo} recusou o pedido (erro {status}). Veja os detalhes no terminal do servidor."


def responder(frase: str):
    try:
        resposta, acoes = conversar(frase)
    except requests.RequestException as e:
        return jsonify(erro=explicar_erro(e, "a API do LLM")), 502
    return jsonify(transcricao=frase, resposta=resposta, acoes=acoes)


# ---------- Rotas ----------

MENSAGENS_HTTP = {
    404: "Endereço não encontrado no servidor.",
    405: "Esse endereço não aceita esse tipo de pedido.",
    413: "O pedido é grande demais.",
}


@app.errorhandler(HTTPException)
def erro_http(e):
    """Erros do Flask (404, 405...) em JSON e em português, no lugar da página HTML em inglês."""
    return jsonify(erro=MENSAGENS_HTTP.get(e.code, f"O servidor respondeu com erro {e.code}.")), e.code


@app.errorhandler(Exception)
def erro_inesperado(e):
    traceback.print_exception(e)
    print("[servidor] erro inesperado ao atender o pedido; detalhes acima.", file=sys.stderr)
    return jsonify(erro="Erro inesperado no servidor. Veja o terminal do Termux e tente de novo."), 500


@app.get("/")
def pagina():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/voz")
def voz():
    audio = request.get_data()
    if len(audio) < 1000:
        return jsonify(erro="Áudio curto demais. Segure o botão enquanto fala."), 400
    try:
        texto = transcrever(audio, request.content_type)
    except requests.RequestException as e:
        return jsonify(erro=explicar_erro(e, "a API de transcrição")), 502
    if not texto:
        return jsonify(erro="Não entendi nada no áudio. Tente de novo."), 400
    return responder(texto)


@app.post("/texto")
def texto():
    dados = request.get_json(silent=True)
    frase = dados.get("texto") if isinstance(dados, dict) else None
    frase = frase.strip() if isinstance(frase, str) else ""
    if not frase:
        return jsonify(erro="Digite um comando."), 400
    return responder(frase)


if __name__ == "__main__":
    app.run(host=HOST, port=PORTA)
