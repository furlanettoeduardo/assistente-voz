"""
Testes do servidor do celular. O LLM e a transcrição são um servidor falso local e o agente
roda de verdade em 127.0.0.1, então o fluxo completo passa por HTTP sem chamar nenhuma API real.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.auxiliares import (PASTA_SERVIDOR, LLMFalso, carregar_componente, comando_de_teste,
                              copiar_componente, esperar_arquivo, iniciar_agente, resposta_ferramenta,
                              resposta_texto, vigiar_popen)

try:
    import flask  # noqa: F401
    import requests  # noqa: F401
    DEPENDENCIAS = True
except ImportError:
    DEPENDENCIAS = False

PRECISA_DEPENDENCIAS = unittest.skipUnless(
    DEPENDENCIAS, "instale celular_servidor/requirements.txt para testar o servidor")

TOKEN = "token-de-teste"
CHAVE_FALSA = "chave-falsa"
PROGRAMA = "programa de teste"


def carregar_servidor(destino: Path, **config):
    base = {
        "groq_api_key": CHAVE_FALSA, "stt_model": "whisper-falso",
        "llm_base_url": "http://127.0.0.1:9", "llm_api_key": CHAVE_FALSA, "llm_model": "qwen-falso",
        "pc_url": "http://127.0.0.1:9", "pc_token": TOKEN,
    }
    servidor = carregar_componente(PASTA_SERVIDOR, "servidor", "config_servidor.json",
                                   {**base, **config}, destino)
    servidor.app.testing = True
    return servidor


@PRECISA_DEPENDENCIAS
class TestLimparThink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.limpar = staticmethod(carregar_servidor(Path(cls.tmp.name) / "servidor").limpar)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_remove_bloco_think(self):
        self.assertEqual(self.limpar("<think>vou abrir o chrome</think>Abrindo o Chrome."),
                         "Abrindo o Chrome.")

    def test_remove_bloco_com_varias_linhas(self):
        texto = "<think>\nO usuário quer a calculadora.\nVou chamar a ferramenta.\n</think>\n\nPronto."
        self.assertEqual(self.limpar(texto), "Pronto.")

    def test_remove_varios_blocos(self):
        self.assertEqual(self.limpar("<think>a</think>Oi, <think>b</think>tudo bem?"), "Oi, tudo bem?")

    def test_mantem_texto_sem_think(self):
        self.assertEqual(self.limpar("  Abri a calculadora.  "), "Abri a calculadora.")

    def test_so_raciocinio_vira_vazio(self):
        self.assertEqual(self.limpar("<think>só pensei</think>"), "")

    def test_conteudo_nulo_vira_vazio(self):
        self.assertEqual(self.limpar(None), "")

    def test_remove_raciocinio_sem_abertura(self):
        # Quando o template do modelo já abre o <think>, a resposta traz só o fechamento.
        self.assertEqual(self.limpar("O usuário quer a calculadora.\n</think>\n\nAbrindo a calculadora."),
                         "Abrindo a calculadora.")

    def test_remove_raciocinio_sem_fechamento(self):
        self.assertEqual(self.limpar("Certo. <think>\nO usuário quer a calc"), "Certo.")


@PRECISA_DEPENDENCIAS
class TestServidor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # requests respeita HTTP(S)_PROXY; os testes só falam com 127.0.0.1
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        pasta = Path(cls.tmp.name)
        cls.marcador = pasta / "programa-aberto.txt"
        cls.agente, cls.servidor_agente = iniciar_agente(
            pasta / "agente", TOKEN, {PROGRAMA: comando_de_teste(cls.marcador)})
        cls.llm = LLMFalso()
        cls.servidor = carregar_servidor(pasta / "servidor", llm_base_url=cls.llm.url + "/",
                                         pc_url=cls.servidor_agente.url + "/")

    @classmethod
    def tearDownClass(cls):
        cls.llm.parar()
        cls.servidor_agente.parar()
        cls.tmp.cleanup()
        cls.sem_proxy.stop()

    def setUp(self):
        self.marcador.unlink(missing_ok=True)
        self.llm.programar()  # zera respostas e pedidos do teste anterior
        self.cliente = self.servidor.app.test_client()
        self.popen = vigiar_popen(self, self.agente)
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))  # "[agente] abriu: ..."

    def enviar_texto(self, texto: str):
        return self.cliente.post("/texto", json={"texto": texto})

    def test_fluxo_completo_com_tool_call_abre_o_programa(self):
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA},
                                conteudo="<think>O usuário quer o programa de teste.</think>"),
            resposta_texto("<think>\nDeu certo.\n</think>\nPronto, abri o programa de teste."),
        )
        r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json(), {
            "transcricao": "abre o programa de teste",
            "resposta": "Pronto, abri o programa de teste.",
            "acoes": [{"ferramenta": "abrir_programa", "argumentos": {"nome": PROGRAMA},
                       "resultado": {"ok": True, "programa": PROGRAMA}}],
        })
        self.assertTrue(esperar_arquivo(self.marcador), "o programa de teste não chegou a rodar")
        self.popen.assert_called_once()
        self.assertFalse(self.popen.call_args.kwargs.get("shell", False))

        primeiro, segundo = (p["json"] for p in self.llm.pedidos_de_chat())
        self.assertEqual(self.llm.pedidos_de_chat()[0]["cabecalhos"]["Authorization"],
                         f"Bearer {CHAVE_FALSA}")
        self.assertEqual(primeiro["model"], "qwen-falso")
        self.assertEqual(primeiro["tool_choice"], "auto")
        parametros = primeiro["tools"][0]["function"]["parameters"]
        self.assertEqual(parametros["properties"]["nome"]["enum"], [PROGRAMA])
        self.assertEqual(primeiro["messages"][-1], {"role": "user", "content": "abre o programa de teste"})

        chamada, resultado = segundo["messages"][-2:]
        self.assertEqual(chamada["role"], "assistant")
        self.assertEqual(chamada["tool_calls"][0]["id"], "chamada-1")
        self.assertEqual(resultado["role"], "tool")
        self.assertEqual(resultado["tool_call_id"], "chamada-1")
        self.assertEqual(json.loads(resultado["content"]), {"ok": True, "programa": PROGRAMA})

    def test_programa_fora_da_lista_pedido_pelo_llm_e_recusado(self):
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": "cmd"}),
            resposta_texto("Esse programa não está disponível."),
        )
        r = self.enviar_texto("abre o prompt de comando")

        self.assertEqual(r.status_code, 200)
        acao, = r.get_json()["acoes"]
        self.assertEqual(acao["argumentos"], {"nome": "cmd"})
        self.assertIn("não está na lista", acao["resultado"]["erro"])
        self.popen.assert_not_called()
        self.assertFalse(self.marcador.exists())

    def test_ferramenta_desconhecida_nao_executa_nada(self):
        self.llm.programar(
            resposta_ferramenta("chamada-1", "executar_comando", {"comando": "calc.exe"}),
            resposta_texto("Não posso fazer isso."),
        )
        r = self.enviar_texto("roda calc.exe")

        acao, = r.get_json()["acoes"]
        self.assertEqual(acao["resultado"], {"erro": "ferramenta desconhecida: executar_comando"})
        self.popen.assert_not_called()

    def test_token_errado_e_recusado_pelo_agente(self):
        # Mesmo que o LLM invente uma chamada, o agente recusa o token errado.
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA}),
            resposta_texto("Não consegui abrir."),
        )
        with mock.patch.object(self.servidor, "PC_HEADERS", {"Authorization": "Bearer errado"}):
            r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 200)
        acao, = r.get_json()["acoes"]
        self.assertEqual(acao["resultado"], {"erro": "token inválido"})
        self.popen.assert_not_called()
        self.assertFalse(self.marcador.exists())
        # Sem a lista do PC, o LLM nem recebe a ferramenta e é avisado de que o PC está inacessível.
        primeiro = self.llm.pedidos_de_chat()[0]["json"]
        self.assertNotIn("tools", primeiro)
        self.assertIn("inacessível", primeiro["messages"][0]["content"])

    def test_pc_desligado_responde_sem_ferramentas(self):
        self.llm.programar(resposta_texto("O computador está desligado agora."))
        with mock.patch.object(self.servidor, "PC_URL", "http://127.0.0.1:9"):
            r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["acoes"], [])
        self.assertNotIn("tools", self.llm.pedidos_de_chat()[0]["json"])

    def test_para_depois_de_tres_rodadas_de_ferramenta(self):
        chamada = resposta_ferramenta("chamada-1", "abrir_programa", {"nome": "cmd"})
        self.llm.programar(chamada, chamada, chamada)
        r = self.enviar_texto("abre tudo")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["resposta"], "Fiz o que consegui, mas algo não saiu como esperado.")
        self.assertEqual(len(r.get_json()["acoes"]), 3)
        self.assertEqual(len(self.llm.pedidos_de_chat()), 3)

    def test_argumentos_invalidos_do_llm_nao_quebram(self):
        for argumentos in ("{nome: chrome", "null", f'"{PROGRAMA}"', f'["{PROGRAMA}"]', "", None):
            with self.subTest(argumentos=argumentos):
                resposta = resposta_ferramenta("chamada-1", "abrir_programa", {})
                resposta["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = argumentos
                self.llm.programar(resposta, resposta_texto("Não entendi qual programa."))
                r = self.enviar_texto("abre")

                self.assertEqual(r.status_code, 200)
                acao, = r.get_json()["acoes"]
                self.assertEqual(acao["argumentos"], {})
                self.assertIn("não está na lista", acao["resultado"]["erro"])
        self.popen.assert_not_called()

    def test_argumentos_ja_como_objeto(self):
        # A API compatível com OpenAI manda texto JSON, mas alguns backends mandam o objeto pronto.
        resposta = resposta_ferramenta("chamada-1", "abrir_programa", {})
        resposta["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {"nome": PROGRAMA}
        self.llm.programar(resposta, resposta_texto("Abri."))
        r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.get_json()["acoes"][0]["resultado"], {"ok": True, "programa": PROGRAMA})
        self.assertTrue(esperar_arquivo(self.marcador), "o programa de teste não chegou a rodar")

    def test_erro_da_api_do_llm_vira_502(self):
        self.llm.programar({"error": {"message": "model not found"}}, status=404)
        r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 502)
        self.assertIn("Erro na API: 404", r.get_json()["erro"])

    def test_voz_transcreve_e_executa(self):
        self.llm.transcricao = " abre o programa de teste "
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA}),
            resposta_texto("Abri."),
        )
        with mock.patch.object(self.servidor, "GROQ_URL", self.llm.url):
            r = self.cliente.post("/voz", data=b"\x1a\x45\xdf\xa3" + b"\0" * 2000,
                                  content_type="audio/webm;codecs=opus")

        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["transcricao"], "abre o programa de teste")
        self.assertTrue(esperar_arquivo(self.marcador), "o programa de teste não chegou a rodar")
        transcricao = self.llm.pedidos[0]
        self.assertEqual(transcricao["caminho"], "/audio/transcriptions")
        self.assertEqual(transcricao["cabecalhos"]["Authorization"], f"Bearer {CHAVE_FALSA}")
        self.assertIn(b'filename="audio.webm"', transcricao["corpo"])
        self.assertIn(b'name="language"\r\n\r\npt', transcricao["corpo"])

    def test_audio_curto_demais(self):
        r = self.cliente.post("/voz", data=b"123", content_type="audio/webm")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.llm.pedidos, [])

    def test_texto_vazio(self):
        self.assertEqual(self.enviar_texto("   ").status_code, 400)

    def test_pagina_inicial(self):
        r = self.cliente.get("/")
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Segure para falar", r.get_data(as_text=True))


@PRECISA_DEPENDENCIAS
class TestServidorConfig(unittest.TestCase):
    def rodar_servidor(self, config=None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pasta = copiar_componente(PASTA_SERVIDOR, Path(tmp) / "servidor")
            if config is not None:
                (pasta / "config_servidor.json").write_text(json.dumps(config), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(pasta / "servidor.py")], cwd=pasta, capture_output=True,
                text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60,
            )

    def test_sem_config_encerra_pedindo_para_copiar_o_exemplo(self):
        saida = self.rodar_servidor()
        self.assertEqual(saida.returncode, 1)
        self.assertIn("config_servidor.json", saida.stderr)
        self.assertIn("Copie config_servidor.example.json", saida.stderr)
        self.assertNotIn("Traceback", saida.stderr)

    def test_config_sem_chaves_obrigatorias_encerra_listando_as_que_faltam(self):
        saida = self.rodar_servidor({"groq_api_key": CHAVE_FALSA, "stt_model": "whisper-falso"})
        self.assertEqual(saida.returncode, 1)
        self.assertIn("Faltam chaves em", saida.stderr)
        self.assertIn("llm_base_url, llm_api_key, llm_model, pc_url, pc_token", saida.stderr)
        self.assertNotIn("Traceback", saida.stderr)


class TestExemploDeConfigServidor(unittest.TestCase):
    def test_exemplo_tem_as_chaves_que_o_codigo_usa(self):
        exemplo = json.loads((PASTA_SERVIDOR / "config_servidor.example.json").read_text(encoding="utf-8"))
        chaves = {"groq_api_key", "stt_model", "llm_base_url", "llm_api_key", "llm_model",
                  "pc_url", "pc_token", "host", "porta"}
        self.assertLessEqual(chaves, set(exemplo))


if __name__ == "__main__":
    unittest.main()
