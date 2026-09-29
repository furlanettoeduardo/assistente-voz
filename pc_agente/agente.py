"""
Agente do PC: fica escutando na rede local e, a pedido do servidor, abre e fecha programas,
mexe no volume, bloqueia a tela e desliga o PC.

- Só abre e fecha programas cadastrados em config_agente.json (nunca executa comandos livres).
- Volume, bloquear e desligar só funcionam se estiverem liberados em "acoes" (desligar vem desligado).
- Exige o token em cada pedido (cabeçalho "Authorization: Bearer <token>").
- Usa só a biblioteca padrão do Python: não precisa instalar nada.

Rodar:  python agente.py
"""
import errno
import hmac
import json
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sistema

BASE = Path(__file__).parent
ARQUIVO_CONFIG = BASE / "config_agente.json"
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


ACOES_CONHECIDAS = ("volume", "bloquear", "desligar")
ACOES_PADRAO = ["volume", "bloquear"]  # desligar só quando o dono libera
PROIBIDOS_NO_PROCESSO = ("/", "\\", "*", "?")  # um "*" no taskkill poderia fechar tudo
PARTE_DO_WINDOWS = "é parte do próprio Windows, e não um programa para fechar"
# Processos que "fechar" recusa, em minúsculas -> (nome como no Gerenciador de Tarefas, motivo).
PROCESSOS_PROIBIDOS = {
    "applicationframehost.exe": ("ApplicationFrameHost.exe", "é a moldura de todos os aplicativos da Microsoft "
                                 "Store, e fechá-lo fecharia todos. Use o processo do próprio aplicativo, como "
                                 "CalculatorApp.exe"),
    # O pedido de fechar que chega à área de trabalho abre a caixa de desligar, sem passar por "acoes".
    "explorer.exe": ("explorer.exe", "é a barra de tarefas, a área de trabalho e o Explorador de Arquivos do "
                     'Windows, e pedir para fechá-lo abre a caixa "Desligar o Windows"'),
    **{processo: (processo, PARTE_DO_WINDOWS)
       for processo in ("dwm.exe", "csrss.exe", "winlogon.exe", "svchost.exe", "lsass.exe", "services.exe",
                        "smss.exe", "wininit.exe")},
}


def ler_fechar(config: dict, caminho: Path, plataforma: str = sys.platform) -> dict[str, list[str]]:
    """
    "fechar" (opcional): nome falado -> nome do processo, como texto ou lista de textos.
    Devolve os nomes normalizados, cada um com a sua lista de processos.
    """
    fechar = config.get("fechar", {})
    if not isinstance(fechar, dict):
        sys.exit(f'Em {caminho}, "fechar" precisa ser {TIPOS[dict]}, como no exemplo.')
    resultado = {}
    for nome, processos in fechar.items():
        nome = normalizar(nome)
        if isinstance(processos, str):
            processos = [processos]
        if not isinstance(processos, list) or not processos or not all(isinstance(p, str) for p in processos):
            sys.exit(f'Em {caminho}, o processo de "{nome}" em "fechar" precisa ser um texto, como "chrome.exe", '
                     'ou uma lista de textos.')
        processos = [processo.strip() for processo in processos]
        for processo in processos:
            if processo.lower() in PROCESSOS_PROIBIDOS:
                exibido, motivo = PROCESSOS_PROIBIDOS[processo.lower()]
                sys.exit(f'Em {caminho}, "{nome}" em "fechar" não pode usar o {exibido}: ele {motivo}.')
            if not processo or processo.startswith("-") or any(c in processo for c in PROIBIDOS_NO_PROCESSO):
                sys.exit(f'Em {caminho}, "{processo}" (em "fechar", no "{nome}") precisa ser só o nome do processo, '
                         'como "chrome.exe": sem pastas, sem *, sem ? e sem - no começo.')
            # Sem o .exe no fim ("WhatsApp.Root"), o taskkill não acha o processo e o agente responderia que o
            # programa não está aberto.
            if plataforma == "win32" and not processo.lower().endswith(".exe"):
                sys.exit(f'Em {caminho}, "{processo}" (em "fechar", no "{nome}") precisa ter o .exe no fim, como '
                         '"chrome.exe". O nome certo aparece no Gerenciador de Tarefas, na aba Detalhes.')
        resultado[nome] = processos
    return resultado


