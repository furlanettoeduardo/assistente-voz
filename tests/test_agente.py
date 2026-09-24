"""Testes do agente do PC. Usam só a biblioteca padrão, como o próprio agente."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
        self.assertEqual(self.pedir("POST", "/abrir", corpo=b"isto nao e json"),
                         (400, {"erro": "JSON inválido"}))

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

    def test_token_de_exemplo_nao_inicia(self):
        self.gravar_config((PASTA_AGENTE / "config_agente.example.json").read_text(encoding="utf-8"))
        saida = rodar_agente(self.pasta)
        self.assertEqual(saida.returncode, 1)
        self.assertIn("Defina um token", saida.stderr)

    def test_aceita_bom_do_bloco_de_notas(self):
        config = {"token": TOKEN, "porta": 0, "programas": {}}
        (self.pasta / "config_agente.json").write_text(json.dumps(config), encoding="utf-8-sig")
        self.assertEqual(importar_copia(self.pasta, "agente").TOKEN, TOKEN)

    def test_exemplo_tem_as_chaves_que_o_codigo_usa(self):
        exemplo = json.loads((PASTA_AGENTE / "config_agente.example.json").read_text(encoding="utf-8"))
        self.assertLessEqual({"token", "porta", "programas"}, set(exemplo))
        for nome, comando in exemplo["programas"].items():
            with self.subTest(nome=nome):
                self.assertIsInstance(comando, list)
                self.assertTrue(all(isinstance(parte, str) for parte in comando))


if __name__ == "__main__":
    unittest.main()
