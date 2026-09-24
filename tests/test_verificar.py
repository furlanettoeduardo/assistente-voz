"""
Testes do diagnóstico (celular_servidor/verificar.py). O Groq e o LLM são o servidor falso local
e o agente roda de verdade em 127.0.0.1: nenhuma API real é chamada.
"""
import contextlib
import importlib
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.auxiliares import PASTA_SERVIDOR, LLMFalso, copiar_componente, importar_copia, iniciar_agente

try:
    import flask  # noqa: F401
    import requests  # noqa: F401
    DEPENDENCIAS = True
except ImportError:
    DEPENDENCIAS = False

TOKEN = "token-de-teste"
CHAVE = "chave-falsa"
_pastas = itertools.count()


@unittest.skipUnless(DEPENDENCIAS, "instale celular_servidor/requirements.txt para testar o diagnóstico")
class TestVerificar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.agente, cls.servidor_agente = iniciar_agente(
            Path(cls.tmp.name) / "agente", TOKEN, {"calculadora": ["calc.exe"], "navegador": ["firefox"]})
        cls.llm = LLMFalso()

    @classmethod
    def tearDownClass(cls):
        cls.llm.parar()
        cls.servidor_agente.parar()
        cls.tmp.cleanup()
        cls.sem_proxy.stop()

    def setUp(self):
        self.llm.zerar()
        self.config = {"groq_api_key": CHAVE, "stt_model": "whisper-falso", "llm_base_url": self.llm.url,
                       "llm_api_key": CHAVE, "llm_model": "qwen-falso", "pc_url": self.servidor_agente.url,
                       "pc_token": TOKEN, "porta": 8000}

    def verificar(self, config="padrao", **substituir) -> tuple[int, str]:
        """Roda o main() do verificar.py numa cópia da pasta, com o Groq apontando para o falso."""
        pasta = copiar_componente(PASTA_SERVIDOR, Path(self.tmp.name) / f"servidor{next(_pastas)}")
        if config is not None:
            config = self.config if config == "padrao" else config
            (pasta / "config_servidor.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        # O verificar.py faz "import servidor": a cópia desta pasta precisa ser a encontrada.
        sys.path.insert(0, str(pasta))
        self.addCleanup(sys.path.remove, str(pasta))
        sys.modules.pop("servidor", None)
        self.addCleanup(sys.modules.pop, "servidor", None)
        verificar = importar_copia(pasta, "verificar")
        with contextlib.redirect_stdout(io.StringIO()) as saida, contextlib.redirect_stderr(io.StringIO()):
            try:
                importlib.import_module("servidor").GROQ_URL = self.llm.url
            except (SystemExit, ImportError):
                pass  # config inválido ou Flask ausente: o próprio verificar vai explicar
            with contextlib.ExitStack() as pilha:
                for nome, falso in substituir.items():  # troca funções do verificar.py neste teste
                    pilha.enter_context(mock.patch.object(verificar, nome, falso))
                codigo = verificar.executar()
        self.pasta, self.modulo = pasta, verificar
        return codigo, saida.getvalue()

    def test_tudo_certo(self):
        codigo, saida = self.verificar()
        self.assertEqual(codigo, 0, saida)
        self.assertEqual(saida.count("  OK: "), 6, saida)
        self.assertIn("Programas liberados: calculadora, navegador.", saida)
        self.assertIn("Tudo certo!", saida)
        # A chave do Groq foi testada só com a lista de modelos, sem áudio nem chat.
        caminhos = {p["caminho"] for p in self.llm.pedidos}
        self.assertEqual(caminhos, {"/models"})
        self.assertEqual(self.llm.pedidos[0]["cabecalhos"]["Authorization"], f"Bearer {CHAVE}")

    def test_sem_config(self):
        codigo, saida = self.verificar(config=None)
        self.assertEqual(codigo, 2)
        self.assertIn("[2/6] Arquivo de configuração", saida)
        self.assertIn("FALHOU: Arquivo de configuração não encontrado", saida)
        self.assertIn("Copie config_servidor.example.json", saida)

    def test_config_invalido(self):
        codigo, saida = self.verificar(config={**self.config, "pc_url": "192.168.0.10:8765"})
        self.assertEqual(codigo, 2)
        self.assertIn('"pc_url" precisa ser um endereço completo, começando com http://', saida)

    def test_sem_flask(self):
        with mock.patch.dict(sys.modules, {"flask": None}):
            codigo, saida = self.verificar()
        self.assertEqual(codigo, 2)
        self.assertIn("FALHOU: faltam as bibliotecas: flask.", saida)
        caminho = self.modulo.caminho  # ~/... no Termux, caminho completo nos outros casos
        self.assertIn(f"pip install -r {caminho(self.pasta / 'requirements.txt')}", saida)
        self.assertIn(f"bash {caminho(self.pasta.parent / 'scripts' / 'termux-instalar.sh')}", saida)

    def test_valores_de_exemplo(self):
        exemplo = json.loads((PASTA_SERVIDOR / "config_servidor.example.json").read_text(encoding="utf-8"))
        config = {**self.config, "groq_api_key": exemplo["groq_api_key"], "llm_model": exemplo["llm_model"]}
        codigo, saida = self.verificar(config)
        self.assertEqual(codigo, 1)
        self.assertIn("ainda com o valor de exemplo: groq_api_key, llm_model.", saida)
        # Caminho completo: no fluxo do README o terminal fica na raiz, não em celular_servidor.
        arquivo = self.modulo.caminho(self.pasta / "config_servidor.json")
        self.assertIn(f"preencha esses valores em {arquivo} (no Termux: nano {arquivo})", saida)
        self.assertIn("PULADO: preencha groq_api_key primeiro.", saida)
        self.assertIn("PULADO: preencha llm_api_key e llm_model primeiro.", saida)
        self.assertEqual(self.llm.pedidos, [], "não deveria chamar a API com chave de exemplo")

    def test_chave_do_groq_recusada(self):
        self.llm.status_modelos = 401
        codigo, saida = self.verificar()
        self.assertEqual(codigo, 1)
        self.assertIn("FALHOU: o Groq recusou a chave em groq_api_key.", saida)
        self.assertIn("https://console.groq.com/keys", saida)

    def test_llm_inacessivel(self):
        codigo, saida = self.verificar(config={**self.config, "llm_base_url": "http://127.0.0.1:9"})
        self.assertEqual(codigo, 1)
        self.assertIn("FALHOU: sem conexão com a API do LLM em http://127.0.0.1:9.", saida)
        self.assertIn("OLLAMA_HOST=0.0.0.0", saida)

    def test_modelo_de_transcricao_inexistente(self):
        self.llm.modelos = ["whisper-large-v3", "whisper-large-v3-turbo", "qwen-falso"]
        codigo, saida = self.verificar()
        self.assertEqual(codigo, 1)
        self.assertIn("o modelo de transcrição 'whisper-falso' não existe no Groq", saida)
        self.assertIn("whisper-large-v3, whisper-large-v3-turbo", saida)

    def test_modelo_do_llm_inexistente_sugere_os_qwen(self):
        self.llm.modelos = ["whisper-falso", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
        codigo, saida = self.verificar()
        self.assertEqual(codigo, 1)
        self.assertIn("o modelo 'qwen-falso' não existe", saida)
        self.assertIn("troque llm_model por um destes: qwen/qwen3.8-27b", saida)
        self.assertNotIn("gpt-oss", saida)

    def test_modelo_do_ollama_com_latest(self):
        self.llm.modelos = ["whisper-falso", "qwen-falso:latest"]
        codigo, saida = self.verificar()
        self.assertEqual(codigo, 0, saida)

    def test_agente_fora_do_ar(self):
        codigo, saida = self.verificar(config={**self.config, "pc_url": "http://127.0.0.1:9"})
        self.assertEqual(codigo, 1)
        self.assertIn("[5/6] Agente do PC em http://127.0.0.1:9", saida)
        self.assertIn("FALHOU: não consegui conectar ao PC", saida)
        self.assertIn("iniciar_agente.bat", saida)
        self.assertIn("ipconfig", saida)
        self.assertIn("Rede privada", saida)
        self.assertIn("PULADO: depende do passo 5.", saida)

    def test_token_recusado(self):
        codigo, saida = self.verificar(config={**self.config, "pc_token": "outro-token"})
        self.assertEqual(codigo, 1)
        self.assertIn("OK: o agente respondeu.", saida)
        self.assertIn("FALHOU: o agente recusou o pc_token.", saida)

    def test_outro_servico_na_porta_do_agente(self):
        codigo, saida = self.verificar(config={**self.config, "pc_url": self.llm.url})
        self.assertEqual(codigo, 1)
        self.assertIn("mas não parece ser o agente (erro 404)", saida)

    def test_api_que_responde_uma_pagina_web(self):
        self.llm.modelos = None  # 200 com HTML no lugar da lista de modelos
        codigo, saida = self.verificar()
        self.assertEqual(codigo, 1)
        self.assertIn("FALHOU: o Groq respondeu, mas não com a lista de modelos esperada", saida)
        self.assertNotIn("sem conexão", saida)

    def test_erro_inesperado_tem_codigo_proprio(self):
        def quebrar(*args):
            raise RuntimeError("falha de teste")
        codigo, saida = self.verificar(verificar_agente=quebrar)
        self.assertEqual(codigo, 3)
        self.assertIn("O diagnóstico parou por um erro inesperado (detalhe técnico: RuntimeError('falha de teste'))",
                      saida)

    def test_ctrl_c_tem_codigo_proprio(self):
        def interromper(*args):
            raise KeyboardInterrupt
        codigo, saida = self.verificar(verificar_groq=interromper)
        self.assertEqual(codigo, 130)
        self.assertIn("Diagnóstico interrompido.", saida)

    def test_mensagens_so_em_portugues(self):
        # Mesmo com tudo falhando, nenhuma mensagem crua em inglês chega à tela.
        self.llm.status_modelos = 401
        codigo, saida = self.verificar(config={**self.config, "pc_url": "http://127.0.0.1:9",
                                                "llm_base_url": "http://127.0.0.1:9"})
        for ingles in ("Traceback", "Error", "Connection", "refused", "Max retries", "HTTPConnectionPool"):
            self.assertNotIn(ingles, saida)


if __name__ == "__main__":
    unittest.main()