def ler_acoes(config: dict, caminho: Path) -> frozenset[str]:
    """Lê "acoes" (opcional): o que o servidor pode pedir além de abrir e fechar programas."""
    acoes = config.get("acoes", ACOES_PADRAO)
    conhecidas = '"volume", "bloquear" e "desligar"'
    if not isinstance(acoes, list) or not all(isinstance(acao, str) for acao in acoes):
        sys.exit(f'Em {caminho}, "acoes" precisa ser uma lista de textos, como ["volume", "bloquear"]. '
                 f"As ações que existem são {conhecidas}.")
    for acao in acoes:
        if normalizar(acao) not in ACOES_CONHECIDAS:
            sys.exit(f'Em {caminho}, "acoes" tem uma ação que não existe: "{acao}". '
                     f"As ações que existem são {conhecidas}.")
    return frozenset(normalizar(acao) for acao in acoes)


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

FECHAR = ler_fechar(CONFIG, ARQUIVO_CONFIG)  # nome -> lista de nomes de processo
ACOES = ler_acoes(CONFIG, ARQUIVO_CONFIG)


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


def explicar_falha(nome: str, erro: OSError) -> str:
    """Traduz a falha do sistema ao abrir um programa (no Linux a mensagem original vem em inglês)."""
    executavel = PROGRAMAS[nome][0]
    if isinstance(erro, FileNotFoundError):
        return f"não encontrei '{executavel}' no PC; confira o comando de '{nome}' no config_agente.json"
    if isinstance(erro, PermissionError):
        return f"o PC não deu permissão para executar '{executavel}'"
    return f"o PC não conseguiu abrir '{nome}'"


def resumo_da_config() -> list[str]:
    """Linhas que o agente mostra ao iniciar, dizendo o que ele pode fazer."""
    linhas = [f"[agente] programas liberados: {', '.join(sorted(PROGRAMAS))}"]
    if FECHAR:
        linhas.append(f"[agente] fecha: {', '.join(sorted(FECHAR))}")
    if ACOES:
        linhas.append(f"[agente] ações liberadas: {', '.join(sorted(ACOES))}")
    return linhas


def falha_do_sistema(o_que: str, erro: sistema.ErroNoSistema) -> tuple[int, dict]:
    """Mostra o detalhe técnico só no terminal e devolve a mensagem em português."""
    detalhe = f" (detalhe técnico: {erro.detalhe})" if erro.detalhe else ""
    print(f"[agente] não consegui {o_que}: {erro.mensagem}{detalhe}", file=sys.stderr)
    return 500, {"erro": erro.mensagem}


# Cada rota recebe o pedido (um dict) e devolve (status HTTP, resposta).

def rota_abrir(pedido: dict) -> tuple[int, dict]:
    nome = normalizar(pedido.get("programa", ""))
    if nome not in PROGRAMAS:
        return 404, {"erro": f"programa '{nome}' não está na lista"}
    try:
        abrir_programa(nome)
    except OSError as e:
        print(f"[agente] não consegui abrir '{nome}' (detalhe técnico: {e})")
        return 500, {"erro": explicar_falha(nome, e)}
    print(f"[agente] abriu: {nome}")
    return 200, {"ok": True, "programa": nome}


def rota_fechar(pedido: dict) -> tuple[int, dict]:
    nome = normalizar(pedido.get("programa", ""))
    if nome not in FECHAR:
        return 404, {"erro": f"'{nome}' não está na lista de programas que posso fechar"}
    try:
        resultado = sistema.fechar(FECHAR[nome])
    except sistema.ErroNoSistema as e:
        return falha_do_sistema(f"fechar {nome}", e)
    print(f"[agente] fechar {nome}: {resultado}")
    if resultado == "fechou":
        return 200, {"ok": True, "programa": nome, "descricao": f"fechei {nome}"}
    if resultado == "pediu":
        return 200, {"ok": True, "programa": nome, "descricao": f"pedi para fechar {nome}, mas ele continua "
                                                              "aberto; talvez esteja esperando você salvar algo"}
    if resultado == "nao_aberto":
        return 409, {"erro": f"{nome} não está aberto"}
    return 500, {"erro": f"o PC não conseguiu fechar {nome}"}


