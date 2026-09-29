"""
Ações no sistema para o agente do PC: volume, fechar programas, bloquear a tela e desligar.

- Só biblioteca padrão. O ctypes do Windows só aparece dentro das funções do Windows, então o
  módulo pode ser importado em qualquer sistema.
- Comandos sempre como lista, sem shell. Falhas viram ErroNoSistema com a mensagem em português
  para o usuário e o detalhe técnico à parte, para o terminal.
"""
import csv
import functools
import os
import re
import shutil
import subprocess
import sys
import time

PLATAFORMA = sys.platform  # os testes trocam para simular outro sistema
TIMEOUT = 10  # segundos para cada comando
ESPERA_FECHAR = 4.0  # por quanto tempo conferir se o programa fechou depois do pedido
INTERVALO_FECHAR = 0.5
LIMITE_DO_NOME_NO_LINUX = 15  # o Linux guarda só os 15 primeiros caracteres do nome do processo
dormir = time.sleep  # os testes trocam para não esperar de verdade

# Sem janela de console piscando quando o agente roda sem terminal. O getattr deixa os testes
# simularem o Windows em outro sistema.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

ENVIADO, NAO_ABERTO, FALHOU = "enviado", "nao_aberto", "falhou"

# Core Audio (IAudioEndpointVolume), índices da vtable: 0 a 2 são do IUnknown.
GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator
ACTIVATE = 3  # IMMDevice
SET_MASTER_SCALAR, GET_MASTER_SCALAR, SET_MUTE, GET_MUTE = 7, 9, 14, 15
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0
E_NOTFOUND = 0x80070490  # não há saída de som
RPC_E_CHANGED_MODE = 0x80010106  # a thread já iniciou o COM em outro modo (dá para usar assim mesmo)

SEM_SAIDA_DE_SOM = "o PC não tem uma saída de som ativa"
SEM_VOLUME_NESTE_SISTEMA = "o controle de volume ainda não funciona neste sistema"
SEM_FERRAMENTA_DE_VOLUME = "não achei o wpctl nem o pactl para controlar o volume neste PC"


class ErroNoSistema(Exception):
    """Falha numa ação do sistema: `mensagem` vai para o usuário; `detalhe`, só para o terminal."""

    def __init__(self, mensagem: str, detalhe: str = ""):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.detalhe = detalhe


def _windows() -> bool:
    return PLATAFORMA == "win32"


def _linux() -> bool:
    return PLATAFORMA.startswith("linux")


def _registrar(texto: str) -> None:
    print(f"[agente] {texto}", file=sys.stderr, flush=True)


