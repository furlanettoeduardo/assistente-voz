"""
Lâmpada Tuya (como a Elgin Smart Color) controlada pela rede local com o tinytuya, sem a nuvem.

Fala direto com os DPS das lâmpadas Tuya "v2": 20 liga, 21 modo, 22 brilho, 23 temperatura do branco
e 24 cor. Cada pedido lê o estado e manda todos os DPS numa chamada só, com espera curta: com os valores
padrão do tinytuya, uma lâmpada desligada no interruptor prenderia o pedido por até 90 segundos. Com os
valores daqui, cada chamada faz até duas tentativas de 2 segundos (e, se a lâmpada recusar vários DPS
juntos, o tinytuya reenvia um por um).
"""
import contextlib
import ipaddress
import threading

# Cores que o LLM pode escolher. Os brancos usam o modo "white" com a temperatura do DP 23
# (0 = quente, 1000 = frio, o padrão da Tuya); as outras usam o modo "colour" com matiz em graus
# e saturação de 0 a 1000. O "branco" é o branco puro, sem nada de amarelo; "branco frio" fica
# como outro nome dele, para o LLM não ter de escolher.
BRANCOS = {"branco": 1000, "branco neutro": 500, "branco quente": 0, "branco frio": 1000}
CORES = {"vermelho": (0, 1000), "laranja": (30, 1000), "amarelo": (55, 1000), "verde": (120, 1000),
         "ciano": (180, 1000), "azul": (240, 1000), "roxo": (275, 1000), "rosa": (330, 700)}
NOMES_DAS_CORES = [*BRANCOS, *CORES]

CAMPOS = ("id", "chave_local", "ip", "versao")
VERSOES = (3.1, 3.3, 3.4, 3.5)
PORTA_TUYA = 6668
TEMPO_LIMITE = 2.0  # segundos por tentativa; com connection_retry_limit=1, o tinytuya faz até duas

SEM_RESPOSTA = {"902", "905"}  # lâmpada inalcançável ou calada
CONEXAO_RECUSADA = {"901"}  # recusou ou derrubou a conexão: desligada ou com versão errada (3.5 tratada como 3.3)
CHAVE_OU_VERSAO = {"900", "904", "914"}  # resposta que não decifra ou negociação de chave que falha


class ErroNaLampada(Exception):
    """Falha ao falar com a lâmpada: a mensagem é em português; o detalhe técnico, do tinytuya."""

    def __init__(self, mensagem: str, detalhe: str = ""):
        super().__init__(mensagem)
        self.detalhe = detalhe


def ler_config(bloco, exemplo) -> tuple[dict | None, list[str]]:
    """
    Confere o bloco "lampada" do config. Devolve (config, campos que faltam):
    - (None, []): não há lâmpada configurada;
    - (None, [campos]): falta preencher algum campo (vazio ou com o valor de exemplo), então a
      lâmpada fica desligada, mas o resto do servidor funciona;
    - (config pronta, []).
    Um valor com formato errado levanta ValueError com a explicação em português.
    """
    if bloco is None:
        return None, []
    if not isinstance(bloco, dict):
        raise ValueError('"lampada" precisa ser um objeto entre chaves { }, como no exemplo.')
    exemplo = exemplo if isinstance(exemplo, dict) else {}
    faltando = [campo for campo in CAMPOS
                if str(bloco.get(campo) or "").strip() in ("", str(exemplo.get(campo, "")))]
    if faltando:
        return None, faltando

    dev_id = str(bloco["id"]).strip()
    chave = str(bloco["chave_local"]).strip()
    ip = str(bloco["ip"]).strip()
    if not dev_id.isascii() or " " in dev_id:
        raise ValueError('"id" da lâmpada precisa ser o "id" do devices.json, sem espaços nem acentos.')
    if len(chave) != 16 or not chave.isascii():
        raise ValueError('"chave_local" da lâmpada precisa ter 16 caracteres: é o "key" do devices.json.')
    try:
        endereco = ipaddress.ip_address(ip)
    except ValueError:
        endereco = None
    # 0.0.0.0 faria o tinytuya procurar a lâmpada na rede a cada pedido, travando uns 18 segundos.
    if endereco is None or endereco.is_unspecified or endereco.is_multicast:
        raise ValueError(f'"ip" da lâmpada precisa ser o endereço dela na rede, como 192.168.0.20 (veio "{ip}").')
    try:
        versao = float(bloco["versao"])
    except (TypeError, ValueError):
        versao = None
    if versao not in VERSOES:
        raise ValueError('"versao" da lâmpada precisa ser 3.1, 3.3, 3.4 ou 3.5: é a "Version" do tinytuya scan.')
    return {"id": dev_id, "chave_local": chave, "ip": ip, "versao": versao}, []


def hsv16(matiz: int, saturacao: int, valor: int) -> str:
    """Cor no formato do DP 24: 12 dígitos hex, matiz 0-360 e saturação e valor 0-1000."""
    return f"{matiz:04x}{saturacao:04x}{valor:04x}"


def ler_hsv16(texto) -> tuple[int, int, int] | None:
    if not isinstance(texto, str) or len(texto) != 12:
        return None
    try:
        return int(texto[0:4], 16), int(texto[4:8], 16), int(texto[8:12], 16)
    except ValueError:
        return None


def brilho_atual(dps: dict) -> int:
    """Brilho de 10 a 1000 no modo em que a lâmpada está (no modo cor, é o V do DP 24)."""
    hsv = ler_hsv16(dps.get("24"))
    if dps.get("21") == "colour" and hsv:
        valor = hsv[2]
    elif isinstance(dps.get("22"), int) and not isinstance(dps.get("22"), bool):
        valor = dps["22"]
    else:
        valor = 1000
    return min(1000, max(10, valor))


