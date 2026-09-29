"""
Previsão do tempo pelo Open-Meteo, grátis e sem chave (dados CC BY 4.0, de open-meteo.com).

A cidade é buscada no geocoding do próprio Open-Meteo e guardada sem prazo; a previsão de cada lugar fica
guardada por 30 minutos. Se a consulta falhar, vale a última previsão com até 6 horas. O dia ("hoje",
"amanhã"...) é calculado no fuso da cidade, com o utc_offset_seconds que vem na resposta: não depende do
fuso do aparelho nem do tzdata.
"""
import math
import sys
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import requests

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
PREVISAO_URL = "https://api.open-meteo.com/v1/forecast"

TEMPO_LIMITE = 5  # segundos por chamada
VALIDADE = 30 * 60  # por quanto tempo a previsão guardada vale sem consultar de novo
VALIDADE_DA_COPIA = 6 * 60 * 60  # até quando a previsão guardada serve se a consulta falhar
DIAS_MAXIMO = 6  # a consulta pede 7 dias: de hoje até daqui a 6
DESLOCAMENTO_MAXIMO = 18 * 60 * 60  # os fusos reais vão de -12 h a +14 h
CAMPOS_DIARIOS = ("weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max")

MSG_SEM_CIDADE = 'diga a cidade, ou preencha "cidade" no config_servidor.json'
MSG_FALHA = "não consegui consultar a previsão do tempo agora"
MSG_FAIXA = "só consigo a previsão de hoje até daqui a 6 dias"

# Códigos WMO que o Open-Meteo usa. O weather_code diário é o tempo mais severo do dia: uma trovoada curta
# faz o dia inteiro virar "trovoada". O 96 e o 99 (granizo) só aparecem em alguns modelos.
WMO_PT = {
    0: "céu limpo", 1: "céu quase limpo", 2: "parcialmente nublado", 3: "nublado",
    45: "neblina", 48: "neblina com geada",
    51: "garoa fraca", 53: "garoa", 55: "garoa forte", 56: "garoa congelante", 57: "garoa congelante forte",
    61: "chuva fraca", 63: "chuva", 65: "chuva forte", 66: "chuva congelante", 67: "chuva congelante forte",
    71: "neve fraca", 73: "neve", 75: "neve forte", 77: "grãos de neve",
    80: "pancadas de chuva fracas", 81: "pancadas de chuva", 82: "pancadas de chuva fortes",
    85: "pancadas de neve", 86: "pancadas de neve fortes",
    95: "trovoada", 96: "trovoada com granizo", 97: "trovoada forte", 99: "trovoada com granizo forte",
}
CONDICAO_DESCONHECIDA = "tempo instável"

# Tabelas próprias: o locale do sistema vem em inglês no Windows.
DIAS_DA_SEMANA = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado",
                  "domingo")
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")


class ErroNoTempo(Exception):
    """Falha na previsão: a mensagem é em português; o detalhe técnico, para o terminal."""

    def __init__(self, mensagem: str, detalhe: str = ""):
        super().__init__(mensagem)
        self.detalhe = detalhe


def normalizar(texto) -> str:
    """Sem acentos, sem diferença de maiúsculas e com espaços simples."""
    decomposto = unicodedata.normalize("NFKD", str(texto))
    return " ".join("".join(c for c in decomposto if not unicodedata.combining(c)).casefold().split())


def resumir(texto: str) -> str:
    """Começo do corpo de uma resposta numa linha só, para o detalhe técnico no terminal."""
    return " ".join(str(texto)[:300].split()) or "(empty body)"