def _rodar(comando: list[str], erro: str, ambiente: dict | None = None) -> subprocess.CompletedProcess:
    """Roda `comando` sem shell e devolve o resultado. Se nem der para rodar, levanta ErroNoSistema(erro)."""
    opcoes = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "errors": "replace",
              "timeout": TIMEOUT}
    if _windows():
        opcoes.update(encoding="oem", creationflags=CREATE_NO_WINDOW)  # os comandos do Windows falam em OEM
    else:
        opcoes.update(encoding="utf-8")
    if ambiente is not None:
        opcoes["env"] = ambiente
    programa = os.path.basename(comando[0])  # "shutdown", e não "/usr/sbin/shutdown", na mensagem falada
    try:
        return subprocess.run(comando, **opcoes)
    except FileNotFoundError as e:
        raise ErroNoSistema(f"não encontrei o comando {programa} neste PC", str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise ErroNoSistema(f"o comando {programa} demorou demais para responder", str(e)) from e
    except OSError as e:
        raise ErroNoSistema(erro, str(e)) from e


def _detalhe(resultado: subprocess.CompletedProcess) -> str:
    saida = " ".join(f"{resultado.stdout or ''} {resultado.stderr or ''}".split())
    return f"{resultado.args[0]} exit code {resultado.returncode}: {saida}"


# ---------- Volume ----------

def volume_ler() -> tuple[int, bool]:
    """Devolve (nível de 0 a 100, mudo)."""
    if _windows():
        return _volume_windows(_ler_windows)
    if _linux():
        return _volume_ler_linux()
    raise ErroNoSistema(SEM_VOLUME_NESTE_SISTEMA)


def volume_definir(nivel: int) -> None:
    """Deixa o volume em `nivel` (0 a 100). Não mexe no mudo."""
    nivel = max(0, min(100, int(nivel)))
    if _windows():
        return _volume_windows(lambda com, volume: _definir_windows(com, volume, nivel))
    if _linux():
        return _volume_definir_linux(nivel)
    raise ErroNoSistema(SEM_VOLUME_NESTE_SISTEMA)


def volume_mudo(mudo: bool) -> None:
    """Liga (True) ou desliga (False) o mudo."""
    if _windows():
        return _volume_windows(lambda com, volume: _mudo_windows(com, volume, bool(mudo)))
    if _linux():
        return _volume_mudo_linux(bool(mudo))
    raise ErroNoSistema(SEM_VOLUME_NESTE_SISTEMA)


@functools.cache
def _core_audio():
    """Monta, uma vez, as peças de ctypes do Core Audio. Só funciona no Windows."""
    import ctypes
    import types
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                    ("d4", ctypes.c_ubyte * 8)]

    def guid(texto: str) -> GUID:
        return GUID.from_buffer_copy(uuid.UUID(texto).bytes_le)

    ole32 = ctypes.OleDLL("ole32")  # OleDLL: um HRESULT de falha vira OSError
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None  # void: sem isso o OleDLL leria lixo como HRESULT
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    return types.SimpleNamespace(
        ctypes=ctypes, wintypes=wintypes, GUID=GUID, ole32=ole32,
        CLSID_MMDeviceEnumerator=guid("bcde0395-e52f-467c-8e3d-c4579291692e"),
        IID_IMMDeviceEnumerator=guid("a95664d2-9614-4f35-a746-de8db63617e6"),
        IID_IAudioEndpointVolume=guid("5cdf2c82-841e-4546-9722-0cf74078229a"),
    )


def _metodo(com, objeto, indice: int, *tipos):
    """Método `indice` da vtable da interface COM `objeto`, pronto para chamar sem o this."""
    ctypes = com.ctypes
    vtable = ctypes.cast(objeto, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    funcao = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *tipos)(vtable[indice])
    return lambda *argumentos: funcao(objeto, *argumentos)


def _liberar(com, objeto) -> None:
    """IUnknown::Release, se a interface chegou a ser obtida."""
    if objeto:
        ctypes = com.ctypes
        vtable = ctypes.cast(objeto, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(objeto)


def _hresult(erro: OSError) -> int:
    return (getattr(erro, "winerror", None) or 0) & 0xFFFFFFFF


def _erro_de_volume(erro: OSError) -> ErroNoSistema:
    codigo = _hresult(erro)
    detalhe = f"Core Audio HRESULT 0x{codigo:08X}: {erro}"
    if codigo == E_NOTFOUND:
        return ErroNoSistema(SEM_SAIDA_DE_SOM, detalhe)
    return ErroNoSistema("não consegui controlar o volume do PC", detalhe)


def _volume_windows(acao):
    """
    Abre o IAudioEndpointVolume da saída de som padrão, roda acao(com, volume) e libera tudo.
    Cada pedido do agente roda numa thread nova, por isso o COM é iniciado a cada chamada.
    """
    try:
        com = _core_audio()
        com.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)  # S_FALSE (já iniciado) também conta
        iniciou = True
    except OSError as e:
        if _hresult(e) != RPC_E_CHANGED_MODE:
            raise _erro_de_volume(e) from e
        iniciou = False  # a thread já tinha o COM em outro modo: usa assim e não finaliza
    ctypes, wintypes = com.ctypes, com.wintypes
    enumerador, dispositivo, volume = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        com.ole32.CoCreateInstance(ctypes.byref(com.CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                   ctypes.byref(com.IID_IMMDeviceEnumerator), ctypes.byref(enumerador))
        # GetDefaultAudioEndpoint(eRender, eConsole): E_NOTFOUND quando não há saída de som.
        _metodo(com, enumerador, GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_uint, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p))(0, 0, ctypes.byref(dispositivo))
        _metodo(com, dispositivo, ACTIVATE, ctypes.POINTER(com.GUID), wintypes.DWORD, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(com.IID_IAudioEndpointVolume), CLSCTX_ALL, None,
                                                 ctypes.byref(volume))
        return acao(com, volume)
    except OSError as e:
        raise _erro_de_volume(e) from e
    finally:
        for interface in (volume, dispositivo, enumerador):
            _liberar(com, interface)
        if iniciou:
            com.ole32.CoUninitialize()