ACOES_VOLUME = ("consultar", "definir", "aumentar", "diminuir", "mudo", "tirar_mudo")


def ler_nivel(pedido: dict, minimo: int, padrao: int | None = None) -> int | None:
    """O "nivel" do pedido, de `minimo` a 100; None se for inválido (ou se faltar e não houver padrão)."""
    nivel = pedido.get("nivel")
    if nivel is None:
        return padrao
    if isinstance(nivel, bool) or not isinstance(nivel, (int, float)):  # True seria 1
        return None
    if isinstance(nivel, float):  # 35.0 vale; 35.5, NaN e infinito, não
        if not nivel.is_integer():
            return None
        nivel = int(nivel)
    return nivel if minimo <= nivel <= 100 else None


def mudar_volume(acao: str, nivel: int | None) -> tuple[int, bool, str]:
    """Faz a ação de volume e devolve (volume, mudo, descrição) como ficaram."""
    if acao == "definir":
        sistema.volume_definir(nivel)
        sistema.volume_mudo(False)  # depois do nível, para não soar alto no nível antigo
        return nivel, False, f"deixei o volume em {nivel}%"
    atual, mudo = sistema.volume_ler()  # lê antes de mexer: se a leitura falhar, nada muda
    if acao == "consultar":
        return atual, mudo, f"{'o PC está no mudo; ' if mudo else ''}o volume está em {atual}%"
    if acao == "aumentar":
        novo = min(100, atual + nivel)
        sistema.volume_definir(novo)
        sistema.volume_mudo(False)
        return novo, False, f"aumentei o volume para {novo}%"
    if acao == "diminuir":
        novo = max(0, atual - nivel)
        sistema.volume_definir(novo)
        return novo, mudo, f"diminuí o volume para {novo}%"
    if acao == "mudo":
        sistema.volume_mudo(True)
        return atual, True, "coloquei o PC no mudo"
    sistema.volume_mudo(False)  # tirar_mudo
    return atual, False, f"tirei o PC do mudo; o volume está em {atual}%"


def rota_volume(pedido: dict) -> tuple[int, dict]:
    if "volume" not in ACOES:
        return 403, {"erro": "o controle de volume está desligado no config_agente.json do PC"}
    acao = pedido.get("acao")
    if not isinstance(acao, str) or acao not in ACOES_VOLUME:
        return 400, {"erro": "ação de volume inválida; use consultar, definir, aumentar, diminuir, mudo "
                             "ou tirar_mudo"}
    nivel = None
    if acao == "definir":
        nivel = ler_nivel(pedido, 0)
        if nivel is None:
            return 400, {"erro": "para definir o volume, diga um nível de 0 a 100"}
    elif acao in ("aumentar", "diminuir"):
        nivel = ler_nivel(pedido, 1, padrao=10)
        if nivel is None:
            return 400, {"erro": f"diga quanto {acao} o volume, de 1 a 100"}
    try:
        volume, mudo, descricao = mudar_volume(acao, nivel)
    except sistema.ErroNoSistema as e:
        return falha_do_sistema("mexer no volume", e)
    print(f"[agente] volume: {descricao}")
    return 200, {"ok": True, "volume": volume, "mudo": mudo, "descricao": descricao}


ACOES_ENERGIA = {"bloquear": "bloquear a tela", "desligar": "desligar o PC", "cancelar": "cancelar o desligamento"}