def numero(valor) -> float | int | None:
    """
    O valor, se for um número de verdade, senão None. O JSON do Python aceita NaN e Infinity, e um inteiro
    de 400 dígitos daria OverflowError no math.isfinite, por isso o int não passa por ele.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    if isinstance(valor, int):
        return valor
    return valor if math.isfinite(valor) else None


def condicao(codigo) -> str:
    """Código WMO em português; qualquer outra coisa vira "tempo instável"."""
    valor = numero(codigo)
    if valor is None or valor != int(valor):
        return CONDICAO_DESCONHECIDA
    return WMO_PT.get(int(valor), CONDICAO_DESCONHECIDA)


def nome_do_dia(data: date, dias_a_frente: int) -> str:
    """Devolve "hoje", "amanhã", "depois de amanhã" ou, a partir de 3 dias, "quinta-feira, 1º de outubro"."""
    if dias_a_frente < 3:
        return ("hoje", "amanhã", "depois de amanhã")[dias_a_frente]
    dia = "1º" if data.day == 1 else str(data.day)
    return f"{DIAS_DA_SEMANA[data.weekday()]}, {dia} de {MESES[data.month - 1]}"


def graus(valor: int) -> str:
    return f"{valor} grau" if abs(valor) == 1 else f"{valor} graus"


def como_esta(texto: str) -> str:
    """Dá "está nublado", mas "está com céu limpo" ou "está com chuva fraca"."""
    return f"está {texto}" if texto.endswith("nublado") else f"está com {texto}"


def escolher_lugar(resultados: list, pedido: str) -> dict | None:
    """
    Entre os resultados do geocoding, prefere o de nome igual ao pedido (a parte antes da vírgula, sem
    acento nem maiúsculas) que seja uma cidade (feature_code PPL*, mas não PPLX, que é bairro); senão, o
    primeiro. A busca é por prefixo e olha nomes alternativos, então o primeiro nem sempre é o certo.
    """
    validos = [r for r in resultados if isinstance(r, dict) and str(r.get("name") or "").strip()
               and numero(r.get("latitude")) is not None and numero(r.get("longitude")) is not None]
    procurado = normalizar(pedido.split(",")[0])
    for resultado in validos:
        codigo = str(resultado.get("feature_code") or "")
        if normalizar(resultado["name"]) == procurado and codigo.startswith("PPL") and codigo != "PPLX":
            return resultado
    return validos[0] if validos else None


def problema_na_previsao(dados) -> str | None:
    """O que falta na resposta do forecast (em inglês, para o terminal), ou None se ela está completa."""
    if not isinstance(dados, dict):
        return "the answer is not a JSON object"
    deslocamento = numero(dados.get("utc_offset_seconds"))
    if deslocamento is None:
        return "utc_offset_seconds is missing"
    if abs(deslocamento) > DESLOCAMENTO_MAXIMO:  # fora disso, a conta do dia daria OverflowError
        return f"utc_offset_seconds is out of range: {deslocamento}"
    atual = dados.get("current")
    if not isinstance(atual, dict) or "temperature_2m" not in atual or "weather_code" not in atual:
        return "current is missing"
    diario = dados.get("daily")
    if not isinstance(diario, dict):
        return "daily is missing"
    datas = diario.get("time")
    if not isinstance(datas, list) or not all(isinstance(d, str) for d in datas):
        return "daily.time is missing"
    for campo in CAMPOS_DIARIOS:
        if not isinstance(diario.get(campo), list) or len(diario[campo]) != len(datas):
            return f"daily.{campo} is missing or has the wrong length"
    return None


def achar_dia(dados: dict, dias_a_frente: int, agora_utc: datetime) -> tuple[date, int | None]:
    """A data pedida no fuso da cidade e a posição dela em daily.time (None se não estiver lá)."""
    hoje = (agora_utc + timedelta(seconds=dados["utc_offset_seconds"])).date()
    alvo = hoje + timedelta(days=dias_a_frente)
    try:
        return alvo, dados["daily"]["time"].index(alvo.isoformat())
    except ValueError:
        return alvo, None


def ler_dias(valor) -> int | None:
    """dias_a_frente vem do LLM: aceita 2, 2.0, "2", e None ou "" (hoje); o resto vira None."""
    if valor is None:
        return 0
    if isinstance(valor, bool):
        return None
    if isinstance(valor, str):
        if not valor.strip():  # o LLM às vezes manda "" num campo opcional, como faz com a cidade
            return 0
        try:
            return int(valor.strip())
        except ValueError:
            return None
    valor = numero(valor)
    return int(valor) if valor is not None and valor == int(valor) else None


def montar_resposta(lugar: dict, dados: dict, dias_a_frente: int, data: date, indice: int,
                    com_agora: bool) -> dict:
    """A resposta da ferramenta para o dia na posição `indice`; "agora" só entra no dia de hoje."""
    diario = dados["daily"]
    minima, maxima = numero(diario["temperature_2m_min"][indice]), numero(diario["temperature_2m_max"][indice])
    if minima is None or maxima is None:
        raise ErroNoTempo(MSG_FALHA, f"no temperature for {data.isoformat()} in the forecast")
    chance = numero(diario["precipitation_probability_max"][indice])
    resposta = {
        "cidade": lugar["exibido"],
        "dia": nome_do_dia(data, dias_a_frente),
        "data": data.isoformat(),
        "condicao": condicao(diario["weather_code"][indice]),
        "minima": round(minima),
        "maxima": round(maxima),
        "chance_de_chuva": None if chance is None else round(chance),
    }
    previsto = (f"{resposta['condicao']}, mínima de {resposta['minima']} e máxima de "
                f"{graus(resposta['maxima'])}")
    if resposta["chance_de_chuva"] is not None:
        previsto += f", {resposta['chance_de_chuva']}% de chance de chuva"

    temperatura = numero(dados["current"].get("temperature_2m"))
    if dias_a_frente == 0 and com_agora and temperatura is not None:
        resposta["agora"] = {"temperatura": round(temperatura), "condicao": condicao(dados["current"]["weather_code"])}
        resposta["descricao"] = (f"agora faz {graus(resposta['agora']['temperatura'])} e "
                                 f"{como_esta(resposta['agora']['condicao'])} em {lugar['nome']}; hoje: {previsto}")
    elif dias_a_frente < 3:
        resposta["descricao"] = f"{resposta['dia']} em {lugar['nome']}: {previsto}"
    else:
        resposta["descricao"] = f"{resposta['dia']}, em {lugar['nome']}: {previsto}"
    return resposta


class Tempo:
    """Previsão do tempo com os caches da cidade e da previsão; um objeto só atende o servidor inteiro."""

    def __init__(self, cidade_padrao: str | None = None, sessao=requests, relogio=time.monotonic,
                 agora_utc=lambda: datetime.now(timezone.utc), geocoding_url=GEOCODING_URL,
                 previsao_url=PREVISAO_URL):
        self.cidade_padrao = " ".join(str(cidade_padrao or "").split()) or None
        self._sessao = sessao
        self._relogio = relogio
        self._agora_utc = agora_utc
        self._geocoding_url = geocoding_url
        self._previsao_url = previsao_url
        self._trava = threading.Lock()  # o Flask atende pedidos em threads
        self._lugares = {}  # cidade normalizada -> lugar
        self._previsoes = {}  # (latitude, longitude) -> (momento do relógio, resposta do forecast)

    def previsao(self, cidade: str | None = None, dias_a_frente: int = 0) -> dict:
        """
        Previsão de `cidade` (ou da cidade padrão) daqui a `dias_a_frente` dias, de 0 (hoje) a 6.
        Levanta ErroNoTempo com a mensagem em português se não der.
        """
        dias = ler_dias(dias_a_frente)
        if dias is None or not 0 <= dias <= DIAS_MAXIMO:
            raise ErroNoTempo(MSG_FAIXA, f"dias_a_frente={dias_a_frente!r}")
        pedido = " ".join(str(cidade or "").split()) or self.cidade_padrao
        if not pedido:
            raise ErroNoTempo(MSG_SEM_CIDADE)
        lugar = self._buscar_lugar(pedido)
        agora_utc = self._agora_utc()
        dados, copia = self._buscar_previsao(lugar, dias, agora_utc)
        data, indice = achar_dia(dados, dias, agora_utc)
        if indice is None:
            raise ErroNoTempo(MSG_FAIXA, f"{data.isoformat()} is not in daily.time {dados['daily']['time']}")
        # A temperatura "agora" de uma cópia de horas atrás já não é a de agora: fica só a previsão do dia.
        return montar_resposta(lugar, dados, dias, data, indice, com_agora=not copia)

    def _pedir(self, url: str, parametros: dict):
        """GET que devolve o JSON; qualquer falha vira ErroNoTempo."""
        try:
            resposta = self._sessao.get(url, params=parametros, timeout=TEMPO_LIMITE)
        except requests.RequestException as e:
            raise ErroNoTempo(MSG_FALHA, f"{url}: {e!r}") from e
        # O status vem antes do JSON: um 5xx pode ser uma página HTML de um proxy.
        if resposta.status_code != 200:
            raise ErroNoTempo(MSG_FALHA, f"{url} answered HTTP {resposta.status_code}: {resumir(resposta.text)}")
        try:
            return resposta.json()
        except ValueError as e:  # inclui o requests.JSONDecodeError
            raise ErroNoTempo(MSG_FALHA, f"{url} sent invalid JSON: {resumir(resposta.text)}") from e

    def _buscar_lugar(self, pedido: str) -> dict:
        """Nome, nome exibido e coordenadas da cidade, do cache ou do geocoding. Aceita "Cidade, UF"."""
        chave = ", ".join(parte for parte in map(normalizar, pedido.split(",")) if parte)
        with self._trava:
            if chave in self._lugares:
                return self._lugares[chave]
        if len(normalizar(pedido.split(",")[0])) < 2:  # o geocoding exige pelo menos 2 letras
            raise ErroNoTempo(f"não encontrei a cidade {pedido}")
        dados = self._pedir(self._geocoding_url, {"name": pedido, "count": 5, "language": "pt", "format": "json"})
        if not isinstance(dados, dict):
            raise ErroNoTempo(MSG_FALHA, f"geocoding sent something that is not an object: {resumir(dados)}")
        resultados = dados.get("results")  # sem resultado, a chave nem vem
        escolhido = escolher_lugar(resultados, pedido) if isinstance(resultados, list) else None
        if escolhido is None:
            raise ErroNoTempo(f"não encontrei a cidade {pedido}")
        nome = str(escolhido["name"]).strip()
        estado = str(escolhido.get("admin1") or "").strip()
        lugar = {"nome": nome, "exibido": f"{nome}, {estado}" if estado else nome,
                 "latitude": escolhido["latitude"], "longitude": escolhido["longitude"]}
        with self._trava:
            self._lugares[chave] = lugar
        return lugar

    def _buscar_previsao(self, lugar: dict, dias: int, agora_utc: datetime) -> tuple[dict, bool]:
        """
        Resposta do forecast para o lugar e se ela é uma cópia antiga, usada porque a consulta falhou.
        A cópia guardada só vale se tiver o dia pedido: perto da meia-noite ela pode ter ficado de ontem.
        """
        coordenadas = (lugar["latitude"], lugar["longitude"])
        with self._trava:
            guardada = self._previsoes.get(coordenadas)
        agora = self._relogio()
        if guardada and agora - guardada[0] < VALIDADE and achar_dia(guardada[1], dias, agora_utc)[1] is not None:
            return guardada[1], False
        try:
            dados = self._pedir(self._previsao_url, {
                "latitude": coordenadas[0], "longitude": coordenadas[1],
                "current": "temperature_2m,weather_code", "daily": ",".join(CAMPOS_DIARIOS),
                "timezone": "auto",  # sem ele os dias viram dias em UTC, sem dar erro
                "forecast_days": DIAS_MAXIMO + 1,
            })
            problema = problema_na_previsao(dados)
            if problema:
                raise ErroNoTempo(MSG_FALHA, f"unexpected forecast answer: {problema}")
        except ErroNoTempo as erro:
            idade = agora - guardada[0] if guardada else None
            if idade is None or idade > VALIDADE_DA_COPIA or achar_dia(guardada[1], dias, agora_utc)[1] is None:
                raise
            print(f"[servidor] a consulta da previsão do tempo falhou; usei a de {int(idade // 60)} minutos atrás "
                  f"(detalhe técnico: {erro.detalhe})", file=sys.stderr)
            return guardada[1], True
        with self._trava:
            # Tira as que já não servem nem como cópia, para o cache não crescer com cada cidade pedida.
            for chave in [c for c, (momento, _) in self._previsoes.items() if agora - momento > VALIDADE_DA_COPIA]:
                del self._previsoes[chave]
            self._previsoes[coordenadas] = (agora, dados)
        return dados, False
