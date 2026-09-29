"""
Testes das ferramentas da v0.2 no servidor: fechar programas, volume, bloquear e desligar o PC (com a
confirmação por voz), Wake-on-LAN, previsão do tempo e Spotify. O agente roda de verdade em 127.0.0.1, mas
as funções que mexem no PC (sistema.py) são trocadas por mocks: nenhum teste muda o volume, bloqueia a
tela ou desliga nada. O Open-Meteo e o Spotify também são trocados por objetos de teste.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from tests.auxiliares import (LLMFalso, ServidorLocal, comando_de_teste, esperar_arquivo, iniciar_agente,
                              resposta_ferramenta, resposta_texto, vigiar_popen)
from tests.test_servidor import (PRECISA_DEPENDENCIAS, TOKEN, carregar_servidor, ferramenta_do_pedido,
                                 nomes_das_ferramentas)

PROGRAMA = "programa de teste"
FUNCOES_DO_SISTEMA = ("volume_ler", "volume_definir", "volume_mudo", "fechar", "bloquear", "desligar",
                      "cancelar_desligamento")


class TempoDeMentira:
    def __init__(self, erro=None):
        self.erro, self.pedidos = erro, []

    def previsao(self, cidade=None, dias_a_frente=0):
        self.pedidos.append((cidade, dias_a_frente))
        if self.erro:
            raise self.erro
        return {"cidade": "Curitiba, Paraná", "dia": "amanhã", "data": "2026-09-30", "condicao": "chuva",
                "minima": 12, "maxima": 20, "chance_de_chuva": 80,
                "descricao": "amanhã em Curitiba: chuva, mínima de 12 e máxima de 20 graus, 80% de chance de chuva"}


class SpotifyDeMentira:
    """Imita spotify.Spotify: levanta os erros da fila `erros`, na ordem, e depois toca."""

    def __init__(self, modulo, erros=(), conectado=True):
        self.modulo, self.erros, self._conectado, self.pedidos = modulo, list(erros), conectado, []

    def conectado(self):
        return self._conectado

    def tocar(self, busca, tipo="musica"):
        self.pedidos.append(("tocar", busca, tipo))
        if self.erros:
            raise self.erros.pop(0)
        return f'coloquei "{busca}", de Legião Urbana'

    def controlar(self, acao):
        self.pedidos.append(("controlar", acao))
        if self.erros:
            raise self.erros.pop(0)
        return "pausei a música"


@PRECISA_DEPENDENCIAS
class TestFerramentasDoPC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        pasta = Path(cls.tmp.name)
        cls.marcador = pasta / "programa-aberto.txt"
        cls.marcador_spotify = pasta / "spotify-aberto.txt"
        cls.agente, cls.servidor_agente = iniciar_agente(
            pasta / "agente", TOKEN,
            {PROGRAMA: comando_de_teste(cls.marcador), "spotify": comando_de_teste(cls.marcador_spotify)},
            fechar={PROGRAMA: "teste.exe"}, acoes=["volume", "bloquear", "desligar"])
        cls.llm = LLMFalso()
        cls.servidor = carregar_servidor(pasta / "servidor", llm_base_url=cls.llm.url,
                                         pc_url=cls.servidor_agente.url, cidade="Curitiba, PR")

    @classmethod
    def tearDownClass(cls):
        cls.llm.parar()
        cls.servidor_agente.parar()
        cls.tmp.cleanup()
        cls.sem_proxy.stop()

    def setUp(self):
        self.llm.zerar()
        for marcador in (self.marcador, self.marcador_spotify):
            marcador.unlink(missing_ok=True)
        self.cliente = self.servidor.app.test_client()
        self.popen = vigiar_popen(self, self.agente)
        # Nada aqui pode mexer no PC de verdade: cada função do sistema vira um mock com um padrão inofensivo.
        self.sistema = mock.Mock()
        self.sistema.volume_ler.return_value = (40, False)
        self.sistema.fechar.return_value = "fechou"
        self.sistema.desligar.return_value = "30 segundos"
        self.sistema.cancelar_desligamento.return_value = True
        for nome in FUNCOES_DO_SISTEMA:
            patcher = mock.patch.object(self.agente.sistema, nome, getattr(self.sistema, nome))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.servidor._pendentes.clear()  # nenhuma pergunta de outro teste fica valendo
        self.servidor._memorias.clear()
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.terminal = pilha.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def enviar(self, texto: str) -> dict:
        r = self.cliente.post("/texto", json={"texto": texto})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def trocar(self, nome: str, valor):
        patcher = mock.patch.object(self.servidor, nome, valor)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------- ferramentas oferecidas ----------

    def test_ferramentas_seguem_o_que_o_agente_libera(self):
        self.llm.programar(resposta_texto("Oi."))
        self.enviar("oi")
        pedido = self.llm.pedidos_de_chat()[0]["json"]
        self.assertEqual(nomes_das_ferramentas(pedido), ["abrir_programa", "fechar_programa", "volume_do_pc",
                                                         "energia_do_pc", "previsao_do_tempo"])
        self.assertEqual(ferramenta_do_pedido(pedido, "fechar_programa")["parameters"]["properties"]["nome"]["enum"],
                         [PROGRAMA])
        self.assertEqual(ferramenta_do_pedido(pedido, "energia_do_pc")["parameters"]["properties"]["acao"]["enum"],
                         ["bloquear", "desligar", "cancelar"])
        tempo = ferramenta_do_pedido(pedido, "previsao_do_tempo")["parameters"]["properties"]
        self.assertIn("vale Curitiba, PR", tempo["cidade"]["description"])

    def test_agente_antigo_so_com_programas(self):
        class AgenteAntigo(BaseHTTPRequestHandler):
            def do_GET(self):
                corpo = json.dumps({"programas": ["chrome"]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, *args):
                pass

        antigo = ServidorLocal(AgenteAntigo)
        self.addCleanup(antigo.parar)
        self.trocar("PC_URL", antigo.url)
        self.assertEqual(self.servidor.consultar_pc(),
                         {"programas": ["chrome"], "fechaveis": [], "acoes": [], "fora_do_ar": False})

    # ---------- fechar e volume ----------

    def test_fechar_programa(self):
        self.llm.programar(resposta_ferramenta("c1", "fechar_programa", {"nome": PROGRAMA}),
                           resposta_texto("Fechei."))
        dados = self.enviar("fecha o programa de teste")
        self.assertEqual(dados["acoes"][0]["resultado"],
                         {"ok": True, "programa": PROGRAMA, "descricao": f"fechei {PROGRAMA}"})
        self.sistema.fechar.assert_called_once_with(["teste.exe"])

    def test_volume(self):
        self.llm.programar(resposta_ferramenta("c1", "volume_do_pc", {"acao": "definir", "nivel": 35}),
                           resposta_texto("Pronto."))
        dados = self.enviar("volume em 35")
        self.assertEqual(dados["acoes"][0]["resultado"]["descricao"], "deixei o volume em 35%")
        self.sistema.volume_definir.assert_called_once_with(35)

    def test_bloquear_nao_pede_confirmacao(self):
        self.llm.programar(resposta_ferramenta("c1", "energia_do_pc", {"acao": "bloquear"}),
                           resposta_texto("Bloqueei."))
        dados = self.enviar("bloqueia o pc")
        self.assertEqual(dados["acoes"][0]["resultado"]["descricao"], "bloqueei a tela")
        self.sistema.bloquear.assert_called_once_with()

    # ---------- ações que dão certo dispensam a 2ª rodada ----------

    def test_acao_que_deu_certo_responde_sem_outra_rodada(self):
        self.llm.programar(resposta_ferramenta("c1", "abrir_programa", {"nome": PROGRAMA}),
                           resposta_ferramenta("c2", "volume_do_pc", {"acao": "definir", "nivel": 30}),
                           resposta_texto("não deveria ser usado"))
        dados = self.enviar("abre o programa de teste")
        self.assertEqual(dados["resposta"], f"Pronto, abri {PROGRAMA}.")
        self.assertEqual(len(self.llm.pedidos_de_chat()), 1)
        self.llm.zerar()
        self.llm.programar(resposta_ferramenta("c1", "volume_do_pc", {"acao": "definir", "nivel": 30}))
        self.assertEqual(self.enviar("volume em 30")["resposta"], "Pronto, deixei o volume em 30%.")

    def test_consultas_e_erros_voltam_ao_llm(self):
        self.trocar("TEMPO", TempoDeMentira())
        casos = [
            ("qual o volume?", resposta_ferramenta("c1", "volume_do_pc", {"acao": "consultar"})),
            ("vai chover?", resposta_ferramenta("c1", "previsao_do_tempo", {})),
            ("fecha o paint", resposta_ferramenta("c1", "fechar_programa", {"nome": "paint"})),  # erro do agente
            ("abre e diz o tempo", {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "abrir_programa",
                                                              "arguments": f'{{"nome": "{PROGRAMA}"}}'}},
                {"id": "c2", "type": "function", "function": {"name": "previsao_do_tempo", "arguments": "{}"}}]}}]}),
        ]
        for pedido, chamada in casos:
            with self.subTest(pedido=pedido):
                self.llm.zerar()
                self.llm.programar(chamada, resposta_texto("Resposta do LLM."))
                self.assertEqual(self.enviar(pedido)["resposta"], "Resposta do LLM.")
                self.assertEqual(len(self.llm.pedidos_de_chat()), 2)

    def test_linha_de_tempos_no_terminal(self):
        self.llm.programar(resposta_texto("Oi."))
        with contextlib.redirect_stdout(io.StringIO()) as saida:
            self.enviar("oi")
        self.assertRegex(saida.getvalue(), r"\[servidor\] tempos: LLM \d+,\d s em 1 rodada · total \d+,\d s")

    # ---------- desligar com confirmação ----------

    def pedir_para_desligar(self) -> dict:
        # O LLM falso ainda tem uma resposta que troca a pergunta: ela não pode chegar ao usuário.
        self.llm.programar(resposta_ferramenta("c1", "energia_do_pc", {"acao": "desligar"}),
                           resposta_texto("Prefere que eu só bloqueie a tela?"))
        dados = self.enviar("desliga o computador")
        self.assertEqual(dados["acoes"][0]["resultado"],
                         {"confirmar": True, "pergunta": "Quer mesmo desligar o PC? Diga sim para confirmar."})
        self.assertEqual(dados["resposta"], "Quer mesmo desligar o PC? Diga sim para confirmar.")
        self.assertEqual(len(self.llm.pedidos_de_chat()), 1, "a pergunta sai sem outra rodada do LLM")
        self.sistema.desligar.assert_not_called()  # a ferramenta só pergunta
        self.llm.zerar()
        return dados

    def test_desligar_so_depois_do_sim(self):
        self.pedir_para_desligar()
        pedidos_ao_llm = len(self.llm.pedidos_de_chat())
        dados = self.enviar("Sim, pode desligar.")
        self.assertEqual(dados["resposta"],
                         "Tudo bem, o PC vai desligar em 30 segundos; para cancelar, diga cancela o desligamento.")
        self.assertEqual(dados["acoes"][0]["ferramenta"], "energia_do_pc")
        self.sistema.desligar.assert_called_once_with()
        self.assertEqual(len(self.llm.pedidos_de_chat()), pedidos_ao_llm, "o sim não passa pelo LLM")
        # A pergunta vale uma vez só: outro "sim" já não desliga de novo.
        self.llm.programar(resposta_texto("Sim o quê?"))
        self.enviar("sim")
        self.sistema.desligar.assert_called_once_with()

    def test_nao_cancela_o_desligamento(self):
        self.pedir_para_desligar()
        dados = self.enviar("não, deixa")
        self.assertEqual(dados, {"transcricao": "não, deixa", "resposta": "Tudo bem, não vou desligar o PC.",
                                 "acoes": []})
        self.sistema.desligar.assert_not_called()

    def test_outro_pedido_descarta_a_pergunta(self):
        self.pedir_para_desligar()
        self.llm.programar(resposta_texto("Abri."), resposta_texto("Sim o quê?"))
        self.enviar("abre o programa de teste")
        self.enviar("sim")
        self.sistema.desligar.assert_not_called()

    def test_pergunta_vencida_nao_desliga(self):
        self.trocar("CONFIRMACAO_SEGUNDOS", -1)
        self.pedir_para_desligar()
        self.llm.programar(resposta_texto("Sim o quê?"))
        self.enviar("sim")
        self.sistema.desligar.assert_not_called()

    def test_o_que_conta_como_sim_e_como_nao(self):
        casos = {"Sim.": True, "SIM!": True, "sim, pode desligar o computador": True, "Pode.": True,
                 "confirmo": True, "Não.": False, "não, deixa": False, "cancela": False,
                 "não, obrigado": False, "deixa pra lá": False, "Tá bom.": True, "ok": True, "beleza": True,
                 "claro que sim": True, "isso mesmo": True, "sim, obrigado": True, "desliga o PC": True,
                 "desliga a luz": None,
                 "sim, e abre o chrome": None, "abre o chrome": None, "": None, "pode abrir o spotify": None,
                 "deixa a luz azul": None, "deixa o volume em 20": None, "espera, qual a previsão para amanhã?": None,
                 "sim, não tem problema": None}
        for frase, esperado in casos.items():
            with self.subTest(frase=frase):
                self.assertIs(self.servidor.resposta_de_confirmacao(frase), esperado)

    def test_desligar_nao_liberado_no_agente(self):
        pc = {"programas": [PROGRAMA], "fechaveis": [], "acoes": ["volume"], "fora_do_ar": False}
        self.assertEqual(self.servidor.energia_do_pc({"acao": "desligar"}, pc),
                         {"erro": "desligar o PC não está liberado no config_agente.json do PC"})
        self.assertIsNone(self.servidor.tirar_pendente())

    def test_resumo_com_pergunta(self):
        acoes = [{"ferramenta": "fechar_programa", "argumentos": {},
                  "resultado": {"ok": True, "programa": "chrome", "descricao": "fechei chrome"}},
                 {"ferramenta": "energia_do_pc", "argumentos": {},
                  "resultado": {"confirmar": True, "pergunta": "Quer mesmo desligar o PC? Diga sim para confirmar."}}]
        self.assertEqual(self.servidor.resumir(acoes),
                         "Pronto, fechei chrome. Quer mesmo desligar o PC? Diga sim para confirmar.")

    # ---------- Wake-on-LAN ----------

    def test_pacote_magico(self):
        for mac in ("AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF"):
            with self.subTest(mac=mac):
                pacote = self.servidor.pacote_magico(mac)
                self.assertEqual(len(pacote), 102)
                self.assertEqual(pacote, b"\xff" * 6 + bytes.fromhex("AABBCCDDEEFF") * 16)

    def test_liga_o_pc_e_depois_abre_o_programa(self):
        self.trocar("PC_MAC", "AA:BB:CC:DD:EE:FF")
        self.trocar("PC_URL", "http://127.0.0.1:9")
        self.trocar("INTERVALO_WOL", 0.01)
        enviados = []

        def ligar():  # o "boot": depois do pacote mágico, o agente passa a responder
            enviados.append(True)
            self.servidor.PC_URL = self.servidor_agente.url

        self.trocar("enviar_wol", ligar)
        self.llm.programar(resposta_ferramenta("c1", "ligar_pc", {}),
                           resposta_ferramenta("c2", "abrir_programa", {"nome": PROGRAMA}),
                           resposta_texto("Liguei o PC e abri o programa."))
        dados = self.enviar("abre o programa de teste")
        self.assertEqual([a["resultado"].get("descricao") for a in dados["acoes"]], ["liguei o PC", None])
        self.assertTrue(dados["acoes"][1]["resultado"]["ok"])
        self.assertTrue(esperar_arquivo(self.marcador))
        self.assertEqual(enviados, [True])
        primeiro, segundo = (p["json"] for p in self.llm.pedidos_de_chat()[:2])
        self.assertIn("ligar_pc", nomes_das_ferramentas(primeiro))
        self.assertIn("ligue-o antes com a ferramenta ligar_pc", primeiro["messages"][0]["content"])
        self.assertIn("abrir_programa", nomes_das_ferramentas(segundo))  # as ferramentas foram refeitas

    def test_pc_que_nao_liga(self):
        self.trocar("PC_MAC", "AA:BB:CC:DD:EE:FF")
        self.trocar("PC_URL", "http://127.0.0.1:9")
        self.trocar("ESPERA_WOL", 0.05)
        self.trocar("INTERVALO_WOL", 0.01)
        self.trocar("enviar_wol", lambda: None)
        resultado = self.servidor.ligar_pc()
        self.assertIn("mandei o sinal para ligar o PC, mas o agente não respondeu", resultado["erro"])

    def test_sem_mac_nao_oferece_ligar_pc(self):
        self.trocar("PC_URL", "http://127.0.0.1:9")
        self.llm.programar(resposta_texto("O PC está desligado."))
        self.enviar("abre o programa de teste")
        self.assertNotIn("ligar_pc", nomes_das_ferramentas(self.llm.pedidos_de_chat()[0]["json"]))
        self.assertIn("preencha \"pc_mac\"", self.servidor.ligar_pc()["erro"])

    # ---------- previsão do tempo ----------

    def test_previsao_do_tempo(self):
        falso = TempoDeMentira()
        self.trocar("TEMPO", falso)
        self.llm.programar(resposta_ferramenta("c1", "previsao_do_tempo", {"dias_a_frente": 1}),
                           resposta_texto("Amanhã chove."))
        dados = self.enviar("vai chover amanhã?")
        resultado = dados["acoes"][0]["resultado"]
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["condicao"], "chuva")
        self.assertEqual(falso.pedidos, [(None, 1)])  # sem cidade no pedido, vale a do config

    def test_previsao_com_erro_e_argumento_invalido(self):
        self.trocar("TEMPO", TempoDeMentira(self.servidor.tempo.ErroNoTempo("não encontrei a cidade Xyz", "")))
        self.assertEqual(self.servidor.previsao_do_tempo({"cidade": "Xyz"}), {"erro": "não encontrei a cidade Xyz"})
        # Quem confere dias_a_frente é o tempo.py, antes de qualquer consulta à internet.
        self.trocar("TEMPO", self.servidor.tempo.Tempo("Curitiba, PR"))
        self.assertEqual(self.servidor.previsao_do_tempo({"dias_a_frente": "muito"}),
                         {"erro": "só consigo a previsão de hoje até daqui a 6 dias"})

    # ---------- Spotify ----------

    def test_ferramentas_do_spotify_so_com_conta_conectada(self):
        self.trocar("SPOTIFY", SpotifyDeMentira(self.servidor.spotify, conectado=False))
        self.llm.programar(resposta_texto("Oi."), resposta_texto("Oi."))
        self.enviar("oi")
        self.assertNotIn("tocar_musica", nomes_das_ferramentas(self.llm.pedidos_de_chat()[0]["json"]))
        self.trocar("SPOTIFY", SpotifyDeMentira(self.servidor.spotify))
        self.enviar("oi")
        self.assertEqual(nomes_das_ferramentas(self.llm.pedidos_de_chat()[1]["json"])[-2:],
                         ["tocar_musica", "controlar_musica"])

    def test_tocar_musica(self):
        falso = SpotifyDeMentira(self.servidor.spotify)
        self.trocar("SPOTIFY", falso)
        self.llm.programar(resposta_ferramenta("c1", "tocar_musica", {"busca": "Tempo Perdido"}),
                           resposta_texto("Tocando."))
        dados = self.enviar("toca tempo perdido")
        self.assertEqual(dados["acoes"][0]["resultado"],
                         {"ok": True, "descricao": 'coloquei "Tempo Perdido", de Legião Urbana'})
        self.assertEqual(falso.pedidos, [("tocar", "Tempo Perdido", "musica")])

    def test_spotify_fechado_e_aberto_pelo_agente(self):
        erro = self.servidor.spotify.ErroNoSpotify("o Spotify não está aberto no PC", "sem_dispositivo")
        falso = SpotifyDeMentira(self.servidor.spotify, erros=[erro, erro])
        self.trocar("SPOTIFY", falso)
        self.trocar("INTERVALO_SPOTIFY", 0.01)
        pc = self.servidor.consultar_pc()
        resultado = self.servidor.tocar_musica({"busca": "Legião Urbana", "tipo": "artista"}, pc)
        self.assertEqual(resultado, {"ok": True, "descricao": 'abri o Spotify e coloquei "Legião Urbana", de Legião Urbana'})
        self.assertTrue(esperar_arquivo(self.marcador_spotify), "o agente não abriu o Spotify")
        self.assertEqual(len(falso.pedidos), 3)

    def test_spotify_que_nao_aparece_depois_de_aberto(self):
        erro = self.servidor.spotify.ErroNoSpotify("o Spotify não está aberto no PC", "sem_dispositivo")
        self.trocar("SPOTIFY", SpotifyDeMentira(self.servidor.spotify, erros=[erro] * 10))
        self.trocar("INTERVALO_SPOTIFY", 0.01)
        self.trocar("ESPERA_SPOTIFY", 0.03)
        resultado = self.servidor.tocar_musica({"busca": "x"}, self.servidor.consultar_pc())
        self.assertEqual(resultado, {"erro": "abri o Spotify no PC, mas ele ainda não apareceu para tocar; "
                                             "peça de novo em alguns segundos"})

    def test_outros_erros_do_spotify_nao_abrem_o_app(self):
        erro = self.servidor.spotify.ErroNoSpotify("o Spotify recusou: a conta precisa ser Premium", "premium",
                                                   "HTTP 403")
        self.trocar("SPOTIFY", SpotifyDeMentira(self.servidor.spotify, erros=[erro]))
        resultado = self.servidor.tocar_musica({"busca": "x"}, self.servidor.consultar_pc())
        self.assertEqual(resultado, {"erro": "o Spotify recusou: a conta precisa ser Premium"})
        self.popen.assert_not_called()
        self.assertIn("detalhe técnico: HTTP 403", self.terminal.getvalue())

    def test_controlar_musica_e_argumentos(self):
        self.trocar("SPOTIFY", SpotifyDeMentira(self.servidor.spotify))
        self.assertEqual(self.servidor.controlar_musica({"acao": "pausar"}), {"ok": True, "descricao": "pausei a música"})
        self.assertIn("a ação precisa ser", self.servidor.controlar_musica({"acao": "embaralhar"})["erro"])
        self.assertEqual(self.servidor.tocar_musica({"busca": "  "}, self.servidor.consultar_pc()),
                         {"erro": "diga o que tocar"})
        for tipo in (["musica"], {"a": 1}, 5):  # o Ollama não confere o schema: o LLM pode mandar qualquer coisa
            with self.subTest(tipo=tipo):
                self.assertIn("o tipo precisa ser", self.servidor.tocar_musica({"busca": "x", "tipo": tipo},
                                                                               self.servidor.consultar_pc())["erro"])
        self.trocar("SPOTIFY", None)
        self.assertEqual(self.servidor.tocar_musica({"busca": "x"}, self.servidor.consultar_pc()),
                         {"erro": "o Spotify não está configurado no servidor"})


@PRECISA_DEPENDENCIAS
class TestConfigDaV02(unittest.TestCase):
    def carregar(self, **config):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        return carregar_servidor(Path(tmp.name) / "servidor", **config)

    def encerra(self, mensagem: str, **config):
        with self.assertRaises(SystemExit) as saida:
            self.carregar(**config)
        self.assertIn(mensagem, str(saida.exception.code))

    def test_valores_de_exemplo_valem_como_nao_preenchidos(self):
        exemplo = json.loads((Path(__file__).parent.parent / "celular_servidor" / "config_servidor.example.json")
                             .read_text(encoding="utf-8"))
        servidor = self.carregar(cidade=exemplo["cidade"], pc_mac=exemplo["pc_mac"],
                                 pc_broadcast=exemplo["pc_broadcast"], spotify=exemplo["spotify"])
        self.assertIsNone(servidor.CIDADE)
        self.assertIsNone(servidor.PC_MAC)
        self.assertEqual(servidor.PC_BROADCAST, "255.255.255.255")
        self.assertIsNone(servidor.SPOTIFY)
        self.assertIsNone(servidor.aviso_do_spotify())

    def test_valores_preenchidos(self):
        servidor = self.carregar(cidade=" Curitiba, PR ", pc_mac="aa-bb-cc-dd-ee-ff", pc_broadcast="192.168.0.255",
                                 spotify={"client_id": "abc123", "dispositivo": "NOTEBOOK"})
        self.assertEqual(servidor.CIDADE, "Curitiba, PR")
        self.assertEqual(servidor.PC_MAC, "aa-bb-cc-dd-ee-ff")
        self.assertEqual(servidor.PC_BROADCAST, "192.168.0.255")
        self.assertEqual(servidor.SPOTIFY.client_id, "abc123")
        # Nos testes o spotify_token.json nunca é copiado: a conta aparece como não conectada.
        self.assertIn("rode python spotify_conectar.py", servidor.aviso_do_spotify())

    def test_formatos_errados_encerram_em_portugues(self):
        self.encerra('"pc_mac" precisa ser o endereço MAC da placa de rede do PC', pc_mac="AA:BB:CC")
        self.encerra('"pc_broadcast" precisa ser um endereço IPv4', pc_broadcast="192.168.0")
        self.encerra('"spotify" precisa ser um objeto entre chaves', spotify="abc")
        self.encerra('"cidade" precisa ser um texto entre aspas', cidade=123)
        self.encerra('"client_id" do bloco "spotify" precisa ser um texto', spotify={"client_id": 5})


if __name__ == "__main__":
    unittest.main()
