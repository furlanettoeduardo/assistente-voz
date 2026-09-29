"""
Servidor do celular (roda no Termux ou, durante o desenvolvimento, no próprio PC).

Fluxo: áudio -> Whisper (Groq) -> LLM com ferramentas -> agente do PC -> resposta em texto.

Rodar:  python servidor.py   e abrir http://localhost:8000 no navegador do mesmo aparelho.
"""
import base64
import errno
import importlib.util
import ipaddress
import json
import logging
import re
import socket
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

try:
    import requests
    from flask import Flask, jsonify, request, send_from_directory
    from werkzeug.exceptions import HTTPException
    from werkzeug.serving import make_server
except ImportError as e:  # o .venv não foi ativado ou o requirements não foi instalado
    sys.exit(f"Falta a biblioteca {e.name}. Na pasta celular_servidor, rode: pip install -r requirements.txt\n"
             "No PC, ative antes o .venv; no Termux, dá para rodar bash scripts/termux-instalar.sh na pasta do projeto.")

import lampada
import spotify
import tempo
import voz

BASE = Path(__file__).parent
ARQUIVO_CONFIG = BASE / "config_servidor.json"
TIPOS = {str: "um texto entre aspas", dict: "um objeto entre chaves { }"}
# Motivos que o módulo json dá em inglês, pelo começo da mensagem (o 3.13 mudou o da vírgula sobrando).
MOTIVOS_JSON = (
    ("Expecting ',' delimiter", "falta uma vírgula entre dois itens"),
    ("Expecting ':' delimiter", "faltam os dois-pontos entre o nome e o valor"),
    ("Expecting property name", "vírgula sobrando antes do } ou nome sem aspas duplas"),
    ("Illegal trailing comma", "vírgula sobrando antes do } ou do ]"),
    ("Expecting value", "falta um valor, ou tem uma vírgula sobrando"),
    ("Invalid \\escape", "barra invertida simples dentro de um texto"),
    ("Invalid \\uXXXX escape", "barra invertida simples dentro de um texto"),  # caminhos como D:\utilitarios
    ("Unterminated string", "aspas abertas que não foram fechadas"),
    ("Invalid control character", "quebra de linha ou tabulação dentro de um texto entre aspas"),
    ("Extra data", "tem texto sobrando depois do último }"),
)


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
        motivo = next((pt for en, pt in MOTIVOS_JSON if e.msg.startswith(en)),
                      f"formato inválido (detalhe técnico: {e.msg})")
        sys.exit(
            f"Erro de JSON em {caminho}, linha {e.lineno}, coluna {e.colno}: {motivo}.\n"
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


def url_valida(url: str) -> bool:
    """http(s)://, um host que o requests consiga usar e, se houver, uma porta que exista."""
    try:
        partes = urlsplit(url)
        partes.port  # levanta ValueError para porta inválida ou colchete sobrando
        host = partes.hostname or ""
        host.encode("idna")  # levanta UnicodeError (um ValueError) com rótulo vazio, como em "192.168.0..10"
    except ValueError:
        return False
    return (partes.scheme in ("http", "https") and bool(host) and host.isascii()
            and not host.startswith((".", "*")) and not any(c.isspace() for c in url))


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
    if not url_valida(CFG[_chave].strip()):
        sys.exit(f'Em {ARQUIVO_CONFIG}, "{_chave}" precisa ser um endereço completo, começando com http:// '
                 "ou https://, como em config_servidor.example.json.")
    CFG[_chave] = CFG[_chave].strip()
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


def ler_exemplo() -> dict:
    """config_servidor.example.json, para reconhecer os campos que ainda estão com o valor de exemplo."""
    try:
        exemplo = json.loads((BASE / "config_servidor.example.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return exemplo if isinstance(exemplo, dict) else {}


EXEMPLO = ler_exemplo()

# A lâmpada é opcional: sem o bloco "lampada", ou com campos por preencher, o servidor funciona sem ela.
try:
    _CONFIG_LAMPADA, LAMPADA_FALTANDO = lampada.ler_config(CFG.get("lampada"), EXEMPLO.get("lampada"))
except ValueError as e:
    sys.exit(f"Em {ARQUIVO_CONFIG}, {e}")
if _CONFIG_LAMPADA and importlib.util.find_spec("tinytuya") is None:
    sys.exit("Falta a biblioteca tinytuya, usada pela lâmpada. Na pasta celular_servidor, rode: "
             "pip install -r requirements.txt\nNo PC, ative antes o .venv; no Termux, rode bash "
             "scripts/termux-instalar.sh na pasta do projeto, que instala também o python-cryptography.")
LAMPADA = lampada.Lampada(_CONFIG_LAMPADA) if _CONFIG_LAMPADA else None


def aviso_da_lampada() -> str:
    """Uma linha para o terminal dizendo se a lâmpada está em uso."""
    if LAMPADA is not None:
        return f"[servidor] lâmpada em {LAMPADA.config['ip']} (protocolo {LAMPADA.config['versao']})."
    if LAMPADA_FALTANDO:
        return (f"[servidor] lâmpada ainda não configurada: falta preencher {', '.join(LAMPADA_FALTANDO)} no "
                "bloco \"lampada\" do config_servidor.json (se não tiver lâmpada, apague o bloco).")
    return "[servidor] nenhuma lâmpada configurada."


def texto_opcional(bloco: dict, chave: str, exemplo: dict, onde: str = "") -> str | None:
    """Texto opcional do config: ausente, vazio ou ainda com o valor de exemplo vale como não preenchido."""
    valor = bloco.get(chave)
    if valor is None:
        return None
    if not isinstance(valor, str):
        sys.exit(f'Em {ARQUIVO_CONFIG}, "{chave}"{onde} precisa ser {TIPOS[str]}, como em config_servidor.example.json.')
    valor = valor.strip()
    return valor if valor and valor != exemplo.get(chave) else None


# Previsão do tempo: "cidade" é só a cidade padrão; o usuário pode perguntar de outra.
CIDADE = texto_opcional(CFG, "cidade", EXEMPLO)
TEMPO = tempo.Tempo(CIDADE)

# Wake-on-LAN: com "pc_mac", o servidor consegue ligar o PC pela rede (quando roda em outro aparelho).
PC_MAC = texto_opcional(CFG, "pc_mac", EXEMPLO)
if PC_MAC and not re.fullmatch(r"[0-9A-Fa-f]{2}([:-]?)[0-9A-Fa-f]{2}(\1[0-9A-Fa-f]{2}){4}", PC_MAC):
    sys.exit(f'Em {ARQUIVO_CONFIG}, "pc_mac" precisa ser o endereço MAC da placa de rede do PC, como '
             f'AA:BB:CC:DD:EE:FF (veio "{PC_MAC}").')
PC_BROADCAST = texto_opcional(CFG, "pc_broadcast", EXEMPLO) or "255.255.255.255"
try:
    ipaddress.IPv4Address(PC_BROADCAST)
except ValueError:
    sys.exit(f'Em {ARQUIVO_CONFIG}, "pc_broadcast" precisa ser um endereço IPv4, como 192.168.0.255 '
             f'(veio "{PC_BROADCAST}").')

# Spotify: com o "client_id" do app criado no painel do Spotify; o token fica no spotify_token.json.
_BLOCO_SPOTIFY = CFG.get("spotify")
if _BLOCO_SPOTIFY is not None and not isinstance(_BLOCO_SPOTIFY, dict):
    sys.exit(f'Em {ARQUIVO_CONFIG}, "spotify" precisa ser {TIPOS[dict]}, como em config_servidor.example.json.')
_EXEMPLO_SPOTIFY = EXEMPLO.get("spotify") if isinstance(EXEMPLO.get("spotify"), dict) else {}
_CLIENT_ID = texto_opcional(_BLOCO_SPOTIFY or {}, "client_id", _EXEMPLO_SPOTIFY, ' do bloco "spotify"')
SPOTIFY = spotify.Spotify(_CLIENT_ID, dispositivo=texto_opcional(
    _BLOCO_SPOTIFY or {}, "dispositivo", _EXEMPLO_SPOTIFY, ' do bloco "spotify"')) if _CLIENT_ID else None


# Voz das respostas: com "voz" (uma voz do Piper baixada na pasta vozes), o servidor manda o áudio junto
# com o texto. Sem o piper-tts ou sem o arquivo, vale a voz do navegador, e o terminal explica.
_NOME_DA_VOZ = CFG.get("voz")
if _NOME_DA_VOZ is not None and not isinstance(_NOME_DA_VOZ, str):
    sys.exit(f'Em {ARQUIVO_CONFIG}, "voz" precisa ser {TIPOS[str]}, como em config_servidor.example.json.')
_NOME_DA_VOZ = (_NOME_DA_VOZ or "").strip() or None
if _NOME_DA_VOZ and not re.fullmatch(r"[A-Za-z0-9_.-]+", _NOME_DA_VOZ):
    sys.exit(f'Em {ARQUIVO_CONFIG}, "voz" precisa ser o nome de uma voz do Piper, como "pt_BR-cadu-medium" '
             f'(veio "{_NOME_DA_VOZ}").')
VOZ, ERRO_DA_VOZ, AVISO_DA_VOZ = voz.preparar(_NOME_DA_VOZ)


def aviso_do_spotify() -> str | None:
    """Uma linha para o terminal quando o Spotify está configurado."""
    if SPOTIFY is None:
        return None
    if SPOTIFY.conectado():
        return "[servidor] Spotify conectado."
    return "[servidor] Spotify ainda não conectado: na pasta celular_servidor, rode python spotify_conectar.py."


GROQ_URL = "https://api.groq.com/openai/v1"
PC_URL = CFG["pc_url"].rstrip("/")
PC_HEADERS = {"Authorization": f"Bearer {CFG['pc_token']}"}
LLM_URL = CFG["llm_base_url"].rstrip("/")
LLM_HEADERS = {"Authorization": f"Bearer {CFG['llm_api_key']}"}

PROMPT_SISTEMA = (
    "Você é uma assistente de voz doméstica em português do Brasil. "
    "Suas respostas serão lidas em voz alta: responda em uma ou duas frases curtas, "
    "sem markdown, listas ou emojis. Use as ferramentas quando o usuário pedir uma ação ou uma "
    "informação que só elas trazem, como a previsão do tempo. Para agir, chame sempre a ferramenta: "
    "nunca diga que fez algo sem tê-la chamado. Se ele pedir um programa que não está na lista, diga "
    "quais estão disponíveis."
)

DIAS_DA_SEMANA = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado",
                  "domingo")
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")


def agora() -> datetime:
    return datetime.now().astimezone()  # hora do aparelho que roda o servidor, sem depender do tzdata


def descrever_momento(momento: datetime) -> str:
    """Data e hora por extenso, sem depender do locale do sistema (que no Windows vem em inglês)."""
    return (f"Agora é {DIAS_DA_SEMANA[momento.weekday()]}, {momento.day} de {MESES[momento.month - 1]} de "
            f"{momento.year}, {momento:%H:%M}.")


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

def consultar_pc(avisar: bool = True) -> dict:
    """
    O que o agente do PC oferece: programas que abre, programas que fecha e ações liberadas. Com o PC
    inacessível vem tudo vazio, e "fora_do_ar" diz se ele nem respondeu (aí o Wake-on-LAN ajuda; com o
    token errado, não).
    """
    try:
        r = requests.get(f"{PC_URL}/programas", headers=PC_HEADERS, timeout=3)
        r.raise_for_status()
        dados = r.json()
    except requests.RequestException as e:  # inclui o JSON inválido
        status = getattr(e.response, "status_code", None)
        if avisar and status == 401:
            print("[servidor] o agente do PC recusou o pc_token: ele precisa ser igual ao token do agente.",
                  file=sys.stderr)
        elif avisar:
            print(f"[servidor] não consegui falar com o agente do PC em {PC_URL} (detalhe técnico: {e})",
                  file=sys.stderr)
        return {"programas": [], "fechaveis": [], "acoes": [], "fora_do_ar": status is None}

    def lista(chave: str) -> list[str]:  # agentes antigos só mandam "programas"
        valor = dados.get(chave) if isinstance(dados, dict) else None
        return [str(item) for item in valor] if isinstance(valor, list) else []

    return {"programas": lista("programas"), "fechaveis": lista("fechaveis"), "acoes": lista("acoes"),
            "fora_do_ar": False}


def ferramenta(nome: str, descricao: str, propriedades: dict | None = None, obrigatorias=()) -> dict:
    parametros = {"type": "object", "properties": propriedades or {}}
    if obrigatorias:
        parametros["required"] = list(obrigatorias)
    return {"type": "function", "function": {"name": nome, "description": descricao, "parameters": parametros}}


ACOES_DE_VOLUME = ("consultar", "definir", "aumentar", "diminuir", "mudo", "tirar_mudo")


FERRAMENTA_LAMPADA = {
    "type": "function",
    "function": {
        "name": "controlar_lampada",
        "description": "Controla a lâmpada inteligente da casa: liga, desliga, muda o brilho ou a cor. "
                       "Mande só o que o usuário pediu.",
        "parameters": {
            "type": "object",
            "properties": {
                "ligar": {"type": "boolean", "description": "true para ligar, false para desligar."},
                "brilho": {"type": "integer", "minimum": 1, "maximum": 100,
                           "description": "Brilho em porcentagem, de 1 a 100."},
                "cor": {"type": "string", "enum": lampada.NOMES_DAS_CORES,
                        "description": "Cor da luz. Para luz branca, use branco (o branco puro); branco quente "
                                       "é amarelado e só vale quando o usuário pedir luz quente ou amarelada."},
            },
        },
    },
}


def montar_ferramentas(pc: dict) -> list[dict]:
    """Só as ferramentas que funcionam agora: o que o agente liberou e o que está configurado no servidor."""
    ferramentas = []
    if pc["programas"]:
        ferramentas.append(ferramenta("abrir_programa", "Abre um programa no computador do usuário.", {
            "nome": {"type": "string", "enum": pc["programas"], "description": "Nome do programa a abrir."}},
            ["nome"]))
    if pc["fechaveis"]:
        ferramentas.append(ferramenta("fechar_programa", "Fecha um programa aberto no computador do usuário.", {
            "nome": {"type": "string", "enum": pc["fechaveis"], "description": "Nome do programa a fechar."}},
            ["nome"]))
    if "volume" in pc["acoes"]:
        ferramentas.append(ferramenta("volume_do_pc", "Consulta ou muda o volume do som do computador.", {
            "acao": {"type": "string", "enum": list(ACOES_DE_VOLUME),
                     "description": "consultar diz o volume atual; definir põe no nível pedido; aumentar e "
                                    "diminuir mudam o volume em nivel pontos (10 se o usuário não disser "
                                    "quanto); mudo e tirar_mudo."},
            "nivel": {"type": "integer", "minimum": 0, "maximum": 100,
                      "description": "Porcentagem: o volume final em definir, ou quanto mudar em aumentar "
                                     "e diminuir."}}, ["acao"]))
    energia = (["bloquear"] if "bloquear" in pc["acoes"] else []) + (
        ["desligar", "cancelar"] if "desligar" in pc["acoes"] else [])
    if energia:
        ferramentas.append(ferramenta(
            "energia_do_pc", "Bloqueia a tela do computador, desliga o computador ou cancela um desligamento "
                             "agendado. Para desligar, o servidor pede confirmação ao usuário.", {
                "acao": {"type": "string", "enum": energia}}, ["acao"]))
    if pc["fora_do_ar"] and PC_MAC:
        ferramentas.append(ferramenta(
            "ligar_pc", "Liga o computador pela rede quando ele está desligado. Use antes de fazer algo no "
                        "computador, se ele estiver desligado."))
    if LAMPADA is not None:
        ferramentas.append(FERRAMENTA_LAMPADA)
    ferramentas.append(ferramenta(
        "previsao_do_tempo", "Consulta a previsão do tempo (temperatura e chuva) de hoje até daqui a 6 dias.", {
            "cidade": {"type": "string",
                       "description": f"Cidade, só se o usuário disser uma; sem ela, vale {CIDADE}." if CIDADE
                       else "Cidade, com a sigla do estado quando ajudar, como Curitiba, PR."},
            "dias_a_frente": {"type": "integer", "minimum": 0, "maximum": 6,
                              "description": "0 para agora ou hoje, 1 para amanhã, e assim por diante."}}))
    if SPOTIFY is not None and SPOTIFY.conectado():
        ferramentas.append(ferramenta("tocar_musica", "Toca música no Spotify do computador.", {
            "busca": {"type": "string",
                      "description": "O que tocar, do jeito que o usuário disse: música, artista, álbum ou playlist."},
            "tipo": {"type": "string", "enum": list(spotify.TIPOS),
                     "description": "musica, artista, album ou playlist."}}, ["busca"]))
        ferramentas.append(ferramenta(
            "controlar_musica", "Pausa, continua, pula para a próxima ou volta para a anterior no Spotify.", {
                "acao": {"type": "string", "enum": list(spotify.ACOES)}}, ["acao"]))
    return ferramentas


def ler_argumentos(bruto) -> dict:
    """Os argumentos vêm do LLM: aceita objeto ou texto JSON de objeto e descarta o resto."""
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto or "{}")
        except json.JSONDecodeError:
            return {}
    return bruto if isinstance(bruto, dict) else {}


def chamadas_validas(msg: dict) -> list[dict]:
    """tool_calls que dá para executar e responder: com id e com o nome da função."""
    brutas = msg.get("tool_calls")
    if not isinstance(brutas, list):
        return []
    return [c for c in brutas if isinstance(c, dict) and c.get("id")
            and isinstance(c.get("function"), dict) and c["function"].get("name")]


def pedir_ao_pc(rota: str, dados: dict, timeout: float = 5) -> dict:
    """Manda um pedido ao agente e devolve o JSON dele; uma queda da rede vira erro em português."""
    try:
        r = requests.post(f"{PC_URL}{rota}", headers=PC_HEADERS, json=dados, timeout=timeout)
        resposta = r.json()
    except requests.RequestException as e:  # inclui o JSON inválido
        print(f"[servidor] não consegui falar com o agente do PC em {PC_URL} (detalhe técnico: {e})",
              file=sys.stderr)
        return {"erro": "não consegui falar com o computador"}
    return resposta if isinstance(resposta, dict) else {"erro": "o computador mandou uma resposta que não entendi"}


def executar_ferramenta(nome: str, args: dict, pc: dict) -> dict:
    if nome == "abrir_programa":
        return pedir_ao_pc("/abrir", {"programa": args.get("nome", "")})
    if nome == "fechar_programa":  # o agente espera uns segundos para conferir se fechou
        return pedir_ao_pc("/fechar", {"programa": args.get("nome", "")}, timeout=20)
    if nome == "volume_do_pc":
        return pedir_ao_pc("/volume", {chave: args[chave] for chave in ("acao", "nivel") if chave in args}, timeout=10)
    if nome == "energia_do_pc":
        return energia_do_pc(args, pc)
    if nome == "ligar_pc":
        return ligar_pc()
    if nome == "controlar_lampada":
        return controlar_lampada(args)
    if nome == "previsao_do_tempo":
        return previsao_do_tempo(args)
    if nome == "tocar_musica":
        return tocar_musica(args, pc)
    if nome == "controlar_musica":
        return controlar_musica(args)
    return {"erro": f"ferramenta desconhecida: {nome}"}


# ---------- Memória curta da conversa ----------

CONVERSA_PADRAO = "padrao"  # quem não manda um id (um satélite antigo, um teste com curl)
MEMORIA_PEDIDOS = 4  # quantos pedidos anteriores o LLM vê: o bastante para "agora fecha ele"
MEMORIA_SEGUNDOS = 300  # parada por 5 minutos, a conversa recomeça do zero
MEMORIA_CONVERSAS = 50  # limite de conversas guardadas ao mesmo tempo
_memorias: dict = {}  # conversa -> {"pedidos": [[mensagens de um pedido], ...], "ate": instante}
_trava_memoria = threading.Lock()


def ler_conversa(valor) -> str:
    """O id de conversa que a página (ou o satélite) manda; qualquer coisa estranha vira a conversa padrão."""
    if isinstance(valor, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", valor):
        return valor
    return CONVERSA_PADRAO


def lembrar(conversa: str) -> list[dict]:
    """As mensagens dos últimos pedidos desta conversa, na ordem, se ela não ficou parada tempo demais."""
    with _trava_memoria:
        memoria = _memorias.get(conversa)
        if memoria is None or time.monotonic() > memoria["ate"]:
            _memorias.pop(conversa, None)
            return []
        return [mensagem for pedido in memoria["pedidos"] for mensagem in pedido]


def guardar_pedido(conversa: str, mensagens: list[dict]) -> None:
    """Guarda um pedido inteiro (usuário, chamadas de ferramenta, resultados e a resposta final)."""
    limpas = [{**m, "content": limpar(m["content"])} if m["role"] == "assistant" and m.get("content") else m
              for m in mensagens]
    agora_ = time.monotonic()
    with _trava_memoria:
        for vencida in [c for c, m in _memorias.items() if agora_ > m["ate"]]:
            del _memorias[vencida]
        memoria = _memorias.pop(conversa, {"pedidos": []})  # volta para o fim: é a mais recente
        memoria["pedidos"] = (memoria["pedidos"] + [limpas])[-MEMORIA_PEDIDOS:]
        memoria["ate"] = agora_ + MEMORIA_SEGUNDOS
        _memorias[conversa] = memoria
        while len(_memorias) > MEMORIA_CONVERSAS:
            del _memorias[next(iter(_memorias))]  # a mais antiga


# ---------- Confirmação por voz (desligar o PC) ----------

CONFIRMACAO_SEGUNDOS = 30
_pendentes: dict = {}  # conversa -> {"acao": "desligar", "ate": instante em que a pergunta vence}
_trava_pendente = threading.Lock()
PERGUNTA_DESLIGAR = "Quer mesmo desligar o PC? Diga sim para confirmar."
PALAVRAS_SIM = {"sim", "confirmo", "confirma", "pode", "isso", "claro", "ok", "ta", "beleza", "certo", "aham",
                "desliga", "manda", "positivo"}
PALAVRAS_NAO = {"nao", "cancela", "cancelar", "deixa", "esquece", "espera"}
# Só frases curtas contam: "sim, pode desligar o computador" confirma e "não, obrigado" recusa, mas "sim, e abre
# o chrome" ou "deixa a luz azul" são outros pedidos e seguem para o LLM.
PALAVRAS_DA_CONFIRMACAO = PALAVRAS_SIM | {"desligar", "o", "pc", "computador", "por", "favor", "quero", "tenho",
                                          "certeza", "confirmado", "ser", "agora", "bom", "mesmo", "que", "obrigado",
                                          "obrigada", "ja", "logo"}
PALAVRAS_DA_RECUSA = PALAVRAS_NAO | {"o", "pc", "computador", "desligar", "desliga", "desligamento", "pra", "pa",
                                     "la", "obrigado", "obrigada", "precisa", "quero", "mais", "tarde", "isso"}


def guardar_pendente(acao: str, conversa: str = CONVERSA_PADRAO) -> None:
    with _trava_pendente:
        _pendentes[conversa] = {"acao": acao, "ate": time.monotonic() + CONFIRMACAO_SEGUNDOS}


def tirar_pendente(conversa: str = CONVERSA_PADRAO) -> str | None:
    """
    A ação esperando confirmação nesta conversa, se a pergunta ainda vale. A pergunta vale para um pedido
    só, e só na conversa em que foi feita: um "sim" dito em outro aparelho não desliga nada.
    """
    with _trava_pendente:
        pendente = _pendentes.pop(conversa, None) or {}
    return pendente.get("acao") if pendente and time.monotonic() <= pendente["ate"] else None


def palavras(texto: str) -> list[str]:
    sem_acento = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z]+", sem_acento)


def resposta_de_confirmacao(texto: str) -> bool | None:
    """True para um "sim", False para um "não" e None quando a frase é outro pedido."""
    lista = palavras(texto)
    if not lista:
        return None
    if lista[0] in PALAVRAS_SIM and all(palavra in PALAVRAS_DA_CONFIRMACAO for palavra in lista):
        return True
    if lista[0] in PALAVRAS_NAO and all(palavra in PALAVRAS_DA_RECUSA for palavra in lista):
        return False
    return None


def energia_do_pc(args: dict, pc: dict) -> dict:
    acao = args.get("acao")
    if acao not in ("bloquear", "desligar", "cancelar"):
        return {"erro": "a ação precisa ser bloquear, desligar ou cancelar"}
    if acao != "desligar":
        return pedir_ao_pc("/energia", {"acao": acao}, timeout=10)
    if pc["fora_do_ar"]:
        return {"erro": "o computador já está desligado ou fora da rede"}
    if "desligar" not in pc["acoes"]:
        return {"erro": "desligar o PC não está liberado no config_agente.json do PC"}
    # Quem desliga é o próximo pedido, se for um "sim". A pergunta é guardada em conversar, que a devolve
    # sem passar pelo LLM: um "sim" só pode valer para uma pergunta que a pessoa ouviu do jeito que está aqui.
    return {"confirmar": True, "pergunta": PERGUNTA_DESLIGAR}


def cumprir_pendente(acao: str, confirmou: bool) -> tuple[str, list[dict]]:
    if not confirmou:
        return "Tudo bem, não vou desligar o PC.", []
    resultado = pedir_ao_pc("/energia", {"acao": acao}, timeout=10)
    acoes = [{"ferramenta": "energia_do_pc", "argumentos": {"acao": acao}, "resultado": resultado}]
    if resultado.get("ok") and resultado.get("descricao"):
        return f"Tudo bem, {resultado['descricao']}.", acoes
    return f"Não consegui desligar o PC: {resultado.get('erro') or 'o computador não respondeu'}.", acoes


# ---------- Wake-on-LAN ----------

ESPERA_WOL = 90  # segundos até o agente responder; com o boot completo, 60 pode ser pouco
INTERVALO_WOL = 3
REENVIO_WOL = 20


def pacote_magico(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(re.sub(r"[:-]", "", mac)) * 16


def enviar_wol() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):  # UDP não confirma a entrega
            sock.sendto(pacote_magico(PC_MAC), (PC_BROADCAST, 9))


def ligar_pc() -> dict:
    """Manda o pacote mágico e espera o agente responder."""
    if not PC_MAC:
        return {"erro": 'ligar o PC pela rede não está configurado: preencha "pc_mac" no config_servidor.json'}
    if not consultar_pc(avisar=False)["fora_do_ar"]:
        return {"ok": True, "descricao": "o PC já estava ligado"}
    fim = time.monotonic() + ESPERA_WOL
    proximo_envio = 0.0
    while time.monotonic() < fim:
        if time.monotonic() >= proximo_envio:
            try:
                enviar_wol()
            except OSError as e:
                print(f"[servidor] não consegui mandar o Wake-on-LAN (detalhe técnico: {e})", file=sys.stderr)
                return {"erro": "não consegui mandar o sinal para ligar o PC"}
            proximo_envio = time.monotonic() + REENVIO_WOL
        time.sleep(INTERVALO_WOL)
        if not consultar_pc(avisar=False)["fora_do_ar"]:
            return {"ok": True, "descricao": "liguei o PC"}
    return {"erro": f"mandei o sinal para ligar o PC, mas o agente não respondeu em {ESPERA_WOL} segundos: se o "
                    "PC ligou, faça login nele e abra o agente"}


# ---------- Previsão do tempo e Spotify ----------

def previsao_do_tempo(args: dict) -> dict:
    cidade = args.get("cidade")
    cidade = cidade.strip() if isinstance(cidade, str) else ""
    try:  # o tempo.py confere dias_a_frente, que vem do LLM ("", 2.0, "2"...)
        return {"ok": True, **TEMPO.previsao(cidade or None, args.get("dias_a_frente"))}
    except tempo.ErroNoTempo as e:
        if e.detalhe:
            print(f"[servidor] falha na previsão do tempo: {e} (detalhe técnico: {e.detalhe})", file=sys.stderr)
        return {"erro": str(e)}


ESPERA_SPOTIFY = 15  # segundos para o aplicativo abrir e aparecer como dispositivo
INTERVALO_SPOTIFY = 2


def erro_do_spotify(e: "spotify.ErroNoSpotify") -> dict:
    if e.detalhe:
        print(f"[servidor] falha no Spotify: {e} (detalhe técnico: {e.detalhe})", file=sys.stderr)
    return {"erro": str(e)}


def tocar_musica(args: dict, pc: dict) -> dict:
    if SPOTIFY is None:
        return {"erro": "o Spotify não está configurado no servidor"}
    busca = args.get("busca")
    busca = busca.strip() if isinstance(busca, str) else ""
    tipo = args.get("tipo") or "musica"
    if not busca:
        return {"erro": "diga o que tocar"}
    if not isinstance(tipo, str) or tipo not in spotify.TIPOS:
        return {"erro": f"o tipo precisa ser {', '.join(spotify.TIPOS)}"}
    try:
        return {"ok": True, "descricao": SPOTIFY.tocar(busca, tipo)}
    except spotify.ErroNoSpotify as e:
        # O Spotify fechado não aparece como dispositivo: se o agente sabe abrir, abre e tenta de novo.
        if e.codigo != "sem_dispositivo" or "spotify" not in pc["programas"]:
            return erro_do_spotify(e)
    if not pedir_ao_pc("/abrir", {"programa": "spotify"}).get("ok"):
        return {"erro": "o Spotify não está aberto no PC e não consegui abrir"}
    fim = time.monotonic() + ESPERA_SPOTIFY
    while True:
        time.sleep(INTERVALO_SPOTIFY)
        try:
            return {"ok": True, "descricao": f"abri o Spotify e {SPOTIFY.tocar(busca, tipo)}"}
        except spotify.ErroNoSpotify as e:
            if e.codigo != "sem_dispositivo":
                return erro_do_spotify(e)
            if time.monotonic() >= fim:  # o próprio servidor abriu o aplicativo: não adianta mandar abrir
                return {"erro": "abri o Spotify no PC, mas ele ainda não apareceu para tocar; peça de novo em "
                                "alguns segundos"}


def controlar_musica(args: dict) -> dict:
    if SPOTIFY is None:
        return {"erro": "o Spotify não está configurado no servidor"}
    acao = args.get("acao")
    if acao not in spotify.ACOES:
        return {"erro": f"a ação precisa ser {', '.join(spotify.ACOES)}"}
    try:
        return {"ok": True, "descricao": SPOTIFY.controlar(acao)}
    except spotify.ErroNoSpotify as e:
        return erro_do_spotify(e)


def controlar_lampada(args: dict) -> dict:
    """Confere os argumentos que vieram do LLM e manda o pedido para a lâmpada."""
    if LAMPADA is None:
        return {"erro": "não há lâmpada configurada no servidor"}
    ligar, brilho, cor = args.get("ligar"), args.get("brilho"), args.get("cor")
    if ligar is not None and not isinstance(ligar, bool):
        return {"erro": "o campo ligar precisa ser verdadeiro ou falso"}
    if brilho is not None:
        try:
            brilho = int(brilho) if not isinstance(brilho, bool) else None
        except (TypeError, ValueError, OverflowError):  # OverflowError: o JSON aceita 1e999 e Infinity
            brilho = None
        if brilho is None:
            return {"erro": "o brilho precisa ser um número de 1 a 100"}
        brilho = max(1, min(100, brilho))
    if cor is not None:
        cor = " ".join(str(cor).lower().split())
        if cor not in lampada.NOMES_DAS_CORES:
            return {"erro": f"não conheço a cor {cor}; posso usar: {', '.join(lampada.NOMES_DAS_CORES)}"}
    if ligar is None and brilho is None and cor is None:
        return {"erro": "diga o que fazer com a lâmpada: ligar, desligar, mudar o brilho ou a cor"}
    try:
        return {"ok": True, "descricao": LAMPADA.controlar(ligar=ligar, brilho=brilho, cor=cor)}
    except lampada.ErroNaLampada as e:
        print(f"[servidor] falha na lâmpada: {e} (detalhe técnico: {e.detalhe})", file=sys.stderr)
        return {"erro": str(e)}


# ---------- LLM ----------

def limpar(texto: str) -> str:
    """Remove o raciocínio <think>...</think> que alguns modelos Qwen devolvem."""
    # Um <think> sem fechamento (resposta cortada) vai até o fim do texto.
    texto = re.sub(r"<think>.*?(?:</think>|$)", "", texto if isinstance(texto, str) else "", flags=re.DOTALL)
    # Quando o template do modelo já abre o <think>, só o fechamento aparece: descarta até ele.
    texto = re.sub(r"^.*?</think>", "", texto, flags=re.DOTALL)
    return texto.strip()


def resumir(acoes: list[dict]) -> str:
    """Resposta de reserva quando o modelo não fecha com um texto: conta o que foi feito."""
    resultados = [(a["ferramenta"], a["resultado"]) for a in acoes if isinstance(a["resultado"], dict)]
    abertos = [r["programa"] for nome, r in resultados if nome == "abrir_programa" and r.get("ok") and r.get("programa")]
    feitos = [f"abri {' e '.join(abertos)}"] if abertos else []
    feitos += [r["descricao"] for _, r in resultados if r.get("ok") and r.get("descricao")]
    pergunta = next((r["pergunta"] for _, r in resultados if r.get("confirmar") and r.get("pergunta")), None)
    if feitos and pergunta:
        return f"Pronto, {', '.join(feitos)}. {pergunta}"
    if feitos or pergunta:
        return f"Pronto, {', '.join(feitos)}." if feitos else pergunta
    return "Fiz o que consegui, mas algo não saiu como esperado."


def conversar(texto_usuario: str, conversa: str = CONVERSA_PADRAO) -> tuple[str, list[dict]]:
    historico = lembrar(conversa)
    pendente = tirar_pendente(conversa)
    if pendente:  # a resposta à pergunta "quer mesmo desligar?" não passa pelo LLM
        confirmou = resposta_de_confirmacao(texto_usuario)
        if confirmou is not None:
            resposta, acoes = cumprir_pendente(pendente, confirmou)
            guardar_pedido(conversa, [{"role": "user", "content": texto_usuario},
                                      {"role": "assistant", "content": resposta}])
            return resposta, acoes

    pc = consultar_pc()
    ferramentas = montar_ferramentas(pc)
    sistema = f"{PROMPT_SISTEMA} {descrever_momento(agora())}"
    if not pc["programas"]:
        sistema += " O computador está desligado ou inacessível agora; avise se pedirem algo nele."
        if pc["fora_do_ar"] and PC_MAC:
            sistema += " Se pedirem algo no computador, ligue-o antes com a ferramenta ligar_pc."
    if historico and PERGUNTA_DESLIGAR in (historico[-1].get("content") or ""):
        # A pergunta venceu ou a resposta não foi um "sim" curto: sem este aviso, o LLM lê a resposta como
        # confirmação e diz que vai desligar, sem desligar nada.
        sistema += (" A pergunta de desligar o PC que aparece na conversa não vale mais: se o usuário quiser "
                    "desligar, chame energia_do_pc de novo. Nunca diga que o PC vai desligar sem chamar a ferramenta.")

    mensagens = [{"role": "system", "content": sistema}, *historico, {"role": "user", "content": texto_usuario}]
    inicio = len(mensagens) - 1  # daqui em diante é este pedido, que vai para a memória
    acoes = []
    executadas = {}  # (ferramenta, argumentos) -> resultado

    def terminar(resposta: str) -> tuple[str, list[dict]]:
        guardar_pedido(conversa, mensagens[inicio:] + [{"role": "assistant", "content": resposta}])
        return resposta, acoes

    for rodada in range(1, 4):  # no máximo 3 rodadas por comando
        ultima = rodada == 3
        payload = {"model": CFG["llm_model"], "messages": mensagens, "temperature": 0.3}
        if ferramentas:
            # Na última rodada o modelo só pode responder em texto: uma ferramenta pedida ali
            # seria executada sem que o resultado voltasse para ele.
            payload.update(tools=ferramentas, tool_choice="none" if ultima else "auto")

        try:
            r = requests.post(f"{LLM_URL}/chat/completions", headers=LLM_HEADERS,
                              json=payload, timeout=60)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            if not isinstance(msg, dict):
                raise TypeError(f"message não é um objeto: {msg!r}")
        except (requests.RequestException, KeyError, IndexError, TypeError) as e:
            if not acoes:
                raise
            # Algo já foi feito (um programa pode ter aberto): contar o que aconteceu é melhor do que
            # mandar tentar de novo e abrir outra vez. Inclui o 400 do Groq quando o modelo insiste na
            # ferramenta mesmo com tool_choice="none".
            detalhe = e.response.text[:500] if getattr(e, "response", None) is not None else repr(e)
            print(f"[servidor] a API do LLM falhou depois das ações (detalhe técnico: {detalhe})", file=sys.stderr)
            return terminar(resumir(acoes))
        chamadas = chamadas_validas(msg)

        if not chamadas:
            return terminar(limpar(msg.get("content")))
        if ultima:
            # Backend que ignora tool_choice="none" (o Ollama, por exemplo): não executa nada, e o texto
            # que acompanha a chamada ("Pronto, abri o navegador.") não vale, porque nada foi aberto.
            return terminar(resumir(acoes))

        mensagens.append({"role": "assistant", "content": msg.get("content") or "",
                          "tool_calls": chamadas})
        for chamada in chamadas:
            nome = chamada["function"]["name"]
            args = ler_argumentos(chamada["function"].get("arguments"))
            chave = (nome, json.dumps(args, sort_keys=True))
            if chave not in executadas:  # modelos pequenos repetem a mesma chamada
                executadas[chave] = executar_ferramenta(nome, args, pc)
                acoes.append({"ferramenta": nome, "argumentos": args, "resultado": executadas[chave]})
                if nome == "ligar_pc" and executadas[chave].get("ok"):  # o PC ligou: agora dá para usá-lo
                    pc = consultar_pc()
                    ferramentas = montar_ferramentas(pc)
            mensagens.append({"role": "tool", "tool_call_id": chamada["id"],
                              "content": json.dumps(executadas[chave], ensure_ascii=False)})
        if any(isinstance(a["resultado"], dict) and a["resultado"].get("confirmar") for a in acoes):
            guardar_pendente("desligar", conversa)  # o prazo conta a partir de agora, quando a pergunta sai
            return terminar(resumir(acoes))


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


def gerar_audio(texto: str) -> str | None:
    """A resposta falada pela voz do servidor, em WAV codificado em base64; None sem voz ou se falhar."""
    if VOZ is None:
        return None
    try:
        wav = VOZ.sintetizar(texto)
    except Exception as e:  # a voz é um extra: sem ela, a página fala com a voz do navegador
        print(f"[servidor] a voz do servidor falhou; a página usa a do navegador (detalhe técnico: {e!r})",
              file=sys.stderr)
        return None
    return base64.b64encode(wav).decode("ascii") if wav else None


def responder(frase: str, conversa: str):
    try:
        resposta, acoes = conversar(frase, conversa)
    except requests.RequestException as e:
        return jsonify(erro=explicar_erro(e, "a API do LLM")), 502
    dados = {"transcricao": frase, "resposta": resposta, "acoes": acoes}
    audio = gerar_audio(resposta)
    if audio:  # só com a voz do servidor: quem não sabe tocar o áudio segue usando o texto
        dados["audio"] = audio
    return jsonify(dados)


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
    return responder(texto, ler_conversa(request.headers.get("X-Conversa")))


@app.post("/texto")
def texto():
    dados = request.get_json(silent=True)
    frase = dados.get("texto") if isinstance(dados, dict) else None
    frase = frase.strip() if isinstance(frase, str) else ""
    if not frase:
        return jsonify(erro="Digite um comando."), 400
    conversa = dados.get("conversa") if isinstance(dados, dict) else None
    return responder(frase, ler_conversa(conversa or request.headers.get("X-Conversa")))


def abrir_socket(host: str, porta: int) -> socket.socket:
    """
    Abre o socket antes de entregá-lo ao Werkzeug, para explicar em português por que não abriu
    (o Werkzeug só imprime um aviso em inglês e encerra). Sem SO_REUSEADDR no Windows: lá a opção
    deixa um segundo servidor escutar na mesma porta sem erro, enquanto o antigo continua atendendo.
    """
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, porta))
        sock.listen(128)
    except OSError as e:
        sock.close()
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", None)):
            dica = 'Outro servidor já usa essa porta: feche-o ou troque "porta" no config_servidor.json.'
        elif isinstance(e, socket.gaierror) or e.errno in (errno.EADDRNOTAVAIL,
                                                             getattr(errno, "WSAEADDRNOTAVAIL", None)):
            dica = f'"{host}" não é um endereço deste aparelho: deixe "host" como "127.0.0.1" no config_servidor.json.'
        elif isinstance(e, PermissionError) and porta < 1024:
            dica = 'O sistema só deixa usar portas acima de 1024: troque "porta" no config_servidor.json, por exemplo para 8000.'
        elif isinstance(e, PermissionError):  # no Windows, o 10013 vem das portas reservadas pelo sistema
            dica = ('O sistema não deixou usar essa porta (no Windows, algumas ficam reservadas pelo Hyper-V ou pelo WSL): '
                    'troque "porta" no config_servidor.json, por exemplo para 8001.')
        else:
            dica = 'Confira "host" e "porta" no config_servidor.json.'
        sys.exit(f"Não consegui escutar em {host}:{porta} (detalhe técnico: {e}).\n{dica}")
    return sock


if __name__ == "__main__":
    # make_server no lugar de app.run: mesmo servidor do Flask, sem o aviso e o log em inglês.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    sock = abrir_socket(HOST, PORTA)
    servidor = make_server(HOST, PORTA, app, threaded=True, fd=sock.fileno())
    print(f"[servidor] pronto: abra http://localhost:{sock.getsockname()[1]} no navegador deste aparelho.")
    print(aviso_da_lampada())
    print(AVISO_DA_VOZ)
    if aviso_do_spotify():
        print(aviso_do_spotify())
    print("[servidor] para parar, aperte Ctrl+C.", flush=True)
    servidor.serve_forever()  # o Werkzeug já trata o Ctrl+C
    print("[servidor] encerrado.")
