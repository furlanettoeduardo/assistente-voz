"""
Peças compartilhadas pelos testes. Nada aqui chama APIs reais: o LLM é um servidor HTTP falso
em 127.0.0.1 e o agente roda numa porta livre escolhida pelo sistema.
"""
import fnmatch
import importlib.util
import itertools
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
PASTA_AGENTE = RAIZ / "pc_agente"
PASTA_SERVIDOR = RAIZ / "celular_servidor"

POPEN_REAL = subprocess.Popen  # guardado antes de qualquer mock.patch
_contador = itertools.count()


def _fora_da_copia(pasta: str, nomes: list[str]) -> list[str]:
    """Caches e configs reais, inclusive cópias como "config_agente (1).json" (os mesmos do .gitignore)."""
    exemplos = {"config_agente.example.json", "config_servidor.example.json"}
    return [nome for nome in nomes if nome == "__pycache__" or (
        nome not in exemplos and (fnmatch.fnmatch(nome, "config_agente*.json*")
                                  or fnmatch.fnmatch(nome, "config_servidor*.json*")))]


def copiar_componente(pasta: Path, destino: Path) -> Path:
    """Copia a pasta do componente sem os configs reais do usuário nem caches."""
    shutil.copytree(pasta, destino, ignore=_fora_da_copia)
    return destino


def carregar_componente(pasta: Path, modulo: str, nome_config: str, config: dict, destino: Path):
    """
    Copia o componente para `destino`, grava o config de teste e importa o módulo da cópia.
    Assim o código lê config_*.json do jeito normal, sem tocar nos arquivos reais do projeto.
    """
    copiar_componente(pasta, destino)
    (destino / nome_config).write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return importar_copia(destino, modulo)


def importar_copia(pasta: Path, modulo: str):
    """Importa `pasta/modulo.py` com um nome único, para cada teste ter a sua própria cópia."""
    nome = f"{modulo}_teste_{next(_contador)}"
    spec = importlib.util.spec_from_file_location(nome, pasta / f"{modulo}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod  # o Flask procura o módulo aqui para achar a pasta static
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules[nome]
        raise
    return mod


class ServidorLocal:
    """Sobe um ThreadingHTTPServer em 127.0.0.1, numa porta livre, numa thread em segundo plano."""

    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def parar(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


def iniciar_agente(destino: Path, token: str, programas: dict):
    """Carrega uma cópia do agente com o config dado e o põe para escutar em 127.0.0.1."""
    agente = carregar_componente(PASTA_AGENTE, "agente", "config_agente.json",
                                 {"token": token, "porta": 0, "programas": programas}, destino)
    return agente, ServidorLocal(agente.Handler)


def comando_de_teste(marcador: Path) -> list[str]:
    """Programa inofensivo: o próprio Python cria o arquivo `marcador` e termina."""
    codigo = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('aberto', encoding='utf-8')"
    return [sys.executable, "-c", codigo, str(marcador)]


def esperar_arquivo(caminho: Path, segundos: float = 15) -> bool:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if caminho.exists():
            return True
        time.sleep(0.05)
    return False


def vigiar_popen(teste, modulo):
    """
    Troca subprocess.Popen por um mock que ainda abre o processo de verdade, para o teste
    conferir os argumentos. No fim do teste espera os processos terminarem.
    """
    processos = []

    def abrir(*args, **kwargs):
        processo = POPEN_REAL(*args, **kwargs)
        processos.append(processo)
        return processo

    patcher = mock.patch.object(modulo.subprocess, "Popen", side_effect=abrir)
    popen = patcher.start()
    teste.addCleanup(lambda: [p.wait(timeout=15) for p in processos])
    teste.addCleanup(patcher.stop)
    return popen


# ---------- LLM falso (API compatível com OpenAI) ----------

def resposta_texto(conteudo) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": conteudo}}]}


def resposta_ferramenta(id_chamada: str, nome: str, argumentos: dict, conteudo=None) -> dict:
    return {"choices": [{"message": {
        "role": "assistant",
        "content": conteudo,
        "tool_calls": [{
            "id": id_chamada,
            "type": "function",
            "function": {"name": nome, "arguments": json.dumps(argumentos, ensure_ascii=False)},
        }],
    }}]}


class LLMFalso:
    """
    Imita /chat/completions, /audio/transcriptions e GET /models. Cada pedido de chat consome a
    próxima resposta de `respostas` (tupla status, corpo) e fica guardado em `pedidos` para conferência.
    """

    def __init__(self):
        self._trava = threading.Lock()
        self.zerar()
        self._servidor = ServidorLocal(self._criar_handler())
        self.url = self._servidor.url

    def zerar(self) -> None:
        """Volta ao estado inicial entre um teste e outro."""
        with self._trava:
            self.respostas: list[tuple[int, dict]] = []
            self.pedidos: list[dict] = []
            self.transcricao = ""
            self.status_transcricao = 200
            self.modelos = ["whisper-falso", "qwen-falso"]
            self.status_modelos = 200

    def programar(self, *corpos, status: int = 200) -> None:
        self.programar_com_status(*[(status, corpo) for corpo in corpos])

    def programar_com_status(self, *respostas: tuple[int, dict]) -> None:
        with self._trava:
            self.respostas = list(respostas)
            self.pedidos = []

    def pedidos_de_chat(self) -> list[dict]:
        return [p for p in self.pedidos if p["caminho"] == "/chat/completions"]

    def parar(self) -> None:
        self._servidor.parar()

    def _criar_handler(self):
        falso = self

        class Handler(BaseHTTPRequestHandler):
            def _responder(self, status: int, dados: dict) -> None:
                corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def do_GET(self):
                with falso._trava:
                    falso.pedidos.append({"caminho": self.path, "cabecalhos": dict(self.headers), "corpo": b""})
                    if self.path != "/models":
                        return self._responder(404, {"error": {"message": "rota não encontrada"}})
                    if falso.status_modelos != 200:
                        return self._responder(falso.status_modelos, {"error": {"message": "Invalid API Key"}})
                    self._responder(200, {"object": "list", "data": [{"id": m} for m in falso.modelos]})

            def do_POST(self):
                corpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                pedido = {"caminho": self.path, "cabecalhos": dict(self.headers), "corpo": corpo}
                with falso._trava:
                    falso.pedidos.append(pedido)
                    if self.path == "/audio/transcriptions":
                        if falso.status_transcricao != 200:
                            return self._responder(falso.status_transcricao,
                                                   {"error": {"message": "erro falso de transcrição"}})
                        return self._responder(200, {"text": falso.transcricao})
                    if self.path != "/chat/completions":
                        return self._responder(404, {"error": "rota não encontrada"})
                    pedido["json"] = json.loads(corpo)
                    if not falso.respostas:
                        return self._responder(500, {"error": "LLM falso sem resposta programada"})
                    status, dados = falso.respostas.pop(0)
                self._responder(status, dados)

            def log_message(self, formato, *args):
                pass

        return Handler
