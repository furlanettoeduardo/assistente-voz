"""Testes do agente do PC. Usam só a biblioteca padrão, como o próprio agente."""
import contextlib
import errno
import http.client
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tests.auxiliares import (PASTA_AGENTE, comando_de_teste, copiar_componente, esperar_arquivo,
                              importar_copia, iniciar_agente, vigiar_popen)

TOKEN = "token-de-teste"
SEM_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# Funções de pc_agente/sistema.py que mexem no PC: nos testes, são sempre mocks.
FUNCOES_DO_SISTEMA = ("volume_ler", "volume_definir", "volume_mudo", "fechar", "bloquear", "desligar",
                      "cancelar_desligamento")


def blindar_sistema(agente):
    """Faz qualquer chamada ao sistema de verdade falhar o teste, em vez de mexer no volume ou bloquear a tela."""
    bloqueios = {nome: mock.Mock(side_effect=AssertionError(f"o teste chamou sistema.{nome} de verdade"))
                 for nome in FUNCOES_DO_SISTEMA}
    patcher = mock.patch.multiple(agente.sistema, **bloqueios)
    patcher.start()
    return patcher


def rodar_agente(pasta: Path) -> subprocess.CompletedProcess:
    """Roda `python agente.py` numa pasta e devolve a saída, com timeout para não travar."""
    return subprocess.run(
        [sys.executable, str(pasta / "agente.py")], cwd=pasta, capture_output=True,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=15,
    )


class TestAgenteHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.pasta = Path(cls.tmp.name)
        cls.marcador = cls.pasta / "programa-aberto.txt"
        cls.agente, cls.servidor = iniciar_agente(
            cls.pasta / "agente", TOKEN, {"programa de teste": comando_de_teste(cls.marcador)})
        # Sem "acoes" no config, volume e bloquear estão liberados: nenhum teste daqui pode chegar ao sistema.
        cls.blindagem = blindar_sistema(cls.agente)

    @classmethod
    def tearDownClass(cls):
        cls.blindagem.stop()
        cls.servidor.parar()
        cls.tmp.cleanup()

    def setUp(self):
        self.marcador.unlink(missing_ok=True)

    def pedir(self, metodo: str, caminho: str, token=TOKEN, corpo=None) -> tuple[int, dict]:
        cabecalhos = {"Authorization": f"Bearer {token}"} if token is not None else {}
        dados = None
        if corpo is not None:
            dados = corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode("utf-8")
            cabecalhos["Content-Type"] = "application/json"
        pedido = urllib.request.Request(self.servidor.url + caminho, data=dados,
                                        headers=cabecalhos, method=metodo)
        try:
            with contextlib.redirect_stdout(io.StringIO()), SEM_PROXY.open(pedido, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_token_errado_e_recusado(self):
        popen = vigiar_popen(self, self.agente)
        for token in ("errado", "", None, TOKEN + "x"):
            with self.subTest(token=token):
                self.assertEqual(self.pedir("GET", "/programas", token=token),
                                 (401, {"erro": "token inválido"}))
                self.assertEqual(self.pedir("POST", "/abrir", token=token,
                                            corpo={"programa": "programa de teste"}),
                                 (401, {"erro": "token inválido"}))
        popen.assert_not_called()
        self.assertFalse(self.marcador.exists())

    def test_lista_programas_com_token_certo(self):
        # Sem "fechar" e sem "acoes" no config: nada para fechar e as ações padrão (desligar fica de fora).
        self.assertEqual(self.pedir("GET", "/programas"),
                         (200, {"programas": ["programa de teste"], "fechaveis": [], "acoes": ["bloquear", "volume"]}))

    def test_programa_fora_da_lista_e_recusado(self):
        popen = vigiar_popen(self, self.agente)
        for nome in ("cmd", "calc.exe", "rm -rf /", "programa de teste; calc", sys.executable, ""):
            with self.subTest(nome=nome):
                status, resposta = self.pedir("POST", "/abrir", corpo={"programa": nome})
                self.assertEqual(status, 404)
                self.assertIn("não está na lista", resposta["erro"])
        popen.assert_not_called()

    def test_abre_programa_da_lista_sem_shell(self):
        popen = vigiar_popen(self, self.agente)
        status, resposta = self.pedir("POST", "/abrir", corpo={"programa": "  Programa de Teste "})
        self.assertEqual((status, resposta), (200, {"ok": True, "programa": "programa de teste"}))
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], comando_de_teste(self.marcador))
        self.assertFalse(kwargs.get("shell", False))
        for fluxo in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[fluxo], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertNotIn("start_new_session", kwargs)
        else:  # o programa não pode fechar junto com o agente
            self.assertIs(kwargs.get("start_new_session"), True)
        self.assertTrue(esperar_arquivo(self.marcador), "o programa de teste não chegou a rodar")

    def test_json_invalido(self):
        popen = vigiar_popen(self, self.agente)
        for corpo in (b"isto nao e json", b"", b"[]", b"null", b'"programa de teste"', b"1"):
            with self.subTest(corpo=corpo):
                self.assertEqual(self.pedir("POST", "/abrir", corpo=corpo),
                                 (400, {"erro": "JSON inválido"}))
        popen.assert_not_called()

    def test_content_length_invalido(self):
        popen = vigiar_popen(self, self.agente)
        for tamanho in ("abc", "-1", str(10**9)):
            with self.subTest(tamanho=tamanho):
                conexao = http.client.HTTPConnection("127.0.0.1", self.servidor.httpd.server_port, timeout=10)
                self.addCleanup(conexao.close)
                conexao.request("POST", "/abrir", body=b"",
                                headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": tamanho})
                resposta = conexao.getresponse()
                self.assertEqual((resposta.status, json.loads(resposta.read())), (400, {"erro": "JSON inválido"}))
        popen.assert_not_called()

    def test_le_o_corpo_antes_de_recusar(self):
        # Respondendo sem ler o corpo, o Windows às vezes derrubava a conexão do cliente
        # (ConnectionAbortedError, WinError 10053) em vez de entregar o 401 ou o 404.
        corpo = json.dumps({"programa": "programa de teste"}).encode("utf-8")
        for caminho, token, esperado in [("/abrir", "errado", b" 401 "), ("/volume", "errado", b" 401 "),
                                         ("/programas", TOKEN, b" 404 ")]:
            with self.subTest(caminho=caminho), contextlib.redirect_stdout(io.StringIO()), \
                    socket.create_connection(("127.0.0.1", self.servidor.httpd.server_port), timeout=10) as s:
                s.sendall(f"POST {caminho} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
                          f"Content-Length: {len(corpo)}\r\n\r\n".encode("utf-8"))
                s.settimeout(0.5)
                with self.assertRaises(TimeoutError, msg="respondeu antes de receber o corpo"):
                    s.recv(1024)
                s.settimeout(10)
                s.sendall(corpo)
                resposta = b""
                while pedaco := s.recv(4096):
                    resposta += pedaco
                self.assertIn(esperado, resposta.split(b"\r\n", 1)[0])

    def test_pedido_incompleto_nao_abre_programa(self):
        self.assertIsNotNone(vars(self.agente.Handler).get("timeout"), "o agente precisa de timeout nas conexões")
        popen = vigiar_popen(self, self.agente)
        with mock.patch.object(self.agente.Handler, "timeout", 0.5):  # só para o teste ser rápido
            with socket.create_connection(("127.0.0.1", self.servidor.httpd.server_port), timeout=10) as s:
                s.sendall(f"POST /abrir HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                          "Content-Length: 100\r\n\r\n{\"programa\": \"programa de".encode("utf-8"))
                self.assertEqual(s.recv(1024), b"", "o agente deveria fechar a conexão parada")
        popen.assert_not_called()

    def test_cabecalho_com_acento_recebe_401(self):
        # compare_digest com str levantava TypeError e o agente fechava a conexão sem responder.
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida), \
                socket.create_connection(("127.0.0.1", self.servidor.httpd.server_port), timeout=10) as s:
            s.sendall(b"GET /programas HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer senha-\xe7\xe3o\r\n\r\n")
            resposta = b""
            while pedaco := s.recv(4096):
                resposta += pedaco
        self.assertIn(b" 401 ", resposta.split(b"\r\n", 1)[0])
        self.assertIn("pedido recusado de 127.0.0.1: token inválido", saida.getvalue())

    def test_segundo_agente_na_mesma_porta_e_recusado(self):
        # No Windows, o SO_REUSEADDR padrão do http.server deixava os dois escutarem sem erro.
        primeiro = self.agente.Servidor(("127.0.0.1", 0), self.agente.Handler)
        self.addCleanup(primeiro.server_close)
        with self.assertRaises(OSError):
            self.agente.Servidor(("127.0.0.1", primeiro.server_port), self.agente.Handler).server_close()

    def test_rota_desconhecida(self):
        self.assertEqual(self.pedir("GET", "/abrir")[0], 404)
        self.assertEqual(self.pedir("GET", "/volume")[0], 404)
        self.assertEqual(self.pedir("POST", "/programas", corpo={})[0], 404)
        self.assertEqual(self.pedir("POST", "/volume/", corpo={"acao": "consultar"})[0], 404)


class TestAgenteConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.pasta = copiar_componente(PASTA_AGENTE, Path(tmp.name) / "agente")

    def gravar_config(self, conteudo: str) -> None:
        (self.pasta / "config_agente.json").write_text(conteudo, encoding="utf-8")

    def test_sem_config_encerra_pedindo_para_copiar_o_exemplo(self):
        saida = rodar_agente(self.pasta)
        self.assertEqual(saida.returncode, 1)
        self.assertIn("config_agente.json", saida.stderr)
        self.assertIn("Copie config_agente.example.json", saida.stderr)
        self.assertNotIn("Traceback", saida.stderr)

    def test_json_quebrado_encerra_com_linha_coluna_e_motivo_em_portugues(self):
        casos = [
            ('{"token": "C:\\Pasta"}', "barra invertida simples dentro de um texto"),
            ('{"token": "D:\\utilitarios"}', "barra invertida simples dentro de um texto"),  # \u tem outra mensagem
            ('{"token": "abc" "porta": 1}', "falta uma vírgula entre dois itens"),
            ('{"token": "abc",}', "vírgula sobrando antes do }"),  # a mensagem do json mudou no 3.13
            ('{"token": "abc', "aspas abertas que não foram fechadas"),
            ('{"token": "abc"} sobra', "tem texto sobrando depois do último }"),
        ]
        for conteudo, motivo in casos:
            with self.subTest(conteudo=conteudo):
                self.gravar_config(conteudo)
                saida = rodar_agente(self.pasta)
                self.assertEqual(saida.returncode, 1)
                self.assertIn("linha 1, coluna", saida.stderr)
                self.assertIn(motivo, saida.stderr)
                self.assertNotIn("Expecting", saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)

    def test_token_de_exemplo_vazio_ou_com_acento_nao_inicia(self):
        exemplo = json.loads((PASTA_AGENTE / "config_agente.example.json").read_text(encoding="utf-8"))
        # Com token vazio, um pedido sem cabeçalho Authorization seria aceito.
        for token in (exemplo["token"], "", "   ", None, "senha-ção"):
            with self.subTest(token=token):
                # Porta 0: se a checagem regredir, o agente escuta numa porta livre em vez da 8765.
                self.gravar_config(json.dumps({**exemplo, "token": token, "porta": 0}, ensure_ascii=False))
                saida = rodar_agente(self.pasta)
                self.assertEqual(saida.returncode, 1)
                self.assertIn("Defina um token próprio", saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)

    def test_config_incompleto_ou_em_outra_codificacao_encerra_com_mensagem(self):
        com_acento = {"token": TOKEN, "programas": {"música": ["x"]}}
        casos = [
            (b"[]", "precisa ser um objeto JSON"),
            (json.dumps({"token": TOKEN}).encode("utf-8"), ": programas. Compare com config_agente.example.json"),
            (json.dumps(com_acento, ensure_ascii=False).encode("cp1252"), "não está em UTF-8"),  # ANSI
            (json.dumps(com_acento, ensure_ascii=False).encode("utf-16"), "não está em UTF-8"),
        ]
        for conteudo, mensagem in casos:
            with self.subTest(mensagem=mensagem):
                (self.pasta / "config_agente.json").write_bytes(conteudo)
                saida = rodar_agente(self.pasta)
                self.assertEqual(saida.returncode, 1)
                self.assertIn(mensagem, saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)

    def test_valores_com_tipo_errado_encerram_com_mensagem(self):
        base = {"token": TOKEN, "porta": 0, "programas": {"calculadora": ["calc.exe"]}}
        casos = [
            ({"programas": []}, '"programas" precisa ser um objeto entre chaves'),
            ({"programas": None}, '"programas" precisa ser um objeto entre chaves'),
            ({"programas": {}}, "Cadastre pelo menos um programa"),
            ({"programas": {"calculadora": "calc.exe"}}, 'o comando de "calculadora" precisa ser uma lista de textos'),
            ({"programas": {"calculadora": []}}, 'o comando de "calculadora" precisa ser uma lista de textos'),
            ({"programas": {"calculadora": ["calc.exe", 1]}}, 'o comando de "calculadora" precisa ser uma lista'),
            ({"porta": ""}, '"porta" precisa ser um número entre 0 e 65535'),
            ({"porta": None}, '"porta" precisa ser um número entre 0 e 65535'),
            ({"porta": 70000}, '"porta" precisa ser um número entre 0 e 65535'),
        ]
        for mudanca, mensagem in casos:
            with self.subTest(mudanca=mudanca):
                self.gravar_config(json.dumps({**base, **mudanca}))
                saida = rodar_agente(self.pasta)
                self.assertEqual(saida.returncode, 1)
                self.assertIn(mensagem, saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)

    def test_porta_como_texto_numerico_e_aceita(self):
        self.gravar_config(json.dumps({"token": TOKEN, "porta": "8765", "programas": {"calculadora": ["calc.exe"]}}))
        self.assertEqual(importar_copia(self.pasta, "agente").PORTA, 8765)

    def test_aceita_bom_do_bloco_de_notas(self):
        config = {"token": TOKEN, "porta": 0, "programas": {"calculadora": ["calc.exe"]}}
        (self.pasta / "config_agente.json").write_text(json.dumps(config), encoding="utf-8-sig")
        self.assertEqual(importar_copia(self.pasta, "agente").TOKEN, TOKEN)

    def test_nomes_com_maiusculas_no_config_abrem(self):
        # O servidor põe no enum da ferramenta os nomes que /programas devolve, e o LLM manda
        # exatamente esse texto de volta: os dois lados precisam usar a mesma forma.
        comando = ["programa-que-nao-roda"]
        agente, servidor = iniciar_agente(self.pasta.parent / "agente-maiusculas", TOKEN,
                                          {"VS Code": comando, " Bloco  de Notas ": comando})
        self.addCleanup(servidor.parar)
        cabecalhos = {"Authorization": f"Bearer {TOKEN}"}
        with SEM_PROXY.open(urllib.request.Request(servidor.url + "/programas", headers=cabecalhos),
                            timeout=10) as r:
            self.assertEqual(json.loads(r.read())["programas"], ["bloco de notas", "vs code"])

        with mock.patch.object(agente.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            for pedido in ("vs code", "VS Code", "  bloco de   notas"):
                with self.subTest(pedido=pedido):
                    corpo = json.dumps({"programa": pedido}).encode("utf-8")
                    with SEM_PROXY.open(urllib.request.Request(servidor.url + "/abrir", data=corpo,
                                                               headers=cabecalhos), timeout=10) as r:
                        self.assertEqual(r.status, 200)
        self.assertEqual(popen.call_count, 3)

    def test_comando_inexistente_explica_em_portugues(self):
        agente, servidor = iniciar_agente(self.pasta.parent / "agente-inexistente", TOKEN,
                                          {"fantasma": ["programa-que-nao-existe-xyz"]})
        self.addCleanup(servidor.parar)
        pedido = urllib.request.Request(servidor.url + "/abrir", data=b'{"programa": "fantasma"}',
                                        headers={"Authorization": f"Bearer {TOKEN}"})
        with contextlib.redirect_stdout(io.StringIO()) as console, self.assertRaises(urllib.error.HTTPError) as erro:
            SEM_PROXY.open(pedido, timeout=10)
        with erro.exception as resposta:
            self.assertEqual(resposta.code, 500)
            self.assertEqual(json.loads(resposta.read()), {"erro": "não encontrei 'programa-que-nao-existe-xyz' no PC; "
                                                                   "confira o comando de 'fantasma' no config_agente.json"})
        self.assertIn("detalhe técnico", console.getvalue())
        self.assertEqual(agente.explicar_falha("fantasma", PermissionError()),
                         "o PC não deu permissão para executar 'programa-que-nao-existe-xyz'")
        self.assertEqual(agente.explicar_falha("fantasma", OSError()), "o PC não conseguiu abrir 'fantasma'")

    def test_falha_ao_escutar_explica_em_portugues(self):
        self.gravar_config(json.dumps({"token": TOKEN, "porta": 8765, "programas": {"calculadora": ["calc.exe"]}}))
        agente = importar_copia(self.pasta, "agente")
        ocupada = agente.explicar_falha_ao_escutar(OSError(errno.EADDRINUSE, "Address already in use"))
        self.assertIn("Não consegui escutar na porta 8765 (detalhe técnico:", ocupada)
        self.assertIn("Outro agente (ou outro programa) já usa essa porta", ocupada)
        proibida = agente.explicar_falha_ao_escutar(PermissionError(errno.EACCES, "Permission denied"))
        self.assertIn("algumas ficam reservadas pelo Hyper-V ou pelo WSL", proibida)  # 8765: reservada no Windows
        with mock.patch.object(agente, "PORTA", 80):
            baixa = agente.explicar_falha_ao_escutar(PermissionError(errno.EACCES, "Permission denied"))
        self.assertIn("O sistema só deixa usar portas acima de 1024", baixa)

    # Os dois abaixo sobem o agente de verdade em 0.0.0.0: no Windows isso abriria o aviso do firewall.
    @unittest.skipIf(sys.platform == "win32", "sobe o agente em 0.0.0.0, o que aciona o firewall do Windows")
    def test_ctrl_c_encerra_sem_traceback(self):
        self.gravar_config(json.dumps({"token": TOKEN, "porta": 0, "programas": {"calculadora": ["calc.exe"]}}))
        processo = subprocess.Popen([sys.executable, "-u", str(self.pasta / "agente.py")], cwd=self.pasta,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        vigia = threading.Timer(15, processo.kill)  # se o agente travar calado, o readline abaixo não prende a suíte
        vigia.start()
        self.addCleanup(vigia.cancel)
        self.addCleanup(lambda: processo.poll() is None and processo.kill())
        linhas = []
        while not linhas or "para parar" not in linhas[-1]:
            linha = processo.stdout.readline()
            if not linha:
                self.fail(f"o agente parou antes de ficar pronto: {''.join(linhas)}")
            linhas.append(linha)
        primeira = linhas[0]
        time.sleep(0.3)  # deixa o agente entrar no serve_forever
        processo.send_signal(signal.SIGINT)
        resto, _ = processo.communicate(timeout=15)
        self.assertRegex(primeira, r"\[agente\] escutando na porta [1-9]\d*")  # a porta real, não 0
        self.assertIn("[agente] encerrado.", resto)
        self.assertNotIn("Traceback", resto)
        self.assertEqual(processo.returncode, 0)

    @unittest.skipIf(sys.platform == "win32", "sobe o agente em 0.0.0.0, o que aciona o firewall do Windows")
    def test_porta_ocupada_encerra_com_mensagem(self):
        with socket.socket() as ocupada:
            ocupada.bind(("127.0.0.1", 0))
            ocupada.listen()
            porta = ocupada.getsockname()[1]
            self.gravar_config(json.dumps({"token": TOKEN, "porta": porta, "programas": {"calc": ["calc.exe"]}}))
            saida = rodar_agente(self.pasta)
        self.assertEqual(saida.returncode, 1)
        self.assertIn(f"Não consegui escutar na porta {porta} (detalhe técnico:", saida.stderr)
        self.assertIn("Outro agente (ou outro programa) já usa essa porta", saida.stderr)

    def test_exemplo_tem_as_chaves_que_o_codigo_usa(self):
        exemplo = json.loads((PASTA_AGENTE / "config_agente.example.json").read_text(encoding="utf-8"))
        self.assertLessEqual({"token", "porta", "programas", "fechar", "acoes"}, set(exemplo))
        for nome, comando in exemplo["programas"].items():
            with self.subTest(nome=nome):
                self.assertIsInstance(comando, list)
                self.assertTrue(all(isinstance(parte, str) for parte in comando))
        # O exemplo, só com um token de verdade, precisa passar pelas validações do agente.
        self.gravar_config(json.dumps({**exemplo, "token": TOKEN, "porta": 0}, ensure_ascii=False))
        agente = importar_copia(self.pasta, "agente")
        self.assertEqual(agente.FECHAR, {"bloco de notas": ["notepad.exe"], "calculadora": ["CalculatorApp.exe"],
                                         "chrome": ["chrome.exe"], "spotify": ["Spotify.exe"]})
        self.assertEqual(agente.ACOES, {"volume", "bloquear", "desligar"})


class TestConfigFecharEAcoes(unittest.TestCase):
    """As chaves opcionais "fechar" e "acoes" do config_agente.json."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.pasta = copiar_componente(PASTA_AGENTE, Path(tmp.name) / "agente")

    def gravar(self, **extra) -> None:
        config = {"token": TOKEN, "porta": 0, "programas": {"calculadora": ["calc.exe"]}, **extra}
        (self.pasta / "config_agente.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def carregar(self, **extra):
        self.gravar(**extra)
        return importar_copia(self.pasta, "agente")

    def conferir_recusa(self, casos: list[tuple[dict, str]]) -> None:
        for extra, mensagem in casos:
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as saida:
                    self.carregar(**extra)
                texto = str(saida.exception.code)
                self.assertIn(mensagem, texto)
                self.assertIn("config_agente.json", texto)

    def test_config_antigo_sem_as_chaves_novas_continua_valendo(self):
        agente = self.carregar()
        self.assertEqual(agente.FECHAR, {})
        self.assertEqual(agente.ACOES, {"volume", "bloquear"})  # desligar só quando o dono libera
        self.assertEqual(agente.resumo_da_config(), ["[agente] programas liberados: calculadora",
                                                     "[agente] ações liberadas: bloquear, volume"])

    def test_fechar_e_acoes_validos(self):
        agente = self.carregar(fechar={" Bloco de  Notas": " notepad.exe ", "VS Code": ["Code.exe", "Code Helper.exe"]},
                               acoes=["Volume", "desligar", "desligar"])
        self.assertEqual(agente.FECHAR, {"bloco de notas": ["notepad.exe"], "vs code": ["Code.exe", "Code Helper.exe"]})
        self.assertEqual(agente.ACOES, {"volume", "desligar"})
        self.assertEqual(agente.resumo_da_config(), ["[agente] programas liberados: calculadora",
                                                     "[agente] fecha: bloco de notas, vs code",
                                                     "[agente] ações liberadas: desligar, volume"])

    def test_nenhuma_acao_liberada(self):
        agente = self.carregar(fechar={}, acoes=[])
        self.assertEqual((agente.FECHAR, agente.ACOES), ({}, frozenset()))
        self.assertEqual(agente.resumo_da_config(), ["[agente] programas liberados: calculadora"])

    def test_fechar_com_formato_errado_encerra_com_mensagem(self):
        objeto = '"fechar" precisa ser um objeto entre chaves'
        texto = 'o processo de "chrome" em "fechar" precisa ser um texto, como "chrome.exe", ou uma lista de textos'
        self.conferir_recusa([
            ({"fechar": []}, objeto),
            ({"fechar": "chrome.exe"}, objeto),
            ({"fechar": None}, objeto),
            ({"fechar": {"chrome": 5}}, texto),
            ({"fechar": {"chrome": None}}, texto),
            ({"fechar": {"chrome": []}}, texto),
            ({"fechar": {"chrome": ["chrome.exe", 1]}}, texto),
            ({"fechar": {"chrome": {"processo": "chrome.exe"}}}, texto),
        ])

    def test_processo_precisa_ser_so_um_nome_de_arquivo(self):
        # Um "*" no taskkill poderia fechar tudo; um caminho não é o que o taskkill e o pkill esperam.
        casos = ["*", "chrome*.exe", "chr?me.exe", "C:\\Program Files\\Google\\chrome.exe", "/usr/bin/firefox",
                 "pasta/chrome.exe", "", "   ", "-9", "-u"]
        self.conferir_recusa([({"fechar": {"teste": processo}},
                               f'"{processo.strip()}" (em "fechar", no "teste") precisa ser só o nome do processo')
                              for processo in casos])
        self.conferir_recusa([({"fechar": {"teste": ["notepad.exe", "*"]}}, '"*" (em "fechar", no "teste")')])

    def test_no_windows_o_processo_precisa_do_exe(self):
        # Sem o .exe, o taskkill devolve 128 ("não encontrado") mesmo com o programa aberto.
        agente = self.carregar()
        caminho = self.pasta / "config_agente.json"
        # Um ponto no meio não basta: "WhatsApp.Root" daria sempre "não está aberto".
        for processo in ("chrome", "Code Helper", "CHROME", "WhatsApp.Root", "chrome.exe.bak", "chrome.ex", "exe"):
            with self.subTest(processo=processo), self.assertRaises(SystemExit) as saida:
                agente.ler_fechar({"fechar": {"Chrome": ["chrome.exe", processo]}}, caminho, plataforma="win32")
            texto = str(saida.exception.code)
            self.assertIn(f'"{processo}" (em "fechar", no "chrome") precisa ter o .exe no fim', texto)
            self.assertIn("Gerenciador de Tarefas", texto)
        self.assertEqual(agente.ler_fechar({"fechar": {"vs code": ["Code.exe", "Code Helper.EXE"],
                                                       "whatsapp": "WhatsApp.Root.exe"}}, caminho, plataforma="win32"),
                         {"vs code": ["Code.exe", "Code Helper.EXE"], "whatsapp": ["WhatsApp.Root.exe"]})
        # No Linux, os processos não têm extensão.
        self.assertEqual(agente.ler_fechar({"fechar": {"firefox": "firefox"}}, caminho, plataforma="linux"),
                         {"firefox": ["firefox"]})

    def test_recusa_o_application_frame_host(self):
        # Ele é a moldura de todos os aplicativos da Microsoft Store: fechá-lo fecharia todos.
        mensagem = ('"loja" em "fechar" não pode usar o ApplicationFrameHost.exe: ele é a moldura de todos os '
                    "aplicativos da Microsoft Store, e fechá-lo fecharia todos. Use o processo do próprio aplicativo, "
                    "como CalculatorApp.exe.")
        self.conferir_recusa([({"fechar": {"loja": "ApplicationFrameHost.exe"}}, mensagem),
                              ({"fechar": {"loja": ["CalculatorApp.exe", " applicationframehost.EXE"]}}, mensagem)])

    def test_recusa_o_explorer(self):
        # O WM_CLOSE na área de trabalho abre a caixa "Desligar o Windows", passando por cima de "acoes" e da
        # confirmação por voz.
        mensagem = ('"pastas" em "fechar" não pode usar o explorer.exe: ele é a barra de tarefas, a área de trabalho '
                    'e o Explorador de Arquivos do Windows, e pedir para fechá-lo abre a caixa "Desligar o Windows".')
        self.conferir_recusa([({"fechar": {"pastas": processo}}, mensagem)
                              for processo in ("explorer.exe", "Explorer.EXE", " EXPLORER.exe ")])
        self.conferir_recusa([({"fechar": {"pastas": ["notepad.exe", "explorer.exe"]}}, mensagem)])

    def test_recusa_processos_do_proprio_windows(self):
        self.conferir_recusa([({"fechar": {"sistema": "DWM.exe"}}, '"sistema" em "fechar" não pode usar o dwm.exe: '
                               "ele é parte do próprio Windows, e não um programa para fechar.")])
        agente = self.carregar()
        caminho = self.pasta / "config_agente.json"
        for processo in ("dwm.exe", "csrss.exe", "winlogon.exe", "svchost.exe", "lsass.exe", "services.exe",
                         "smss.exe", "wininit.exe"):
            # Em minúsculas ou não, em qualquer plataforma (antes da conferência do .exe).
            for variante, plataforma in ((processo, "win32"), (processo.upper(), "win32"),
                                         (processo.capitalize(), "linux")):
                with self.subTest(processo=variante, plataforma=plataforma), \
                        self.assertRaises(SystemExit) as saida:
                    agente.ler_fechar({"fechar": {"sistema": ["notepad.exe", variante]}}, caminho,
                                      plataforma=plataforma)
                self.assertIn(f'"sistema" em "fechar" não pode usar o {processo}: ele é parte do próprio Windows, e '
                              "não um programa para fechar.", str(saida.exception.code))
        with self.assertRaises(SystemExit) as saida:
            agente.ler_fechar({"fechar": {"barra": "explorer.exe"}}, caminho, plataforma="linux")
        self.assertIn("não pode usar o explorer.exe", str(saida.exception.code))
        # Só o nome exato: um processo que apenas começa igual continua valendo.
        self.assertEqual(agente.ler_fechar({"fechar": {"x": ["explorer++.exe", "svchost2.exe"]}}, caminho,
                                           plataforma="win32"), {"x": ["explorer++.exe", "svchost2.exe"]})

    def test_acoes_com_formato_errado_encerram_com_mensagem(self):
        lista = '"acoes" precisa ser uma lista de textos, como ["volume", "bloquear"]'
        self.conferir_recusa([
            ({"acoes": "volume"}, lista),
            ({"acoes": None}, lista),
            ({"acoes": {"volume": True}}, lista),
            ({"acoes": ["volume", 1]}, lista),
            ({"acoes": ["volume", "reiniciar"]}, '"acoes" tem uma ação que não existe: "reiniciar"'),
            ({"acoes": [""]}, '"acoes" tem uma ação que não existe: ""'),
        ])

    def test_config_errado_encerra_sem_traceback(self):
        self.gravar(fechar={"tudo": "*"})
        saida = rodar_agente(self.pasta)
        self.assertEqual(saida.returncode, 1)
        self.assertIn('"*" (em "fechar", no "tudo") precisa ser só o nome do processo', saida.stderr)
        self.assertNotIn("Traceback", saida.stderr)


def pedir_ao_agente(servidor, caminho: str, corpo=None, token=TOKEN, metodo: str = "POST") -> tuple[int, dict]:
    cabecalhos = {"Authorization": f"Bearer {token}"} if token is not None else {}
    dados = None
    if corpo is not None:
        dados = corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode("utf-8")
        cabecalhos["Content-Type"] = "application/json"
    pedido = urllib.request.Request(servidor.url + caminho, data=dados, headers=cabecalhos, method=metodo)
    try:
        with SEM_PROXY.open(pedido, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read())


class TestAgenteAcoes(unittest.TestCase):
    """
    Rotas /fechar, /volume e /energia pelo HTTP. As funções do sistema são sempre mocks: nenhum
    teste fecha programas, mexe no volume, bloqueia a tela ou desliga o PC de verdade.
    """
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        pasta = Path(cls.tmp.name)
        programas = {"calculadora": ["calc.exe"]}
        cls.agente, cls.servidor = iniciar_agente(
            pasta / "completo", TOKEN, programas, acoes=["volume", "bloquear", "desligar"],
            fechar={"Bloco de Notas": "notepad.exe", "VS Code": ["Code.exe", "CodeHelper.exe"]})
        cls.padrao, cls.servidor_padrao = iniciar_agente(pasta / "padrao", TOKEN, programas)  # sem as chaves novas
        cls.travado, cls.servidor_travado = iniciar_agente(pasta / "travado", TOKEN, programas, acoes=[])

    @classmethod
    def tearDownClass(cls):
        for servidor in (cls.servidor, cls.servidor_padrao, cls.servidor_travado):
            servidor.parar()
        cls.tmp.cleanup()

    def setUp(self):
        # Um só mock para as três cópias: mock_calls mostra todas as chamadas, na ordem.
        self.sistema = mock.Mock()
        for agente in (self.agente, self.padrao, self.travado):
            for nome in FUNCOES_DO_SISTEMA:
                patcher = mock.patch.object(agente.sistema, nome, getattr(self.sistema, nome))
                patcher.start()
                self.addCleanup(patcher.stop)
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        self.terminal = pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.erros = pilha.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def pedir(self, caminho: str, corpo=None, token=TOKEN, servidor=None) -> tuple[int, dict]:
        return pedir_ao_agente(servidor or self.servidor, caminho, corpo, token)

    def erro_no_sistema(self, mensagem: str, detalhe: str = ""):
        return self.agente.sistema.ErroNoSistema(mensagem, detalhe)  # a classe da cópia que o agente usa

    # ----- /programas, token e JSON -----

    def test_programas_lista_fechaveis_e_acoes(self):
        casos = [
            (self.servidor, {"programas": ["calculadora"], "fechaveis": ["bloco de notas", "vs code"],
                             "acoes": ["bloquear", "desligar", "volume"]}),
            (self.servidor_padrao, {"programas": ["calculadora"], "fechaveis": [], "acoes": ["bloquear", "volume"]}),
            (self.servidor_travado, {"programas": ["calculadora"], "fechaveis": [], "acoes": []}),
        ]
        for servidor, esperado in casos:
            with self.subTest(esperado=esperado):
                self.assertEqual(pedir_ao_agente(servidor, "/programas", metodo="GET"), (200, esperado))

    def test_rotas_novas_exigem_token(self):
        pedidos = [("/fechar", {"programa": "bloco de notas"}), ("/volume", {"acao": "mudo"}),
                   ("/volume", {"acao": "definir", "nivel": 100}), ("/energia", {"acao": "bloquear"}),
                   ("/energia", {"acao": "desligar"}), ("/energia", {"acao": "cancelar"})]
        for caminho, corpo in pedidos:
            for token in ("errado", "", None, TOKEN + "x"):
                with self.subTest(caminho=caminho, corpo=corpo, token=token):
                    self.assertEqual(self.pedir(caminho, corpo, token=token), (401, {"erro": "token inválido"}))
        self.assertEqual(self.sistema.mock_calls, [])

    def test_json_invalido_nas_rotas_novas(self):
        for caminho in ("/fechar", "/volume", "/energia"):
            for corpo in (b"isto nao e json", b"", b"[]", b"null", b'"mudo"'):
                with self.subTest(caminho=caminho, corpo=corpo):
                    self.assertEqual(self.pedir(caminho, corpo), (400, {"erro": "JSON inválido"}))
        self.assertEqual(self.sistema.mock_calls, [])

    # ----- /fechar -----

    def test_fechar_cada_resultado(self):
        casos = [
            ("fechou", 200, {"ok": True, "programa": "bloco de notas", "descricao": "fechei bloco de notas"}),
            ("pediu", 200, {"ok": True, "programa": "bloco de notas",
                            "descricao": "pedi para fechar bloco de notas, mas ele continua aberto; talvez esteja "
                                         "esperando você salvar algo"}),
            ("nao_aberto", 409, {"erro": "bloco de notas não está aberto"}),
            ("nao_fechou", 500, {"erro": "o PC não conseguiu fechar bloco de notas"}),
        ]
        for resultado, status, resposta in casos:
            with self.subTest(resultado=resultado):
                self.sistema.reset_mock()
                self.sistema.fechar.return_value = resultado
                self.assertEqual(self.pedir("/fechar", {"programa": "bloco de notas"}), (status, resposta))
                self.assertEqual(self.sistema.mock_calls, [mock.call.fechar(["notepad.exe"])])
        self.assertIn("[agente] fechar bloco de notas: fechou", self.terminal.getvalue())

    def test_fechar_usa_o_nome_normalizado_e_a_lista_de_processos(self):
        self.sistema.fechar.return_value = "fechou"
        self.assertEqual(self.pedir("/fechar", {"programa": "  VS  code "}),
                         (200, {"ok": True, "programa": "vs code", "descricao": "fechei vs code"}))
        self.sistema.fechar.assert_called_once_with(["Code.exe", "CodeHelper.exe"])

    def test_fechar_fora_da_lista_e_recusado(self):
        casos = [("calculadora", "calculadora"),  # abre, mas não está em "fechar"
                 ("notepad.exe", "notepad.exe"), ("*", "*"), ("", ""), ("bloco de notas; calc", "bloco de notas; calc")]
        for pedido, nome in casos:
            with self.subTest(pedido=pedido):
                self.assertEqual(self.pedir("/fechar", {"programa": pedido}),
                                 (404, {"erro": f"'{nome}' não está na lista de programas que posso fechar"}))
        self.assertEqual(self.pedir("/fechar", {}),
                         (404, {"erro": "'' não está na lista de programas que posso fechar"}))
        self.assertEqual(self.pedir("/fechar", {"programa": "bloco de notas"}, servidor=self.servidor_padrao)[0], 404)
        self.assertEqual(self.sistema.mock_calls, [])

    def test_erro_inesperado_responde_500_em_portugues(self):
        # Um erro que não é ErroNoSistema (um bug, por exemplo) não pode derrubar a conexão sem resposta.
        self.sistema.volume_ler.side_effect = RuntimeError("falha inesperada de teste")
        self.sistema.fechar.side_effect = ValueError("embedded null byte")
        for caminho, corpo in (("/volume", {"acao": "consultar"}), ("/fechar", {"programa": "bloco de notas"})):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.pedir(caminho, corpo),
                                 (500, {"erro": "o agente do PC teve um erro inesperado; veja o terminal do PC"}))
        erros = self.erros.getvalue()
        self.assertIn("[agente] erro inesperado em /volume", erros)
        self.assertIn("RuntimeError: falha inesperada de teste", erros)  # o traceback fica só no terminal
        self.assertIn("ValueError: embedded null byte", erros)

    def test_fechar_com_erro_do_sistema(self):
        self.sistema.fechar.side_effect = self.erro_no_sistema("não encontrei o comando taskkill neste PC",
                                                               "[WinError 2] The system cannot find the file")
        self.assertEqual(self.pedir("/fechar", {"programa": "bloco de notas"}),
                         (500, {"erro": "não encontrei o comando taskkill neste PC"}))
        self.assertIn("detalhe técnico: [WinError 2] The system cannot find the file", self.erros.getvalue())

    # ----- /volume -----

    def test_consultar(self):
        casos = [((35, False), "o volume está em 35%"), ((35, True), "o PC está no mudo; o volume está em 35%")]
        for leitura, descricao in casos:
            with self.subTest(leitura=leitura):
                self.sistema.reset_mock()
                self.sistema.volume_ler.return_value = leitura
                self.assertEqual(self.pedir("/volume", {"acao": "consultar"}),
                                 (200, {"ok": True, "volume": 35, "mudo": leitura[1], "descricao": descricao}))
                self.assertEqual(self.sistema.mock_calls, [mock.call.volume_ler()])

    def test_definir_tambem_tira_do_mudo(self):
        for nivel, esperado in ((60, 60), (0, 0), (100, 100), (35.0, 35)):
            with self.subTest(nivel=nivel):
                self.sistema.reset_mock()
                self.assertEqual(self.pedir("/volume", {"acao": "definir", "nivel": nivel}),
                                 (200, {"ok": True, "volume": esperado, "mudo": False,
                                        "descricao": f"deixei o volume em {esperado}%"}))
                # Primeiro o nível, depois o mudo: sem soar alto no nível antigo.
                self.assertEqual(self.sistema.mock_calls,
                                 [mock.call.volume_definir(esperado), mock.call.volume_mudo(False)])

    def test_aumentar_tira_do_mudo(self):
        casos = [({"acao": "aumentar"}, (35, True), 45),  # padrão: 10
                 ({"acao": "aumentar", "nivel": 20}, (35, False), 55),
                 ({"acao": "aumentar", "nivel": 20}, (90, False), 100)]  # não passa de 100
        for corpo, leitura, novo in casos:
            with self.subTest(corpo=corpo, leitura=leitura):
                self.sistema.reset_mock()
                self.sistema.volume_ler.return_value = leitura
                self.assertEqual(self.pedir("/volume", corpo),
                                 (200, {"ok": True, "volume": novo, "mudo": False,
                                        "descricao": f"aumentei o volume para {novo}%"}))
                self.assertEqual(self.sistema.mock_calls, [mock.call.volume_ler(), mock.call.volume_definir(novo),
                                                           mock.call.volume_mudo(False)])

    def test_diminuir_nao_mexe_no_mudo(self):
        casos = [({"acao": "diminuir"}, (35, True), 25),
                 ({"acao": "diminuir", "nivel": 5}, (35, False), 30),
                 ({"acao": "diminuir", "nivel": 50}, (20, True), 0)]  # não passa de 0
        for corpo, leitura, novo in casos:
            with self.subTest(corpo=corpo, leitura=leitura):
                self.sistema.reset_mock()
                self.sistema.volume_ler.return_value = leitura
                self.assertEqual(self.pedir("/volume", corpo),
                                 (200, {"ok": True, "volume": novo, "mudo": leitura[1],
                                        "descricao": f"diminuí o volume para {novo}%"}))
                self.assertEqual(self.sistema.mock_calls, [mock.call.volume_ler(), mock.call.volume_definir(novo)])

    def test_mudo_e_tirar_mudo(self):
        casos = [("mudo", (35, False), True, "coloquei o PC no mudo"),
                 ("tirar_mudo", (35, True), False, "tirei o PC do mudo; o volume está em 35%")]
        for acao, leitura, mudo, descricao in casos:
            with self.subTest(acao=acao):
                self.sistema.reset_mock()
                self.sistema.volume_ler.return_value = leitura
                self.assertEqual(self.pedir("/volume", {"acao": acao, "nivel": 80}),  # o nível é ignorado aqui
                                 (200, {"ok": True, "volume": 35, "mudo": mudo, "descricao": descricao}))
                self.assertEqual(self.sistema.mock_calls, [mock.call.volume_ler(), mock.call.volume_mudo(mudo)])

    def test_nivel_invalido(self):
        definir = "para definir o volume, diga um nível de 0 a 100"
        casos = [
            ({"acao": "definir"}, definir), ({"acao": "definir", "nivel": None}, definir),
            ({"acao": "definir", "nivel": -1}, definir), ({"acao": "definir", "nivel": 101}, definir),
            ({"acao": "definir", "nivel": True}, definir), ({"acao": "definir", "nivel": "50"}, definir),
            ({"acao": "definir", "nivel": 50.5}, definir), ({"acao": "definir", "nivel": float("nan")}, definir),
            ({"acao": "definir", "nivel": [50]}, definir),
            ({"acao": "aumentar", "nivel": 0}, "diga quanto aumentar o volume, de 1 a 100"),
            ({"acao": "aumentar", "nivel": 101}, "diga quanto aumentar o volume, de 1 a 100"),
            ({"acao": "aumentar", "nivel": False}, "diga quanto aumentar o volume, de 1 a 100"),
            ({"acao": "diminuir", "nivel": "10"}, "diga quanto diminuir o volume, de 1 a 100"),
            ({"acao": "diminuir", "nivel": -10}, "diga quanto diminuir o volume, de 1 a 100"),
        ]
        for corpo, mensagem in casos:
            with self.subTest(corpo=corpo):
                self.assertEqual(self.pedir("/volume", corpo), (400, {"erro": mensagem}))
        self.assertEqual(self.sistema.mock_calls, [])

    def test_acao_de_volume_invalida(self):
        erro = {"erro": "ação de volume inválida; use consultar, definir, aumentar, diminuir, mudo ou tirar_mudo"}
        for corpo in ({}, {"acao": "gritar"}, {"acao": None}, {"acao": 5}, {"acao": ["mudo"]}, {"acao": "MUDO"}):
            with self.subTest(corpo=corpo):
                self.assertEqual(self.pedir("/volume", corpo), (400, erro))
        self.assertEqual(self.sistema.mock_calls, [])

    def test_volume_com_erro_do_sistema(self):
        self.sistema.volume_ler.side_effect = self.erro_no_sistema("o PC não tem uma saída de som ativa",
                                                                   "Core Audio HRESULT 0x80070490")
        for acao in ("consultar", "aumentar", "mudo"):
            with self.subTest(acao=acao):
                self.assertEqual(self.pedir("/volume", {"acao": acao}),
                                 (500, {"erro": "o PC não tem uma saída de som ativa"}))
        self.assertEqual(self.sistema.mock_calls, [mock.call.volume_ler()] * 3)  # nada mudou depois da falha
        self.assertIn("Core Audio HRESULT 0x80070490", self.erros.getvalue())

    def test_volume_desligado_no_config(self):
        self.assertEqual(self.pedir("/volume", {"acao": "consultar"}, servidor=self.servidor_travado),
                         (403, {"erro": "o controle de volume está desligado no config_agente.json do PC"}))
        self.assertEqual(self.sistema.mock_calls, [])
        self.sistema.volume_ler.return_value = (35, False)  # no padrão (sem "acoes"), o volume vale
        self.assertEqual(self.pedir("/volume", {"acao": "consultar"}, servidor=self.servidor_padrao)[0], 200)

    # ----- /energia -----

    def test_bloquear(self):
        self.assertEqual(self.pedir("/energia", {"acao": "bloquear"}),
                         (200, {"ok": True, "acao": "bloquear", "descricao": "bloqueei a tela"}))
        self.assertEqual(self.sistema.mock_calls, [mock.call.bloquear()])

    def test_desligar(self):
        self.sistema.desligar.return_value = "30 segundos"
        self.assertEqual(self.pedir("/energia", {"acao": "desligar"}),
                         (200, {"ok": True, "acao": "desligar", "prazo": "30 segundos",
                                "descricao": "o PC vai desligar em 30 segundos; para cancelar, diga cancela o "
                                             "desligamento"}))
        self.assertEqual(self.sistema.mock_calls, [mock.call.desligar()])

    def test_cancelar(self):
        self.sistema.cancelar_desligamento.return_value = True
        self.assertEqual(self.pedir("/energia", {"acao": "cancelar"}),
                         (200, {"ok": True, "acao": "cancelar", "descricao": "cancelei o desligamento"}))
        self.sistema.cancelar_desligamento.return_value = False
        self.assertEqual(self.pedir("/energia", {"acao": "cancelar"}),
                         (409, {"erro": "não havia desligamento agendado"}))
        self.assertEqual(self.sistema.mock_calls, [mock.call.cancelar_desligamento()] * 2)

    def test_energia_com_erro_do_sistema(self):
        casos = [("bloquear", "o Windows não deixou bloquear a tela"),
                 ("desligar", "já existe um desligamento agendado"),
                 ("cancelar", "o PC não conseguiu cancelar o desligamento")]
        funcoes = {"bloquear": "bloquear", "desligar": "desligar", "cancelar": "cancelar_desligamento"}
        for acao, mensagem in casos:
            with self.subTest(acao=acao):
                getattr(self.sistema, funcoes[acao]).side_effect = self.erro_no_sistema(mensagem, "detalhe")
                self.assertEqual(self.pedir("/energia", {"acao": acao}), (500, {"erro": mensagem}))

    def test_acao_de_energia_invalida(self):
        erro = {"erro": "ação de energia inválida; use bloquear, desligar ou cancelar"}
        for corpo in ({}, {"acao": "reiniciar"}, {"acao": None}, {"acao": 1}, {"acao": ["bloquear"]},
                      {"acao": "BLOQUEAR"}):
            with self.subTest(corpo=corpo):
                self.assertEqual(self.pedir("/energia", corpo), (400, erro))
        self.assertEqual(self.sistema.mock_calls, [])

    def test_energia_nao_liberada_no_config(self):
        desligar = {"erro": "desligar o PC está desligado no config_agente.json"}
        bloquear = {"erro": "bloquear a tela está desligado no config_agente.json"}
        casos = [(self.servidor_padrao, "desligar", desligar), (self.servidor_padrao, "cancelar", desligar),
                 (self.servidor_travado, "bloquear", bloquear), (self.servidor_travado, "desligar", desligar),
                 (self.servidor_travado, "cancelar", desligar)]
        for servidor, acao, erro in casos:
            with self.subTest(acao=acao, servidor=servidor.url):
                self.assertEqual(self.pedir("/energia", {"acao": acao}, servidor=servidor), (403, erro))
        self.assertEqual(self.sistema.mock_calls, [])
        # Sem "acoes" no config, bloquear vale.
        self.assertEqual(self.pedir("/energia", {"acao": "bloquear"}, servidor=self.servidor_padrao)[0], 200)
        self.assertEqual(self.sistema.mock_calls, [mock.call.bloquear()])


if __name__ == "__main__":
    unittest.main()