def rota_energia(pedido: dict) -> tuple[int, dict]:
    acao = pedido.get("acao")
    if not isinstance(acao, str) or acao not in ACOES_ENERGIA:
        return 400, {"erro": "ação de energia inválida; use bloquear, desligar ou cancelar"}
    liberada = "bloquear" if acao == "bloquear" else "desligar"  # cancelar anda junto com desligar
    if liberada not in ACOES:
        return 403, {"erro": f"{ACOES_ENERGIA[liberada]} está desligado no config_agente.json"}
    # A confirmação por voz ("tem certeza?") é feita no servidor, antes de chegar aqui.
    try:
        if acao == "bloquear":
            sistema.bloquear()
            dados = {"ok": True, "acao": acao, "descricao": "bloqueei a tela"}
        elif acao == "desligar":
            prazo = sistema.desligar()
            dados = {"ok": True, "acao": acao, "prazo": prazo,
                     "descricao": f"o PC vai desligar em {prazo}; para cancelar, diga cancela o desligamento"}
        elif sistema.cancelar_desligamento():
            dados = {"ok": True, "acao": acao, "descricao": "cancelei o desligamento"}
        else:
            return 409, {"erro": "não havia desligamento agendado"}
    except sistema.ErroNoSistema as e:
        return falha_do_sistema(ACOES_ENERGIA[acao], e)
    print(f"[agente] energia: {dados['descricao']}")
    return 200, dados


ROTAS_POST = {"/abrir": rota_abrir, "/fechar": rota_fechar, "/volume": rota_volume, "/energia": rota_energia}


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
            return self._responder(200, {"programas": sorted(PROGRAMAS), "fechaveis": sorted(FECHAR),
                                         "acoes": sorted(ACOES)})
        self._responder(404, {"erro": "rota não encontrada"})

    def do_POST(self):
        corpo = self._ler_corpo()
        if not self._autorizado():
            return self._responder(401, {"erro": "token inválido"})
        rota = ROTAS_POST.get(self.path)
        if rota is None:
            return self._responder(404, {"erro": "rota não encontrada"})

        try:
            pedido = json.loads(corpo)
        except (TypeError, ValueError):  # tamanho inválido (corpo None), corpo vazio ou JSON quebrado
            pedido = None
        if not isinstance(pedido, dict):
            return self._responder(400, {"erro": "JSON inválido"})
        try:
            status, resposta = rota(pedido)
        except Exception:  # sem isto, a conexão cairia sem resposta e o servidor não saberia o que dizer
            print(f"[agente] erro inesperado em {self.path}:\n{traceback.format_exc()}", file=sys.stderr)
            status, resposta = 500, {"erro": "o agente do PC teve um erro inesperado; veja o terminal do PC"}
        self._responder(status, resposta)

    def log_message(self, formato, *args):
        pass  # silencia o log padrão; usamos nossos próprios prints


class Servidor(ThreadingHTTPServer):
    # No Windows, SO_REUSEADDR deixa um segundo agente escutar na mesma porta sem erro,
    # enquanto o antigo continua atendendo com o token e a lista antigos.
    allow_reuse_address = sys.platform != "win32"


def explicar_falha_ao_escutar(erro: OSError) -> str:
    # 10013 é o "acesso proibido" do Windows, que aparece nas portas reservadas pelo sistema (Hyper-V, WSL).
    proibida = isinstance(erro, PermissionError) or erro.errno == getattr(errno, "WSAEACCES", None)
    if proibida and PORTA < 1024:
        dica = ('O sistema só deixa usar portas acima de 1024: troque "porta" no config_agente.json e em pc_url, '
                "por exemplo para 8765.")
    elif proibida:
        dica = ('O sistema não deixou usar essa porta (no Windows, algumas ficam reservadas pelo Hyper-V ou pelo WSL): '
                'troque "porta" no config_agente.json e em pc_url, por exemplo para 8766.')
    else:
        dica = "Outro agente (ou outro programa) já usa essa porta: feche-o e abra o agente de novo."
    return f"Não consegui escutar na porta {PORTA} (detalhe técnico: {erro}).\n{dica}"


if __name__ == "__main__":
    try:
        servidor = Servidor(("0.0.0.0", PORTA), Handler)
    except OSError as e:
        sys.exit(explicar_falha_ao_escutar(e))
    print(f"[agente] escutando na porta {servidor.server_address[1]}")
    for linha in resumo_da_config():
        print(linha)
    print("[agente] para parar, aperte Ctrl+C.", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
    print("[agente] encerrado.")