def montar_comando(dps: dict, ligar=None, brilho=None, cor=None) -> tuple[dict, str]:
    """
    Traduz o pedido em DPS para mandar numa chamada só, a partir do estado atual da lâmpada.
    `brilho` vem em porcentagem (1-100) e `cor` de NOMES_DAS_CORES. Devolve (DPS, o que foi feito).
    """
    if "20" not in dps or "21" not in dps:
        raise ErroNaLampada("este modelo de lâmpada usa outros comandos e ainda não é suportado",
                            detalhe=f"DPS informados: {sorted(dps)}")
    if ligar is False:
        return {"20": False}, "desliguei a lâmpada"

    comando = {"20": True}
    alvo = None if brilho is None else max(10, min(1000, brilho * 10))
    if cor in BRANCOS:
        comando.update({"21": "white", "22": alvo or brilho_atual(dps), "23": BRANCOS[cor]})
        feito = f"deixei a lâmpada em {cor}"
    elif cor in CORES:
        matiz, saturacao = CORES[cor]
        comando.update({"21": "colour", "24": hsv16(matiz, saturacao, alvo or brilho_atual(dps))})
        feito = f"deixei a lâmpada em {cor}"
    elif brilho is not None:
        hsv = ler_hsv16(dps.get("24"))
        if dps.get("21") == "colour" and hsv:  # no modo cor, o brilho é o V da cor atual
            comando["24"] = hsv16(hsv[0], hsv[1], alvo)
        elif dps.get("21") == "white":
            comando["22"] = alvo
        else:  # numa cena ou no modo música o DP 22 não aparece: volta para o branco
            comando.update({"21": "white", "22": alvo})
        return comando, f"mudei o brilho da lâmpada para {brilho}%"
    else:
        return comando, "liguei a lâmpada"
    if brilho is not None:
        feito += f" com {brilho}% de brilho"
    return comando, feito


def conferir(resposta) -> dict:
    """Levanta ErroNaLampada se o tinytuya devolveu um erro (ele não levanta exceção)."""
    if not isinstance(resposta, dict):
        raise ErroNaLampada("a lâmpada não respondeu", detalhe=repr(resposta))
    codigo = resposta.get("Err")
    if codigo is None:
        return resposta
    detalhe = f"{codigo} {resposta.get('Error')}"
    if str(codigo) in SEM_RESPOSTA:
        raise ErroNaLampada("a lâmpada não respondeu: confira se o interruptor dela está ligado e se ela "
                            "está na mesma rede que o servidor", detalhe)
    if str(codigo) in CONEXAO_RECUSADA:
        raise ErroNaLampada("a lâmpada não aceitou a conexão: confira se ela está ligada e na mesma rede, e se a "
                            '"versao" no config_servidor.json é a do tinytuya scan', detalhe)
    if str(codigo) in CHAVE_OU_VERSAO:
        raise ErroNaLampada('a lâmpada recusou a conexão: confira a "chave_local" e a "versao" no '
                            "config_servidor.json", detalhe)
    raise ErroNaLampada(f"a lâmpada respondeu com o erro {codigo}", detalhe)


def dispositivo_tinytuya(**parametros):
    """Cria o tinytuya.Device sem os efeitos colaterais da biblioteca no terminal."""
    import logging
    import tinytuya
    try:
        import colorama
        colorama.deinit()  # o tinytuya embrulha o stdout e o stderr ao ser importado
    except ImportError:
        pass
    # Em DEBUG ele imprime a chave de sessão e passa a usar um IV fixo no protocolo 3.5.
    logging.getLogger("tinytuya").setLevel(logging.WARNING)
    return tinytuya.Device(**parametros)


class Lampada:
    def __init__(self, config: dict, fabrica=dispositivo_tinytuya, porta: int = PORTA_TUYA):
        self.config = config
        self._fabrica = fabrica
        self._porta = porta
        self._trava = threading.Lock()  # a lâmpada aceita uma conexão por vez

    def _dispositivo(self):
        return self._fabrica(dev_id=self.config["id"], address=self.config["ip"],
                             local_key=self.config["chave_local"], version=self.config["versao"],
                             port=self._porta, connection_timeout=TEMPO_LIMITE,
                             connection_retry_limit=1, connection_retry_delay=0)

    def estado(self) -> dict:
        """DPS atuais da lâmpada."""
        with self._trava, traduzir_erros():
            return ler_estado(self._dispositivo())

    def controlar(self, ligar=None, brilho=None, cor=None) -> str:
        """Aplica o pedido e devolve o que foi feito; levanta ErroNaLampada se não der."""
        with self._trava, traduzir_erros():
            dispositivo = self._dispositivo()
            dps = ler_estado(dispositivo)
            comando, feito = montar_comando(dps, ligar, brilho, cor)
            if all(dps.get(dp) == valor for dp, valor in comando.items()):
                return feito  # já está assim: nada a mandar
            resposta = dispositivo.set_multiple_values(comando)
            # None quer dizer que a lâmpada confirmou o comando sem mandar o estado de volta, como alguns
            # firmwares fazem.
            if resposta is not None:
                conferir(resposta)
            return feito


@contextlib.contextmanager
def traduzir_erros():
    """Transforma qualquer exceção do tinytuya (RuntimeError, ValueError, OSError...) em ErroNaLampada."""
    try:
        yield
    except ErroNaLampada:
        raise
    except Exception as e:
        raise ErroNaLampada("não consegui falar com a lâmpada", repr(e)) from e


def ler_estado(dispositivo) -> dict:
    dps = conferir(dispositivo.status()).get("dps")
    if not isinstance(dps, dict):
        raise ErroNaLampada("a lâmpada mandou um estado que não entendi", detalhe=repr(dps))
    return dps