def _ler_windows(com, volume) -> tuple[int, bool]:
    ctypes = com.ctypes
    nivel, mudo = ctypes.c_float(), com.wintypes.BOOL()
    _metodo(com, volume, GET_MASTER_SCALAR, ctypes.POINTER(ctypes.c_float))(ctypes.byref(nivel))
    _metodo(com, volume, GET_MUTE, ctypes.POINTER(com.wintypes.BOOL))(ctypes.byref(mudo))
    return max(0, min(100, round(nivel.value * 100))), bool(mudo.value)


def _definir_windows(com, volume, nivel: int) -> None:
    ctypes = com.ctypes  # a escala de 0.0 a 1.0 é a mesma do controle de volume do Windows
    _metodo(com, volume, SET_MASTER_SCALAR, ctypes.c_float, ctypes.c_void_p)(nivel / 100, None)


def _mudo_windows(com, volume, mudo: bool) -> None:
    _metodo(com, volume, SET_MUTE, com.wintypes.BOOL, com.ctypes.c_void_p)(mudo, None)


def _ferramenta_de_volume() -> str:
    for nome in ("wpctl", "pactl"):  # wpctl é o do PipeWire; o pactl também funciona nele (pipewire-pulse)
        if shutil.which(nome):
            return nome
    raise ErroNoSistema(SEM_FERRAMENTA_DE_VOLUME)


def _rodar_volume(comando: list[str], erro: str) -> str:
    # LC_ALL=C: saída em inglês e com ponto decimal, do jeito que o código lê.
    resultado = _rodar(comando, erro, ambiente={**os.environ, "LC_ALL": "C"})
    if resultado.returncode != 0:
        raise ErroNoSistema(erro, _detalhe(resultado))
    return resultado.stdout or ""


def _volume_ler_linux() -> tuple[int, bool]:
    erro = "não consegui ler o volume do PC"
    if _ferramenta_de_volume() == "wpctl":
        saida = _rodar_volume(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], erro)  # "Volume: 0.40 [MUTED]"
        achado = re.search(r"Volume:\s*(\d+(?:[.,]\d+)?)", saida)
        if not achado:
            raise ErroNoSistema(erro, f"wpctl output not understood: {saida!r}")
        nivel = round(float(achado.group(1).replace(",", ".")) * 100)
        return max(0, min(100, nivel)), "[MUTED]" in saida
    saida = _rodar_volume(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], erro)
    canais = re.findall(r"(\d+)%", saida)  # "Volume: front-left: 26214 /  40% / -23.88 dB, ..."
    mudo = re.search(r"Mute:\s*(yes|no)", _rodar_volume(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], erro))
    if not canais or not mudo:
        raise ErroNoSistema(erro, f"pactl output not understood: {saida!r}")
    # Com balanço, os canais diferem: vale o mais alto, como no controle de volume do GNOME.
    return max(0, min(100, max(int(c) for c in canais))), mudo.group(1) == "yes"


