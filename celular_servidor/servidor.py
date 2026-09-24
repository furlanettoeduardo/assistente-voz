"""
Servidor do celular (roda no Termux ou, durante o desenvolvimento, no próprio PC).

Fluxo: áudio -> Whisper (Groq) -> LLM com ferramentas -> agente do PC -> resposta em texto.

Rodar:  python servidor.py   e abrir http://localhost:8000 no navegador do mesmo aparelho.
"""
import json
import re
import sys
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

BASE = Path(__file__).parent


def carregar_config(caminho: Path, obrigatorias: tuple[str, ...]) -> dict:
    """Lê o JSON de configuração ou encerra explicando o que fazer."""
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
    return config


CFG = carregar_config(BASE / "config_servidor.json", (
    "groq_api_key", "stt_model", "llm_base_url", "llm_api_key", "llm_model", "pc_url", "pc_token"))

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
    except requests.RequestException:
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


def executar_ferramenta(nome: str, args: dict) -> dict:
    if nome == "abrir_programa":
        try:
            r = requests.post(f"{PC_URL}/abrir", headers=PC_HEADERS,
                              json={"programa": args.get("nome", "")}, timeout=5)
            return r.json()
        except requests.RequestException as e:
            return {"erro": f"não consegui falar com o computador: {e}"}
    return {"erro": f"ferramenta desconhecida: {nome}"}


# ---------- LLM ----------

def limpar(texto: str) -> str:
    """Remove o raciocínio <think>...</think> que alguns modelos Qwen devolvem."""
    return re.sub(r"<think>.*?</think>", "", texto or "", flags=re.DOTALL).strip()


def conversar(texto_usuario: str) -> tuple[str, list[dict]]:
    programas = programas_disponiveis()
    ferramentas = montar_ferramentas(programas)
    sistema = PROMPT_SISTEMA
    if not programas:
        sistema += " O computador está desligado ou inacessível agora; avise se pedirem um programa."

    mensagens = [{"role": "system", "content": sistema},
                 {"role": "user", "content": texto_usuario}]
    acoes = []

    for _ in range(3):  # no máximo 3 rodadas de ferramenta por comando
        payload = {"model": CFG["llm_model"], "messages": mensagens, "temperature": 0.3}
        if ferramentas:
            payload.update(tools=ferramentas, tool_choice="auto")

        r = requests.post(f"{LLM_URL}/chat/completions", headers=LLM_HEADERS,
                          json=payload, timeout=60)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        chamadas = msg.get("tool_calls") or []

        if not chamadas:
            return limpar(msg.get("content")), acoes

        mensagens.append({"role": "assistant", "content": msg.get("content") or "",
                          "tool_calls": chamadas})
        for chamada in chamadas:
            nome = chamada["function"]["name"]
            try:
                args = json.loads(chamada["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            resultado = executar_ferramenta(nome, args)
            acoes.append({"ferramenta": nome, "argumentos": args, "resultado": resultado})
            mensagens.append({"role": "tool", "tool_call_id": chamada["id"],
                              "content": json.dumps(resultado, ensure_ascii=False)})

    return "Fiz o que consegui, mas algo não saiu como esperado.", acoes


# ---------- Rotas ----------

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
        if not texto:
            return jsonify(erro="Não entendi nada no áudio. Tente de novo."), 400
        resposta, acoes = conversar(texto)
    except requests.HTTPError as e:
        return jsonify(erro=f"Erro na API: {e.response.status_code} {e.response.text[:200]}"), 502
    except requests.RequestException as e:
        return jsonify(erro=f"Sem conexão com a API: {e}"), 502
    return jsonify(transcricao=texto, resposta=resposta, acoes=acoes)


@app.post("/texto")
def texto():
    frase = (request.get_json(silent=True) or {}).get("texto", "").strip()
    if not frase:
        return jsonify(erro="Digite um comando."), 400
    try:
        resposta, acoes = conversar(frase)
    except requests.HTTPError as e:
        return jsonify(erro=f"Erro na API: {e.response.status_code} {e.response.text[:200]}"), 502
    except requests.RequestException as e:
        return jsonify(erro=f"Sem conexão com a API: {e}"), 502
    return jsonify(transcricao=frase, resposta=resposta, acoes=acoes)


if __name__ == "__main__":
    app.run(host=CFG.get("host", "127.0.0.1"), port=int(CFG.get("porta", 8000)))
