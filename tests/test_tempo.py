"""
Testes da previsão do tempo (celular_servidor/tempo.py). O Open-Meteo é um servidor falso em 127.0.0.1,
com o geocoding e o forecast no mesmo servidor, separados pelo caminho: nada sai para a internet.
"""
import contextlib
import importlib.util
import io
import json
import os
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.auxiliares import PASTA_SERVIDOR, ServidorLocal

TEM_REQUESTS = importlib.util.find_spec("requests") is not None
if TEM_REQUESTS:
    import requests

    spec = importlib.util.spec_from_file_location("tempo_em_teste", PASTA_SERVIDOR / "tempo.py")
    tempo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tempo)

PRECISA_REQUESTS = unittest.skipUnless(TEM_REQUESTS, "instale celular_servidor/requirements.txt (requests)")

# Segunda-feira, 28/09/2026, 18h em Brasília (21h UTC).
AGORA = datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc)
SJP = {"id": 3448636, "name": "São José dos Pinhais", "latitude": -25.53472, "longitude": -49.20583,
       "elevation": 906.0, "feature_code": "PPLA2", "country_code": "BR", "country": "Brasil",
       "admin1": "Paraná", "admin2": "São José dos Pinhais", "timezone": "America/Sao_Paulo"}
# Valores da resposta real de 28/09/2026 nos dois primeiros dias; os outros, inventados.
CODIGOS = [51, 81, 3, 61, 96, 0, 2]
MAXIMAS = [31.6, 32.8, 27.0, 22.4, 28.2, 30.5, 25.0]
MINIMAS = [17.9, 18.5, 16.2, 14.0, 15.4, 12.6, 13.3]
CHUVA = [6, 73, 20, 90, 100, 0, 15]


def resposta_previsao(inicio="2026-09-28", offset=-10800, dias=7, temperatura=21.1, codigo=3) -> dict:
    """Resposta do forecast no formato real, com `dias` dias a partir de `inicio`."""
    datas = [(date.fromisoformat(inicio) + timedelta(days=i)).isoformat() for i in range(dias)]
    return {
        "latitude": -25.5, "longitude": -49.25, "generationtime_ms": 0.1, "utc_offset_seconds": offset,
        "timezone": "America/Sao_Paulo", "timezone_abbreviation": "GMT-3", "elevation": 906.0,
        "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C", "weather_code": "wmo code"},
        "current": {"time": f"{inicio}T18:00", "interval": 900, "temperature_2m": temperatura, "weather_code": codigo},
        "daily_units": {"time": "iso8601", "weather_code": "wmo code", "temperature_2m_max": "°C",
                        "temperature_2m_min": "°C", "precipitation_probability_max": "%"},
        "daily": {"time": datas, "weather_code": CODIGOS[:dias], "temperature_2m_max": MAXIMAS[:dias],
                  "temperature_2m_min": MINIMAS[:dias], "precipitation_probability_max": CHUVA[:dias]},
    }


class OpenMeteoFalso:
    """
    Imita GET /v1/search (geocoding) e GET /v1/forecast. `cidades` liga o `name` pedido aos resultados;
    `previsao` é (status, corpo), com corpo dict (JSON), bytes (texto cru) ou None (derruba a conexão).
    """

    def __init__(self):
        self._trava = threading.Lock()
        self.zerar()
        self._servidor = ServidorLocal(self._criar_handler())
        self.geocoding_url = f"{self._servidor.url}/v1/search"
        self.previsao_url = f"{self._servidor.url}/v1/forecast"

    def zerar(self) -> None:
        with self._trava:
            self.cidades = {"São José dos Pinhais": [SJP]}
            self.geocoding = None  # (status, corpo) para uma falha do geocoding
            self.previsao = (200, resposta_previsao())
            self.pedidos = []

    def pedidos_em(self, caminho: str) -> list[dict]:
        with self._trava:
            return [p["parametros"] for p in self.pedidos if p["caminho"] == caminho]

    def parar(self) -> None:
        self._servidor.parar()

    def _criar_handler(self):
        falso = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                partes = urlsplit(self.path)
                parametros = {nome: valores[0] for nome, valores in parse_qs(partes.query).items()}
                with falso._trava:
                    falso.pedidos.append({"caminho": partes.path, "parametros": parametros})
                    if partes.path == "/v1/search" and falso.geocoding:
                        status, corpo = falso.geocoding
                    elif partes.path == "/v1/search":
                        resultados = falso.cidades.get(parametros.get("name"))
                        corpo = {"generationtime_ms": 0.07}  # sem resultado, "results" nem vem
                        if resultados is not None:
                            corpo["results"] = resultados
                        status = 200
                    elif partes.path == "/v1/forecast":
                        status, corpo = falso.previsao
                    else:
                        status, corpo = 404, {"error": True, "reason": "Not Found"}
                if corpo is None:
                    return  # fecha a conexão sem responder
                dados = corpo if isinstance(corpo, bytes) else json.dumps(corpo, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html" if isinstance(corpo, bytes) else "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            def log_message(self, formato, *args):
                pass

        return Handler


class Relogio:
    """Relógio monotônico falso: só anda quando o teste manda."""

    def __init__(self):
        self.segundos = 1000.0

    def __call__(self) -> float:
        return self.segundos

    def avancar(self, minutos: float = 0, horas: float = 0) -> None:
        self.segundos += minutos * 60 + horas * 3600


class RespostaFalsa:
    def __init__(self, status: int, corpo=None, texto: str = ""):
        self.status_code, self._corpo, self.text = status, corpo, texto

    def json(self):
        if self.status_code != 200:
            raise AssertionError("leu o JSON antes de conferir o status")
        if self._corpo is None:
            raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)
        return self._corpo


