"""Testes do agente do PC. Usam só a biblioteca padrão, como o próprio agente."""
import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tests.auxiliares import (PASTA_AGENTE, comando_de_teste, copiar_componente, esperar_arquivo,
                              importar_copia, iniciar_agente, vigiar_popen)

TOKEN = "token-de-teste"
SEM_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rodar_agente(pasta: Path) -> subprocess.CompletedProcess:
    """Roda `python agente.py` numa pasta e devolve a saída, com timeout para não travar."""
    return subprocess.run(
        [sys.executable, str(pasta / "agente.py")], cwd=pasta, capture_output=True,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30,
    )


class TestAgenteHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.pasta = Path(cls.tmp.name)
        cls.marcador = cls.pasta / "programa-aberto.txt"
        cls.agente, cls.servidor = iniciar_agente(
            cls.pasta / "agente", TOKEN, {"programa de teste": comando_de_teste(cls.marcador)})

    @classmethod
    def tearDownClass(cls):
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
        self.assertEqual(self.pedir("GET", "/programas"), (200, {"programas": ["programa de teste"]}))

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
        for caminho, token, esperado in [("/abrir", "errado", b" 401 "), ("/programas", TOKEN, b" 404 ")]:
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
        popen = vigiar_popen(self, self.agente)
        with mock.patch.object(self.agente.Handler, "timeout", 0.5):
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

    def test_rota_desconhecida(self):
        self.assertEqual(self.pedir("GET", "/abrir")[0], 404)
        self.assertEqual(self.pedir("POST", "/programas", corpo={})[0], 404)


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

    def test_json_quebrado_encerra_com_linha_e_coluna(self):
        self.gravar_config('{"token": "C:\\Pasta"}')
        saida = rodar_agente(self.pasta)
        self.assertEqual(saida.returncode, 1)
        self.assertIn("linha 1, coluna", saida.stderr)
        self.assertNotIn("Traceback", saida.stderr)

    def test_token_de_exemplo_vazio_ou_com_acento_nao_inicia(self):
        exemplo = json.loads((PASTA_AGENTE / "config_agente.example.json").read_text(encoding="utf-8"))
        # Com token vazio, um pedido sem cabeçalho Authorization seria aceito.
        for token in (exemplo["token"], "", "   ", None, "senha-ção"):
            with self.subTest(token=token):
                self.gravar_config(json.dumps({**exemplo, "token": token}, ensure_ascii=False))
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

    def test_aceita_bom_do_bloco_de_notas(self):
        config = {"token": TOKEN, "porta": 0, "programas": {}}
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
            self.assertEqual(json.loads(r.read()), {"programas": ["bloco de notas", "vs code"]})

        with mock.patch.object(agente.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            for pedido in ("vs code", "VS Code", "  bloco de   notas"):
                with self.subTest(pedido=pedido):
                    corpo = json.dumps({"programa": pedido}).encode("utf-8")
                    with SEM_PROXY.open(urllib.request.Request(servidor.url + "/abrir", data=corpo,
                                                               headers=cabecalhos), timeout=10) as r:
                        self.assertEqual(r.status, 200)
        self.assertEqual(popen.call_count, 3)

    def test_exemplo_tem_as_chaves_que_o_codigo_usa(self):
        exemplo = json.loads((PASTA_AGENTE / "config_agente.example.json").read_text(encoding="utf-8"))
        self.assertLessEqual({"token", "porta", "programas"}, set(exemplo))
        for nome, comando in exemplo["programas"].items():
            with self.subTest(nome=nome):
                self.assertIsInstance(comando, list)
                self.assertTrue(all(isinstance(parte, str) for parte in comando))


if __name__ == "__main__":
    unittest.main()
