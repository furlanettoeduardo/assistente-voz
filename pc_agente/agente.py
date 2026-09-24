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
ARQUIVO_CONFIG = BASE / "config_agente.json"
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
            "Confira vírgulas e aspas; em caminhos do Windows use barras duplas (C:\\\\Pasta\\\\programa.exe)."
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
    """A porta pode vir como número ou texto ("8765"), mas precisa estar entre 0 e 65535."""
    try:
        porta = int(config.get("porta", padrao))
    except (TypeError, ValueError):
        porta = -1
    if not 0 <= porta <= 65535:
        sys.exit(f'Em {caminho}, "porta" precisa ser um número entre 0 e 65535, como {padrao}.')
    return porta


def normalizar(nome) -> str:
    """Compara nomes sem diferenciar maiúsculas nem espaços extras ("VS  Code " == "vs code")."""
    return " ".join(str(nome).split()).lower()


CONFIG = carregar_config(ARQUIVO_CONFIG, {"token": None, "programas": dict})

TOKEN = str(CONFIG["token"] or "").strip()
PORTA = ler_porta(CONFIG, ARQUIVO_CONFIG, 8765)
PROGRAMAS = {normalizar(nome): comando  # nome -> comando (lista de strings)
             for nome, comando in CONFIG["programas"].items()}

if not PROGRAMAS:
    sys.exit(f'Cadastre pelo menos um programa em "programas" no {ARQUIVO_CONFIG}, como no exemplo.')
for nome, comando in PROGRAMAS.items():
    if not isinstance(comando, list) or not comando or not all(isinstance(parte, str) for parte in comando):
        sys.exit(f'Em {ARQUIVO_CONFIG}, o comando de "{nome}" precisa ser uma lista de textos, como ["notepad.exe"].')

# Token vazio deixaria qualquer um na rede abrir programas; com acento, o celular não consegue enviá-lo.
if not TOKEN or TOKEN == "TROQUE-ESTE-TOKEN" or not TOKEN.isascii():
    sys.exit(
        "Defina um token próprio em config_agente.json antes de rodar, sem acentos.\n"
        'Para gerar um: python -c "import secrets; print(secrets.token_urlsafe(24))"'
    )


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
    timeout = 10  # segundos; uma conexão parada não prende a thread para sempre
    LIMITE_CORPO = 64 * 1024  # os pedidos do celular têm poucos bytes

    def _ler_corpo(self) -> bytes | None:
        """
        Lê o corpo antes de qualquer resposta. Se o agente responde sem ler, o Windows
        derruba a conexão e o cliente recebe um erro de rede em vez do 401 ou 404.
        """
        try:
            tamanho = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if not 0 <= tamanho <= self.LIMITE_CORPO:
            return None
        return self.rfile.read(tamanho)

    def _responder(self, status: int, dados: dict) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _autorizado(self) -> bool:
        recebido = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        # Em bytes: com str, compare_digest levanta TypeError se o cabeçalho tiver acento.
        if hmac.compare_digest(recebido.encode("utf-8"), TOKEN.encode("utf-8")):
            return True
        print(f"[agente] pedido recusado de {self.client_address[0]}: token inválido")
        return False

    def do_GET(self):
        if not self._autorizado():
            return self._responder(401, {"erro": "token inválido"})
        if self.path == "/programas":
            return self._responder(200, {"programas": sorted(PROGRAMAS)})
        self._responder(404, {"erro": "rota não encontrada"})

    def do_POST(self):
        corpo = self._ler_corpo()
        if not self._autorizado():
            return self._responder(401, {"erro": "token inválido"})
        if self.path != "/abrir":
            return self._responder(404, {"erro": "rota não encontrada"})

        try:
            pedido = json.loads(corpo)
        except (TypeError, ValueError):  # tamanho inválido (corpo None), corpo vazio ou JSON quebrado
            pedido = None
        if not isinstance(pedido, dict):
            return self._responder(400, {"erro": "JSON inválido"})

        nome = normalizar(pedido.get("programa", ""))
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


class Servidor(ThreadingHTTPServer):
    # No Windows, SO_REUSEADDR deixa um segundo agente escutar na mesma porta sem erro,
    # enquanto o antigo continua atendendo com o token e a lista antigos.
    allow_reuse_address = sys.platform != "win32"


if __name__ == "__main__":
    try:
        servidor = Servidor(("0.0.0.0", PORTA), Handler)
    except OSError as e:
        sys.exit(f"Não consegui escutar na porta {PORTA}: {e}\n"
                 "Feche o outro agente (ou o programa que usa essa porta) e rode de novo.")
    print(f"[agente] escutando na porta {PORTA}")
    print(f"[agente] programas liberados: {', '.join(sorted(PROGRAMAS))}")
    servidor.serve_forever()