class SessaoFalsa:
    """Imita requests.get: guarda os pedidos e devolve, na ordem, as respostas ou exceções dadas."""

    def __init__(self, *respostas):
        self.respostas, self.pedidos = list(respostas), []

    def get(self, url, **kwargs):
        self.pedidos.append((url, kwargs))
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


@PRECISA_REQUESTS
class TestPrevisao(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # requests respeita HTTP(S)_PROXY; os testes só falam com 127.0.0.1
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.falso = OpenMeteoFalso()

    @classmethod
    def tearDownClass(cls):
        cls.falso.parar()
        cls.sem_proxy.stop()

    def setUp(self):
        self.falso.zerar()
        self.relogio = Relogio()
        self.agora = AGORA

    def criar(self, cidade_padrao=None, **extra):
        return tempo.Tempo(cidade_padrao, relogio=self.relogio, agora_utc=lambda: self.agora,
                           geocoding_url=self.falso.geocoding_url, previsao_url=self.falso.previsao_url, **extra)

    def assertErro(self, mensagem: str, funcao, *args, **kwargs) -> "tempo.ErroNoTempo":
        with self.assertRaises(tempo.ErroNoTempo) as erro:
            funcao(*args, **kwargs)
        self.assertEqual(str(erro.exception), mensagem)
        return erro.exception

    # ---------- dias ----------

    def test_hoje_traz_o_agora(self):
        self.assertEqual(self.criar().previsao("São José dos Pinhais"), {
            "cidade": "São José dos Pinhais, Paraná", "dia": "hoje", "data": "2026-09-28",
            "condicao": "garoa fraca", "minima": 18, "maxima": 32, "chance_de_chuva": 6,
            "agora": {"temperatura": 21, "condicao": "nublado"},
            "descricao": "agora faz 21 graus e está nublado em São José dos Pinhais; hoje: garoa fraca, mínima de 18 "
                         "e máxima de 32 graus, 6% de chance de chuva"})

    def test_amanha(self):
        # O exemplo da especificação, com os números da resposta real (18,5 arredonda para 18, como o round).
        self.assertEqual(self.criar().previsao("São José dos Pinhais", 1), {
            "cidade": "São José dos Pinhais, Paraná", "dia": "amanhã", "data": "2026-09-29",
            "condicao": "pancadas de chuva", "minima": 18, "maxima": 33, "chance_de_chuva": 73,
            "descricao": "amanhã em São José dos Pinhais: pancadas de chuva, mínima de 18 e máxima de 33 graus, "
                         "73% de chance de chuva"})

    def test_depois_de_amanha(self):
        resultado = self.criar().previsao("São José dos Pinhais", 2)
        self.assertEqual((resultado["dia"], resultado["data"], resultado["condicao"]),
                         ("depois de amanhã", "2026-09-30", "nublado"))
        self.assertEqual(resultado["descricao"], "depois de amanhã em São José dos Pinhais: nublado, mínima de 16 e "
                                                 "máxima de 27 graus, 20% de chance de chuva")
        self.assertNotIn("agora", resultado)

    def test_dia_da_semana_a_partir_de_3_dias(self):
        clima = self.criar()
        casos = [(3, "quinta-feira, 1º de outubro", "2026-10-01"), (4, "sexta-feira, 2 de outubro", "2026-10-02"),
                 (5, "sábado, 3 de outubro", "2026-10-03"), (6, "domingo, 4 de outubro", "2026-10-04")]
        for dias, dia, data in casos:
            with self.subTest(dias=dias):
                resultado = clima.previsao("São José dos Pinhais", dias)
                self.assertEqual((resultado["dia"], resultado["data"]), (dia, data))
        self.assertEqual(clima.previsao("São José dos Pinhais", 4)["descricao"],
                         "sexta-feira, 2 de outubro, em São José dos Pinhais: trovoada com granizo, mínima de 15 e "
                         "máxima de 28 graus, 100% de chance de chuva")
        self.assertEqual(tempo.nome_do_dia(date(2026, 12, 27), 3), "domingo, 27 de dezembro")
        self.assertEqual(tempo.nome_do_dia(date(2027, 3, 1), 5), "segunda-feira, 1º de março")

    def test_dia_vem_do_fuso_da_cidade(self):
        # 02h30 UTC do dia 29 ainda são 23h30 do dia 28 em Brasília: "hoje" é o dia 28.
        self.agora = datetime(2026, 9, 29, 2, 30, tzinfo=timezone.utc)
        clima = self.criar()
        self.assertEqual(clima.previsao("São José dos Pinhais")["data"], "2026-09-28")
        self.assertEqual(clima.previsao("São José dos Pinhais", 1)["data"], "2026-09-29")

    def test_dia_vem_do_fuso_da_cidade_a_leste(self):
        # 22h30 UTC do dia 28 já são 07h30 do dia 29 em Tóquio (UTC+9): o daily da resposta começa no dia 29.
        self.falso.cidades["Tóquio"] = [{"name": "Tóquio", "latitude": 35.6895, "longitude": 139.69171,
                                          "feature_code": "PPLC", "admin1": "Tóquio"}]
        self.falso.previsao = (200, resposta_previsao(inicio="2026-09-29", offset=9 * 3600))
        self.agora = datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc)
        resultado = self.criar().previsao("Tóquio", 1)
        self.assertEqual((resultado["dia"], resultado["data"]), ("amanhã", "2026-09-30"))

    def test_chance_de_chuva_ausente(self):
        corpo = resposta_previsao()
        corpo["daily"]["precipitation_probability_max"] = [None] * 7
        self.falso.previsao = (200, corpo)
        clima = self.criar()
        resultado = clima.previsao("São José dos Pinhais", 1)
        self.assertIsNone(resultado["chance_de_chuva"])
        self.assertEqual(resultado["descricao"],
                         "amanhã em São José dos Pinhais: pancadas de chuva, mínima de 18 e máxima de 33 graus")
        self.assertTrue(clima.previsao("São José dos Pinhais")["descricao"].endswith("máxima de 32 graus"))

    def test_temperaturas_arredondadas_e_grau_no_singular(self):
        corpo = resposta_previsao(temperatura=0.6, codigo=0)
        corpo["daily"].update(temperature_2m_min=[-2.4] * 7, temperature_2m_max=[1.2] * 7,
                              precipitation_probability_max=[12.0] * 7)
        self.falso.previsao = (200, corpo)
        resultado = self.criar().previsao("São José dos Pinhais")
        self.assertEqual((resultado["minima"], resultado["maxima"], resultado["chance_de_chuva"]), (-2, 1, 12))
        self.assertIsInstance(resultado["chance_de_chuva"], int)
        self.assertEqual(resultado["agora"], {"temperatura": 1, "condicao": "céu limpo"})
        self.assertEqual(resultado["descricao"], "agora faz 1 grau e está com céu limpo em São José dos Pinhais; "
                                                 "hoje: garoa fraca, mínima de -2 e máxima de 1 grau, "
                                                 "12% de chance de chuva")

    def test_tabela_wmo(self):
        oficiais = {0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86,
                    95, 96, 97, 99}
        self.assertEqual(set(tempo.WMO_PT), oficiais)
        self.assertEqual(tempo.condicao(96), "trovoada com granizo")
        self.assertEqual(tempo.condicao(97), "trovoada forte")
        self.assertEqual(tempo.condicao(3.0), "nublado")
        for desconhecido in (4, 98, -1, 3.5, None, "3", True, float("nan")):
            with self.subTest(codigo=desconhecido):
                self.assertEqual(tempo.condicao(desconhecido), "tempo instável")

    def test_codigo_desconhecido_na_resposta(self):
        corpo = resposta_previsao(codigo=42)
        corpo["daily"]["weather_code"] = [None] * 7
        self.falso.previsao = (200, corpo)
        resultado = self.criar().previsao("São José dos Pinhais")
        self.assertEqual((resultado["condicao"], resultado["agora"]["condicao"]), ("tempo instável", "tempo instável"))
        self.assertIn("e está com tempo instável em", resultado["descricao"])

    def test_sem_temperatura_atual_fica_so_a_previsao_do_dia(self):
        self.falso.previsao = (200, resposta_previsao(temperatura=None))
        resultado = self.criar().previsao("São José dos Pinhais")
        self.assertNotIn("agora", resultado)
        self.assertEqual(resultado["descricao"], "hoje em São José dos Pinhais: garoa fraca, mínima de 18 e máxima "
                                                 "de 32 graus, 6% de chance de chuva")

    def test_sem_temperatura_do_dia(self):
        corpo = resposta_previsao()
        corpo["daily"]["temperature_2m_max"][1] = None
        self.falso.previsao = (200, corpo)
        clima = self.criar()
        erro = self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao,
                               "São José dos Pinhais", 1)
        self.assertIn("2026-09-29", erro.detalhe)
        self.assertEqual(clima.previsao("São José dos Pinhais", 2)["maxima"], 27)  # os outros dias continuam

    # ---------- faixa de dias e cidade ----------

    def test_dias_fora_da_faixa(self):
        clima = self.criar()
        for dias in (-1, 7, 30, True, "abc", 1.5, [], float("inf")):
            with self.subTest(dias=dias):
                self.assertErro("só consigo a previsão de hoje até daqui a 6 dias", clima.previsao,
                                "São José dos Pinhais", dias)
        self.assertEqual(self.falso.pedidos, [])  # recusou antes de consultar

    def test_dias_em_outros_formatos(self):
        clima = self.criar()
        self.assertEqual(clima.previsao("São José dos Pinhais", "2")["data"], "2026-09-30")
        self.assertEqual(clima.previsao("São José dos Pinhais", 1.0)["data"], "2026-09-29")
        self.assertEqual(clima.previsao("São José dos Pinhais", None)["dia"], "hoje")
        self.assertEqual(clima.previsao("São José dos Pinhais", "  ")["dia"], "hoje")  # "" do LLM, como a cidade

    def test_numero_enorme_do_llm_da_o_erro_de_faixa(self):
        # O JSON aceita um inteiro de 400 dígitos, e o math.isfinite dele daria OverflowError.
        enorme = json.loads("1" + "0" * 400)
        self.assertErro("só consigo a previsão de hoje até daqui a 6 dias", self.criar().previsao,
                        "São José dos Pinhais", enorme)
        self.assertEqual(tempo.numero(enorme), enorme)
        self.assertEqual(tempo.condicao(enorme), "tempo instável")

    def test_data_que_nao_esta_na_resposta(self):
        self.falso.previsao = (200, resposta_previsao(dias=2))
        clima = self.criar()
        self.assertEqual(clima.previsao("São José dos Pinhais", 1)["dia"], "amanhã")
        self.assertErro("só consigo a previsão de hoje até daqui a 6 dias", clima.previsao, "São José dos Pinhais", 3)

    def test_sem_cidade(self):
        for clima in (self.criar(), self.criar(""), self.criar("   ")):
            for cidade in (None, "", "   "):
                with self.subTest(padrao=clima.cidade_padrao, cidade=cidade):
                    self.assertErro('diga a cidade, ou preencha "cidade" no config_servidor.json', clima.previsao,
                                    cidade)
        self.assertEqual(self.falso.pedidos, [])

    def test_cidade_padrao(self):
        self.falso.cidades["Curitiba"] = [{"name": "Curitiba", "latitude": -25.42778, "longitude": -49.27306,
                                           "feature_code": "PPLA", "admin1": "Paraná"}]
        clima = self.criar("  São José dos Pinhais ")
        self.assertEqual(clima.previsao()["cidade"], "São José dos Pinhais, Paraná")
        self.assertEqual(clima.previsao("  ")["cidade"], "São José dos Pinhais, Paraná")
        self.assertEqual(clima.previsao("Curitiba")["cidade"], "Curitiba, Paraná")  # a do pedido vale mais
        self.assertEqual([p["name"] for p in self.falso.pedidos_em("/v1/search")],
                         ["São José dos Pinhais", "Curitiba"])

    # ---------- escolha da cidade ----------

    def test_descarta_o_bairro_pplx(self):
        # "Bom Jesus, RS" de verdade: Triunfo em 1º (nome alternativo) e um bairro de Porto Alegre em 2º.
        self.falso.cidades["Bom Jesus, RS"] = [
            {"name": "Triunfo", "latitude": -29.94, "longitude": -51.72, "feature_code": "PPLA2",
             "admin1": "Rio Grande do Sul"},
            {"name": "Bom Jesus", "latitude": -30.04, "longitude": -51.16, "feature_code": "PPLX",
             "admin1": "Rio Grande do Sul"},
            {"name": "Bom Jesus", "latitude": -28.66972, "longitude": -50.42972, "feature_code": "PPLA2",
             "admin1": "Rio Grande do Sul", "population": 11519},
        ]
        resultado = self.criar().previsao("Bom Jesus, RS")
        self.assertEqual(resultado["cidade"], "Bom Jesus, Rio Grande do Sul")
        self.assertTrue(resultado["descricao"].startswith("agora faz 21 graus e está nublado em Bom Jesus; hoje:"))
        previsao = self.falso.pedidos_em("/v1/forecast")[0]
        self.assertEqual((previsao["latitude"], previsao["longitude"]), ("-28.66972", "-50.42972"))

    def test_nome_sem_acento_e_sem_maiusculas(self):
        self.falso.cidades["sao jose dos pinhais"] = [
            {"name": "São José", "latitude": -27.61, "longitude": -48.63, "feature_code": "PPLA2",
             "admin1": "Santa Catarina"},
            {**SJP, "name": "São José Dos Pinhais"},
        ]
        resultado = self.criar().previsao("sao jose dos pinhais")
        self.assertEqual(resultado["cidade"], "São José Dos Pinhais, Paraná")
        self.assertEqual(self.falso.pedidos_em("/v1/forecast")[0]["latitude"], str(SJP["latitude"]))

    def test_sem_nome_igual_fica_o_primeiro(self):
        # A busca por prefixo também olha nomes alternativos: "Bom Jesus" trouxe Crisópolis e Tuparetama.
        self.falso.cidades["Bom Jesus"] = [
            {"name": "Crisópolis", "latitude": -11.5, "longitude": -38.15, "feature_code": "PPLA2"},
            {"name": "Tuparetama", "latitude": -7.6, "longitude": -37.31, "feature_code": "PPLA2",
             "admin1": "Pernambuco"},
        ]
        self.assertEqual(self.criar().previsao("Bom Jesus")["cidade"], "Crisópolis")  # sem admin1, só o nome

    def test_nome_igual_mas_so_bairro_fica_o_primeiro(self):
        self.falso.cidades["Centro"] = [
            {"name": "Centro Novo", "latitude": -2.0, "longitude": -46.0, "feature_code": "PPLA2",
             "admin1": "Maranhão"},
            {"name": "Centro", "latitude": -25.43, "longitude": -49.27, "feature_code": "PPLX", "admin1": "Paraná"},
            {"name": "Centro", "latitude": -8.0, "longitude": -35.0},  # sem feature_code: campos vazios não vêm
        ]
        self.assertEqual(self.criar().previsao("Centro")["cidade"], "Centro Novo, Maranhão")

    def test_cidade_nao_encontrada(self):
        clima = self.criar()
        self.assertErro("não encontrei a cidade Xyzqwkk", clima.previsao, "  Xyzqwkk ")
        self.falso.cidades["Lugar Nenhum"] = []
        self.assertErro("não encontrei a cidade Lugar Nenhum", clima.previsao, "Lugar Nenhum")
        self.falso.cidades["Sem Coordenadas"] = [{"name": "Sem Coordenadas", "feature_code": "PPL"}]
        self.assertErro("não encontrei a cidade Sem Coordenadas", clima.previsao, "Sem Coordenadas")
        self.assertErro("não encontrei a cidade x", clima.previsao, "x")  # o geocoding pede 2 letras
        self.assertEqual(self.falso.pedidos_em("/v1/forecast"), [])
        self.assertEqual(len(self.falso.pedidos_em("/v1/search")), 3)

    def test_falha_no_geocoding(self):
        clima = self.criar()
        self.falso.geocoding = (400, {"error": True, "reason": "Parameter count must be between 1 and 100."})
        erro = self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao,
                               "São José dos Pinhais")
        self.assertIn("400", erro.detalhe)
        self.falso.geocoding = (200, ["não", "é", "objeto"])
        self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao, "São José dos Pinhais")
        self.falso.geocoding = (200, b"<html>\n<body>Bad Gateway</body>\n</html>")
        erro = self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao,
                               "São José dos Pinhais")
        self.assertIn("invalid JSON", erro.detalhe)
        self.assertNotIn("\n", erro.detalhe)  # uma linha só no terminal
        self.falso.geocoding = None  # a falha não fica guardada
        self.assertEqual(clima.previsao("São José dos Pinhais")["cidade"], "São José dos Pinhais, Paraná")

    def test_cidade_nao_encontrada_nao_fica_guardada(self):
        clima = self.criar()
        self.assertErro("não encontrei a cidade Curitiba", clima.previsao, "Curitiba")
        self.falso.cidades["Curitiba"] = [{"name": "Curitiba", "latitude": -25.42778, "longitude": -49.27306,
                                           "feature_code": "PPLA", "admin1": "Paraná"}]
        self.assertEqual(clima.previsao("Curitiba")["cidade"], "Curitiba, Paraná")
        self.assertEqual(len(self.falso.pedidos_em("/v1/search")), 2)

    def test_resultados_nulos_ou_incompletos_sao_ignorados(self):
        self.falso.cidades["São José dos Pinhais"] = [None, "texto", {"name": "", "latitude": 1, "longitude": 2},
                                                      {"name": "Sem Latitude", "longitude": 2}, SJP]
        self.assertEqual(self.criar().previsao("São José dos Pinhais")["cidade"], "São José dos Pinhais, Paraná")

    def test_cidade_fica_guardada(self):
        clima = self.criar()
        clima.previsao("São José dos Pinhais")
        self.relogio.avancar(horas=48)
        clima.previsao("sao jose  dos PINHAIS")  # mesma cidade normalizada
        self.assertEqual(len(self.falso.pedidos_em("/v1/search")), 1)
        self.assertEqual(len(self.falso.pedidos_em("/v1/forecast")), 2)

    def test_cidade_com_uf_normalizada(self):
        self.falso.cidades["Bom Jesus, RS"] = [{"name": "Bom Jesus", "latitude": -28.67, "longitude": -50.43,
                                                "feature_code": "PPLA2", "admin1": "Rio Grande do Sul"}]
        clima = self.criar()
        clima.previsao("Bom Jesus, RS")
        clima.previsao("bom jesus ,rs")
        self.assertEqual([p["name"] for p in self.falso.pedidos_em("/v1/search")], ["Bom Jesus, RS"])

    # ---------- parâmetros ----------

    def test_parametros_enviados(self):
        self.criar().previsao("São José dos Pinhais")
        self.assertEqual(self.falso.pedidos_em("/v1/search"),
                         [{"name": "São José dos Pinhais", "count": "5", "language": "pt", "format": "json"}])
        self.assertEqual(self.falso.pedidos_em("/v1/forecast"), [{
            "latitude": "-25.53472", "longitude": "-49.20583", "current": "temperature_2m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": "7"}])

    def test_tempo_limite_de_5_segundos(self):
        sessao = SessaoFalsa(RespostaFalsa(200, {"results": [SJP]}), RespostaFalsa(200, resposta_previsao()))
        clima = tempo.Tempo(sessao=sessao, relogio=self.relogio, agora_utc=lambda: self.agora)
        clima.previsao("São José dos Pinhais")
        self.assertEqual([url for url, _ in sessao.pedidos], [tempo.GEOCODING_URL, tempo.PREVISAO_URL])
        self.assertEqual([kwargs["timeout"] for _, kwargs in sessao.pedidos], [5, 5])

    def test_urls_padrao(self):
        self.assertEqual(tempo.GEOCODING_URL, "https://geocoding-api.open-meteo.com/v1/search")
        self.assertEqual(tempo.PREVISAO_URL, "https://api.open-meteo.com/v1/forecast")

    # ---------- cache e falhas ----------

    def test_previsao_fica_guardada_por_30_minutos(self):
        clima = self.criar()
        primeira = clima.previsao("São José dos Pinhais")
        self.relogio.avancar(minutos=29)
        self.assertEqual(clima.previsao("São José dos Pinhais"), primeira)
        clima.previsao("São José dos Pinhais", 3)
        self.assertEqual(len(self.falso.pedidos_em("/v1/forecast")), 1)
        self.relogio.avancar(minutos=2)
        clima.previsao("São José dos Pinhais")
        self.assertEqual(len(self.falso.pedidos_em("/v1/forecast")), 2)

    def test_previsao_guardada_de_ontem_consulta_de_novo(self):
        # Guardada às 23h50, perto da meia-noite; 10 minutos depois o daily dela começa em "ontem".
        self.agora = datetime(2026, 9, 29, 2, 50, tzinfo=timezone.utc)
        clima = self.criar()
        clima.previsao("São José dos Pinhais", 6)
        self.falso.previsao = (200, resposta_previsao(inicio="2026-09-29"))
        self.agora += timedelta(minutes=20)
        self.relogio.avancar(minutos=20)
        self.assertEqual(clima.previsao("São José dos Pinhais", 6)["data"], "2026-10-05")
        self.assertEqual(len(self.falso.pedidos_em("/v1/forecast")), 2)

    def test_falhas_usam_a_copia_de_ate_6_horas(self):
        corpo_sem_daily = resposta_previsao()
        del corpo_sem_daily["daily"]
        corpo_curto = resposta_previsao()
        corpo_curto["daily"]["temperature_2m_min"] = [17.9]
        falhas = {"429": (429, {"error": True, "reason": "Minutely API request limit exceeded. Please try again in "
                                                         "one minute."}),
                  "503 em HTML": (503, b"<html><body>Service Unavailable</body></html>"),
                  "JSON inválido": (200, b"{\"daily\": "),
                  "sem daily": (200, corpo_sem_daily),
                  "lista com tamanho errado": (200, corpo_curto),
                  "conexão derrubada": (200, None)}
        for nome, falha in falhas.items():
            with self.subTest(falha=nome):
                self.falso.zerar()
                clima = self.criar()
                hoje = clima.previsao("São José dos Pinhais")
                amanha = clima.previsao("São José dos Pinhais", 1)
                self.falso.previsao = falha
                self.relogio.avancar(horas=2)
                with contextlib.redirect_stderr(io.StringIO()) as saida:
                    self.assertEqual(clima.previsao("São José dos Pinhais", 1), amanha)
                    copia = clima.previsao("São José dos Pinhais")
                self.assertIn("usei a de 120 minutos atrás", saida.getvalue())
                self.assertEqual(len(self.falso.pedidos_em("/v1/forecast")), 3)  # tentou de novo a cada pedido
                # A temperatura de 2 horas atrás não é mais a de "agora": fica só a previsão do dia.
                sem_agora = {chave: valor for chave, valor in hoje.items() if chave != "agora"}
                self.assertEqual(copia, {**sem_agora, "descricao": "hoje em São José dos Pinhais: garoa fraca, "
                                         "mínima de 18 e máxima de 32 graus, 6% de chance de chuva"})

    def test_copia_com_mais_de_6_horas_nao_vale(self):
        clima = self.criar()
        amanha = clima.previsao("São José dos Pinhais", 1)
        self.falso.previsao = (503, {"error": True, "reason": "The service is overloaded"})
        self.relogio.avancar(horas=6)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(clima.previsao("São José dos Pinhais", 1), amanha)  # 6 horas ainda vale
        self.relogio.avancar(minutos=1)
        erro = self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao,
                               "São José dos Pinhais", 1)
        self.assertIn("overloaded", erro.detalhe)

    def test_fuso_absurdo_conta_como_falha(self):
        # Um utc_offset_seconds fora da faixa faria a conta do dia dar OverflowError, e ele ficaria guardado.
        clima = self.criar()
        clima.previsao("São José dos Pinhais")
        for deslocamento in (10 ** 13, -19 * 3600, float("nan"), "-10800", None):
            with self.subTest(deslocamento=deslocamento):
                self.assertIsNotNone(tempo.problema_na_previsao({**resposta_previsao(),
                                                                 "utc_offset_seconds": deslocamento}))
        self.assertIsNone(tempo.problema_na_previsao(resposta_previsao(offset=14 * 3600)))
        self.falso.previsao = (200, {**resposta_previsao(), "utc_offset_seconds": 10 ** 13})
        self.relogio.avancar(minutos=31)
        with contextlib.redirect_stderr(io.StringIO()) as saida:
            self.assertEqual(clima.previsao("São José dos Pinhais", 1)["data"], "2026-09-29")  # usou a cópia
        self.assertIn("out of range", saida.getvalue())
        self.assertErro("não consegui consultar a previsão do tempo agora", self.criar().previsao,
                        "São José dos Pinhais")

    def test_previsoes_velhas_saem_do_cache(self):
        self.falso.cidades["Curitiba"] = [{"name": "Curitiba", "latitude": -25.42778, "longitude": -49.27306,
                                           "feature_code": "PPLA", "admin1": "Paraná"}]
        clima = self.criar()
        clima.previsao("São José dos Pinhais")
        self.relogio.avancar(horas=6, minutos=1)
        clima.previsao("Curitiba")
        self.assertEqual(list(clima._previsoes), [(-25.42778, -49.27306)])

    def test_copia_sem_o_dia_pedido_nao_vale(self):
        # Cópia de 23h (Brasília); às 3h do dia seguinte o "daqui a 6 dias" não está nela.
        self.agora = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
        clima = self.criar()
        clima.previsao("São José dos Pinhais")
        self.falso.previsao = (503, b"Service Unavailable")
        self.agora += timedelta(hours=4)
        self.relogio.avancar(horas=4)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(clima.previsao("São José dos Pinhais", 5)["data"], "2026-10-04")
        self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao, "São José dos Pinhais", 6)

    def test_429_sem_copia(self):
        self.falso.previsao = (429, {"error": True, "reason": "Minutely API request limit exceeded. Please try again "
                                                              "in one minute."})
        erro = self.assertErro("não consegui consultar a previsão do tempo agora", self.criar().previsao,
                               "São José dos Pinhais")
        self.assertIn("429", erro.detalhe)
        self.assertIn("Minutely API request limit exceeded", erro.detalhe)

    def test_status_conferido_antes_do_json(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                sessao = SessaoFalsa(RespostaFalsa(200, {"results": [SJP]}),
                                     RespostaFalsa(status, texto="<html>Bad Gateway</html>"))
                clima = tempo.Tempo(sessao=sessao, relogio=self.relogio, agora_utc=lambda: self.agora)
                erro = self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao,
                                       "São José dos Pinhais")
                self.assertIn(str(status), erro.detalhe)

    def test_erros_de_rede_sem_copia(self):
        for excecao in (requests.Timeout("read timed out"), requests.ConnectionError("connection refused")):
            with self.subTest(excecao=type(excecao).__name__):
                sessao = SessaoFalsa(RespostaFalsa(200, {"results": [SJP]}), excecao)
                clima = tempo.Tempo(sessao=sessao, relogio=self.relogio, agora_utc=lambda: self.agora)
                erro = self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao,
                                       "São José dos Pinhais")
                self.assertIn(type(excecao).__name__, erro.detalhe)
        sessao = SessaoFalsa(requests.ConnectionError("no route to host"))
        clima = tempo.Tempo(sessao=sessao, relogio=self.relogio, agora_utc=lambda: self.agora)
        self.assertErro("não consegui consultar a previsão do tempo agora", clima.previsao, "Curitiba")

    def test_erro_tem_mensagem_e_detalhe(self):
        erro = tempo.ErroNoTempo("não consegui consultar a previsão do tempo agora", "HTTP 503")
        self.assertEqual((str(erro), erro.detalhe), ("não consegui consultar a previsão do tempo agora", "HTTP 503"))
        self.assertEqual(tempo.ErroNoTempo("só a mensagem").detalhe, "")

    def test_pedidos_ao_mesmo_tempo(self):
        clima = self.criar()
        resultados, erros = [], []

        def pedir(dias):
            try:
                resultados.append((dias, clima.previsao("São José dos Pinhais", dias)))
            except Exception as e:  # noqa: BLE001 - o teste confere que nada falhou
                erros.append(e)

        threads = [threading.Thread(target=pedir, args=(i % 7,)) for i in range(14)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(erros, [])
        self.assertEqual(len(resultados), 14)
        self.assertEqual({dias: r["data"] for dias, r in resultados},
                         {i: (date(2026, 9, 28) + timedelta(days=i)).isoformat() for i in range(7)})
        clima.previsao("São José dos Pinhais")  # depois disso, tudo vem do cache
        antes = len(self.falso.pedidos)
        clima.previsao("São José dos Pinhais", 2)
        self.assertEqual(len(self.falso.pedidos), antes)


if __name__ == "__main__":
    unittest.main()
