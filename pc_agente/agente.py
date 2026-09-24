"""
Agente do PC: fica escutando na rede local e abre programas a pedido do celular.

- Só abre programas cadastrados em config_agente.json (nunca executa comandos livres).
- Exige o token em cada pedido (cabeçalho "Authorization: Bearer <token>").
- Usa só a biblioteca padrão do Python: não precisa instalar nada.

Rodar:  python agente.py
"""
import hmac
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).parent


def carregar_config(caminho: Path) -> dict:
    """Lê o JSON de configuração ou encerra explicando o que fazer."""
    exemplo = caminho.with_name(f"{caminho.stem}.example.json")
    try:
        texto = caminho.read_text(encoding="utf-8-sig")  # -sig aceita o BOM do Bloco de Notas
    except FileNotFoundError:
        sys.exit(
            f"Arquivo de configuração não encontrado: {caminho}\n"
            f"Copie {exemplo.name} para {caminho.name}, na mesma pasta, e preencha os seus dados."
        )
    try:
        return json.loads(texto)
    except json.JSONDecodeError as e:
        sys.exit(
            f"Erro de JSON em {caminho}, linha {e.lineno}, coluna {e.colno}: {e.msg}\n"
            "Confira vírgulas e aspas; em caminhos do Windows use barras duplas (C:\\\\Pasta\\\\programa.exe)."
        )


CONFIG = carregar_config(BASE / "config_agente.json")

TOKEN = CONFIG["token"]
PORTA = int(CONFIG.get("porta", 8765))
PROGRAMAS = CONFIG["programas"]  # nome -> comando (lista de strings)

if TOKEN == "TROQUE-ESTE-TOKEN":
    sys.exit("Defina um token próprio em config_agente.json antes de rodar.")


def abrir_programa(nome: str) -> None:
    comando = PROGRAMAS[nome]
    opcoes = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        opcoes["start_new_session"] = True  # o programa não fecha junto com o agente
    subprocess.Popen(comando, **opcoes)  # shell=False: nada de comandos livres


class Handler(BaseHTTPRequestHandler):
    def _responder(self, status: int, dados: dict) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _autorizado(self) -> bool:
        recebido = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(recebido, TOKEN)

    def do_GET(self):
        if not self._autorizado():
            return self._responder(401, {"erro": "token inválido"})
        if self.path == "/programas":
            return self._responder(200, {"programas": sorted(PROGRAMAS)})
        self._responder(404, {"erro": "rota não encontrada"})

    def do_POST(self):
        if not self._autorizado():
            return self._responder(401, {"erro": "token inválido"})
        if self.path != "/abrir":
            return self._responder(404, {"erro": "rota não encontrada"})

        try:
            tamanho = int(self.headers.get("Content-Length", 0))
            pedido = json.loads(self.rfile.read(tamanho) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._responder(400, {"erro": "JSON inválido"})

        nome = str(pedido.get("programa", "")).lower().strip()
        if nome not in PROGRAMAS:
            return self._responder(404, {"erro": f"programa '{nome}' não está na lista"})

        try:
            abrir_programa(nome)
        except OSError as e:
            return self._responder(500, {"erro": f"falha ao abrir '{nome}': {e}"})

        print(f"[agente] abriu: {nome}")
        self._responder(200, {"ok": True, "programa": nome})

    def log_message(self, formato, *args):
        pass  # silencia o log padrão; usamos nossos próprios prints


if __name__ == "__main__":
    print(f"[agente] escutando na porta {PORTA}")
    print(f"[agente] programas liberados: {', '.join(sorted(PROGRAMAS))}")
    ThreadingHTTPServer(("0.0.0.0", PORTA), Handler).serve_forever()