def _volume_definir_linux(nivel: int) -> None:
    erro = "não consegui mudar o volume do PC"
    if _ferramenta_de_volume() == "wpctl":
        _rodar_volume(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{nivel}%"], erro)
    else:
        _rodar_volume(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{nivel}%"], erro)


def _volume_mudo_linux(mudo: bool) -> None:
    erro = "não consegui mudar o mudo do PC"
    valor = "1" if mudo else "0"
    if _ferramenta_de_volume() == "wpctl":
        _rodar_volume(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", valor], erro)
    else:
        _rodar_volume(["pactl", "set-sink-mute", "@DEFAULT_SINK@", valor], erro)


# ---------- Fechar programas ----------

def fechar(processos: list[str]) -> str:
    """
    Pede para os processos fecharem e confere por alguns segundos. No Windows é como o X da janela
    (o programa pode perguntar se quer salvar); no Linux o pkill manda SIGTERM, que costuma fechar o
    programa na hora, sem perguntar. Devolve "fechou", "pediu" (pedido enviado, mas continua aberto),
    "nao_aberto" ou "nao_fechou" (nenhum pedido chegou).
    """
    if _windows():
        pedir, existe = _pedir_fechar_windows, _existe_windows
    else:
        pedir, existe = _pedir_fechar_posix, _existe_posix
    situacoes = {processo: pedir(processo) for processo in processos}
    if all(situacao == NAO_ABERTO for situacao in situacoes.values()):
        return "nao_aberto"
    if ENVIADO not in situacoes.values():
        return "nao_fechou"
    # Confere também os que recusaram o pedido: costumam sair junto com o processo principal.
    abertos = [processo for processo, situacao in situacoes.items() if situacao != NAO_ABERTO]
    return "pediu" if _esperar_fechar(abertos, existe) else "fechou"


def _esperar_fechar(processos: list[str], existe) -> list[str]:
    """Confere por até ESPERA_FECHAR segundos; devolve os processos que continuam abertos."""
    restantes = [processo for processo in processos if existe(processo)]
    for _ in range(round(ESPERA_FECHAR / INTERVALO_FECHAR)):
        if not restantes:
            break
        dormir(INTERVALO_FECHAR)
        restantes = [processo for processo in restantes if existe(processo)]
    return restantes


def _pedir_fechar_windows(processo: str) -> str:
    # Sem /F: o taskkill manda WM_CLOSE, como o X da janela. Sem /T também: com /T e sem /F, o taskkill
    # não fecha um processo cujos filhos não têm janela (o conhost, os auxiliares do Chrome) e não manda
    # nada para ninguém, devolvendo 128, o mesmo código de "não encontrado".
    resultado = _rodar(["taskkill", "/IM", processo], "o PC não conseguiu fechar o programa")
    if resultado.returncode == 0:  # pedido enviado a pelo menos uma janela; decide pelo código, não pelo texto
        return ENVIADO
    if resultado.returncode == 128:
        return NAO_ABERTO
    _registrar(f"taskkill não conseguiu pedir para {processo} fechar ({_detalhe(resultado)})")
    return FALHOU


def _existe_windows(processo: str) -> bool:
    erro = "não consegui conferir se o programa fechou"
    resultado = _rodar(["tasklist", "/FI", f"IMAGENAME eq {processo}", "/FO", "CSV", "/NH"], erro)
    if resultado.returncode != 0:
        raise ErroNoSistema(erro, _detalhe(resultado))
    # Sem nenhum processo, o tasklist escreve uma frase ("INFORMAÇÕES: nenhuma tarefa...") em vez do CSV.
    alvo = processo.lower()
    return any(linha and linha[0].strip().lower() == alvo
               for linha in csv.reader((resultado.stdout or "").splitlines()))


def _escapar(processo: str) -> str:
    """O pkill e o pgrep leem o nome como expressão regular: sem escapar, "code.bin" casaria com "codexbin"."""
    return re.sub(r"[.^$*+?()\[\]{}|\\]", lambda achado: "\\" + achado.group(), processo)


def _busca_exata(processo: str) -> list[str]:
    """
    Opções do pkill e do pgrep que acham só o processo com esse nome. No Linux, o -x compara com o nome
    que o sistema guarda, cortado em 15 caracteres: com o nome inteiro ("gnome-calculator") não acharia
    nada, e com o nome cortado ("gnome-calculato") acharia também o gnome-calculator-search-provider.
    Por isso um nome mais comprido é comparado com o comando que abriu o processo (o argv[0], com ou sem
    a pasta). Um espaço separa o argv[0] dos argumentos; [^ ] em vez de \\S funciona em qualquer pgrep.
    """
    if _linux() and len(processo) > LIMITE_DO_NOME_NO_LINUX:
        return ["-f", f"^([^ ]*/)?{_escapar(processo)}( |$)"]
    return ["-x", _escapar(processo)]


def _pedir_fechar_posix(processo: str) -> str:
    resultado = _rodar(["pkill", *_busca_exata(processo)], "o PC não conseguiu fechar o programa")  # SIGTERM
    if resultado.returncode == 0:
        return ENVIADO
    # Código 1 quer dizer "nada casou" OU "sem permissão para mandar o sinal": o pgrep desempata.
    if resultado.returncode == 1 and not _existe_posix(processo):
        return NAO_ABERTO
    _registrar(f"pkill não conseguiu pedir para {processo} fechar ({_detalhe(resultado)})")
    return FALHOU


def _existe_posix(processo: str) -> bool:
    erro = "não consegui conferir se o programa fechou"
    resultado = _rodar(["pgrep", *_busca_exata(processo)], erro)
    if resultado.returncode in (0, 1):
        return resultado.returncode == 0
    raise ErroNoSistema(erro, _detalhe(resultado))


# ---------- Bloquear e desligar ----------

def bloquear() -> None:
    """Bloqueia a tela do usuário logado."""
    if _windows():
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.LockWorkStation():  # só "iniciado": a função é assíncrona
            raise ErroNoSistema("o Windows não deixou bloquear a tela",
                                f"LockWorkStation failed, WinError {ctypes.get_last_error()}")
        return
    if _linux():
        erro = "o PC não conseguiu bloquear a tela"
        resultado = _rodar(["loginctl", "lock-session"], erro)
        if resultado.returncode != 0:
            raise ErroNoSistema(erro, _detalhe(resultado))
        return
    raise ErroNoSistema("bloquear a tela ainda não funciona neste sistema")


def _shutdown_linux() -> str:
    """Caminho do shutdown. No Debian ele fica em /usr/sbin, fora do PATH de um usuário comum."""
    pastas = os.pathsep.join([os.environ.get("PATH", os.defpath), "/usr/sbin", "/sbin"])
    caminho = shutil.which("shutdown", path=pastas)
    if caminho is None:
        raise ErroNoSistema("não encontrei o comando shutdown neste PC", f"shutdown not found in {pastas}")
    return caminho


def desligar() -> str:
    """Agenda o desligamento e devolve em quanto tempo ele acontece."""
    erro = "o PC não conseguiu agendar o desligamento"
    if _windows():
        # Com /t maior que 0, o /f vem junto: os programas fecham sem perguntar. Por isso a confirmação
        # por voz (no servidor) e o cancelamento.
        resultado = _rodar(["shutdown", "/s", "/t", "30", "/c",
                            "A assistente de voz vai desligar o PC em 30 segundos."], erro)
        if resultado.returncode == 1190:  # ERROR_SHUTDOWN_IS_SCHEDULED
            raise ErroNoSistema("já existe um desligamento agendado", _detalhe(resultado))
        prazo = "30 segundos"
    elif _linux():
        resultado = _rodar([_shutdown_linux(), "-P", "+1"], erro)  # o tempo é em minutos
        prazo = "1 minuto"
    else:
        raise ErroNoSistema("desligar o PC ainda não funciona neste sistema")
    if resultado.returncode != 0:
        raise ErroNoSistema(erro, _detalhe(resultado))
    return prazo


def cancelar_desligamento() -> bool:
    """Cancela o desligamento agendado. False se não havia nenhum (no Linux não dá para saber: True)."""
    erro = "o PC não conseguiu cancelar o desligamento"
    if _windows():
        resultado = _rodar(["shutdown", "/a"], erro)
        if resultado.returncode == 1116:  # ERROR_NO_SHUTDOWN_IN_PROGRESS
            return False
    elif _linux():
        resultado = _rodar([_shutdown_linux(), "-c"], erro)
    else:
        raise ErroNoSistema("desligar o PC ainda não funciona neste sistema")
    if resultado.returncode != 0:
        raise ErroNoSistema(erro, _detalhe(resultado))
    return True
