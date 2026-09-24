"""
Testes do servidor do celular. O LLM e a transcrição são um servidor falso local e o agente
roda de verdade em 127.0.0.1, então o fluxo completo passa por HTTP sem chamar nenhuma API real.
"""
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from tests.auxiliares import (PASTA_SERVIDOR, LLMFalso, carregar_componente, comando_de_teste,
                              copiar_componente, esperar_arquivo, importar_copia, iniciar_agente,
                              resposta_ferramenta, resposta_texto, vigiar_popen)

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
    servidor.GROQ_URL = "http://127.0.0.1:9"  # nenhum teste chega ao Groq real por esquecimento
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
        self.llm.zerar()
        self.cliente = self.servidor.app.test_client()
        self.popen = vigiar_popen(self, self.agente)
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))  # "[agente] abriu: ..."
        self.terminal = pilha.enter_context(contextlib.redirect_stderr(io.StringIO()))  # avisos do servidor

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
        self.assertIn("o agente do PC recusou o pc_token", self.terminal.getvalue())

    def test_pc_desligado_responde_sem_ferramentas(self):
        self.llm.programar(resposta_texto("O computador está desligado agora."))
        with mock.patch.object(self.servidor, "PC_URL", "http://127.0.0.1:9"):
            r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["acoes"], [])
        self.assertNotIn("tools", self.llm.pedidos_de_chat()[0]["json"])
        self.assertIn("não consegui falar com o agente do PC", self.terminal.getvalue())

    def test_pc_cai_no_meio_do_comando(self):
        # O LLM pede a ferramenta mesmo sem recebê-la; o erro para a página não traz detalhe em inglês.
        self.llm.programar(resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA}),
                           resposta_texto("Não consegui falar com o computador."))
        with mock.patch.object(self.servidor, "PC_URL", "http://127.0.0.1:9"):
            r = self.enviar_texto("abre o programa de teste")

        acao, = r.get_json()["acoes"]
        self.assertEqual(acao["resultado"], {"erro": "não consegui falar com o computador"})
        self.assertIn("detalhe técnico", self.terminal.getvalue())

    def test_ultima_rodada_so_aceita_texto_e_nao_repete_o_programa(self):
        # O LLM falso insiste na mesma chamada nas 3 rodadas, como um backend que ignora tool_choice.
        chamada = resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA})
        self.llm.programar(chamada, chamada, chamada)
        r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["resposta"], f"Pronto, abri {PROGRAMA}.")
        acao, = r.get_json()["acoes"]
        self.assertEqual(acao["resultado"], {"ok": True, "programa": PROGRAMA})
        self.popen.assert_called_once()  # abriu uma vez só
        escolhas = [p["json"]["tool_choice"] for p in self.llm.pedidos_de_chat()]
        self.assertEqual(escolhas, ["auto", "auto", "none"])
        # A chamada repetida recebeu o mesmo resultado, sem ir de novo ao agente.
        ultimo = self.llm.pedidos_de_chat()[2]["json"]["messages"]
        resultados = [json.loads(m["content"]) for m in ultimo if m["role"] == "tool"]
        self.assertEqual(resultados, [{"ok": True, "programa": PROGRAMA}] * 2)

    def test_groq_recusa_a_ultima_rodada_com_400(self):
        # Com tool_choice="none", alguns modelos insistem na ferramenta e o Groq responde 400.
        chamada = resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA})
        falha = {"error": {"message": "Tool choice is none, but model called a tool", "code": "tool_use_failed"}}
        self.llm.programar_com_status((200, chamada), (200, chamada), (400, falha))
        r = self.enviar_texto("abre o programa de teste")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["resposta"], f"Pronto, abri {PROGRAMA}.")
        self.assertEqual(len(r.get_json()["acoes"]), 1)
        self.assertIn("tool_use_failed", self.terminal.getvalue())

    def test_texto_de_ferramenta_ignorada_na_ultima_rodada_nao_chega_ao_usuario(self):
        # Um backend que ignora tool_choice="none" anuncia uma abertura que o servidor não executou.
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA}),
            resposta_ferramenta("chamada-2", "abrir_programa", {"nome": "cmd"}),
            resposta_ferramenta("chamada-3", "abrir_programa", {"nome": "navegador"},
                                conteudo="Pronto, abri o navegador."),
        )
        r = self.enviar_texto("abre tudo")

        self.assertEqual(r.get_json()["resposta"], f"Pronto, abri {PROGRAMA}.")
        self.assertEqual([a["argumentos"]["nome"] for a in r.get_json()["acoes"]], [PROGRAMA, "cmd"])

    def test_falha_do_llm_depois_de_abrir_nao_vira_erro(self):
        # Mandar "tentar de novo" faria o programa abrir duas vezes.
        chamada = resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA})
        for status, corpo in ((500, {"error": {"message": "Internal Server Error"}}),
                              (400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed"}}),
                              (200, {"sem": "choices"})):
            with self.subTest(status=status, corpo=corpo):
                self.llm.programar_com_status((200, chamada), (status, corpo))
                r = self.enviar_texto("abre o programa de teste")

                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.get_json()["resposta"], f"Pronto, abri {PROGRAMA}.")
                self.assertEqual(len(r.get_json()["acoes"]), 1)
        self.assertIn("falhou depois das ações", self.terminal.getvalue())

    def test_nada_aberto_e_sem_texto_final(self):
        chamada = resposta_ferramenta("chamada-1", "abrir_programa", {"nome": "cmd"})
        self.llm.programar(chamada, chamada, chamada)
        r = self.enviar_texto("abre o cmd")
        self.assertEqual(r.get_json()["resposta"], "Fiz o que consegui, mas algo não saiu como esperado.")

    def test_resposta_em_texto_na_ultima_rodada(self):
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA}),
            resposta_ferramenta("chamada-2", "abrir_programa", {"nome": "cmd"}),
            resposta_texto("<think>ok</think>Abri o programa de teste; o cmd não está na lista."),
        )
        r = self.enviar_texto("abre o programa de teste e o cmd")

        self.assertEqual(r.get_json()["resposta"], "Abri o programa de teste; o cmd não está na lista.")
        self.assertEqual([a["argumentos"]["nome"] for a in r.get_json()["acoes"]], [PROGRAMA, "cmd"])
        self.popen.assert_called_once()

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

    def test_erros_da_api_do_llm_viram_mensagens_em_portugues(self):
        casos = [
            (401, "Invalid API Key", "A API do LLM recusou a chave. Confira a chave no config_servidor.json."),
            (404, "The model `x` does not exist",
             "A API do LLM não encontrou o modelo ou o endereço. Confira o modelo e a URL no config."),
            (429, "Rate limit reached",
             "A API do LLM avisou que o limite de uso acabou por enquanto. Espere um pouco e tente de novo."),
            (413, "Request too large for model", "A API do LLM achou o pedido grande demais. Tente um comando mais curto."),
            (400, "The model `x` has been decommissioned",
             "A API do LLM avisou que o modelo configurado saiu do ar. Escolha outro na lista de modelos."),
            (400, "Failed to call a function (tool_use_failed)", "O modelo se confundiu ao usar a ferramenta. Tente de novo."),
            (503, "Service Unavailable", "A API do LLM está com problemas agora. Tente de novo em instantes."),
            (422, "Unprocessable", "A API do LLM recusou o pedido (erro 422). Veja os detalhes no terminal do servidor."),
        ]
        for status, detalhe, mensagem in casos:
            with self.subTest(status=status, detalhe=detalhe):
                self.llm.programar({"error": {"message": detalhe}}, status=status)
                r = self.enviar_texto("abre o programa de teste")

                self.assertEqual((r.status_code, r.get_json()), (502, {"erro": mensagem}))
                self.assertIn(detalhe, self.terminal.getvalue())  # o detalhe original fica no terminal

    def test_sem_conexao_com_o_llm(self):
        with mock.patch.object(self.servidor, "LLM_URL", "http://127.0.0.1:9"):
            r = self.enviar_texto("abre o programa de teste")
        self.assertEqual((r.status_code, r.get_json()),
                         (502, {"erro": "Sem conexão com a API do LLM. Confira a internet do celular e tente de novo."}))

    def test_voz_transcreve_e_executa(self):
        self.llm.transcricao = " abre o programa de teste "
        self.llm.programar(
            resposta_ferramenta("chamada-1", "abrir_programa", {"nome": PROGRAMA}),
            resposta_texto("Abri."),
        )
        r = self.enviar_audio()

        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["transcricao"], "abre o programa de teste")
        self.assertTrue(esperar_arquivo(self.marcador), "o programa de teste não chegou a rodar")
        transcricao = self.llm.pedidos[0]
        self.assertEqual(transcricao["caminho"], "/audio/transcriptions")
        self.assertEqual(transcricao["cabecalhos"]["Authorization"], f"Bearer {CHAVE_FALSA}")
        self.assertIn(b'filename="audio.webm"', transcricao["corpo"])
        self.assertIn(b'name="language"\r\n\r\npt', transcricao["corpo"])

    def enviar_audio(self, content_type: str = "audio/webm;codecs=opus"):
        with mock.patch.object(self.servidor, "GROQ_URL", self.llm.url):
            return self.cliente.post("/voz", data=b"\x1a\x45\xdf\xa3" + b"\0" * 2000, content_type=content_type)

    def test_erro_na_transcricao_vira_502(self):
        self.llm.status_transcricao = 401
        r = self.enviar_audio()

        self.assertEqual((r.status_code, r.get_json()),
                         (502, {"erro": "A API de transcrição recusou a chave. Confira a chave no config_servidor.json."}))
        self.assertEqual(self.llm.pedidos_de_chat(), [])

    def test_timeout_e_resposta_ilegivel_da_api(self):
        casos = [
            (requests.ReadTimeout("read timed out"), "A API do LLM demorou demais para responder. Tente de novo."),
            (requests.ConnectTimeout("connect timed out"),
             "Sem conexão com a API do LLM. Confira a internet do celular e tente de novo."),
            (requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
             "A API do LLM mandou uma resposta que o servidor não entendeu. Tente de novo."),
        ]
        for erro, mensagem in casos:
            with self.subTest(erro=type(erro).__name__):
                self.assertEqual(self.servidor.explicar_erro(erro, "a API do LLM"), mensagem)

    def test_transcricao_vazia(self):
        self.llm.transcricao = "   "
        r = self.enviar_audio()

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json(), {"erro": "Não entendi nada no áudio. Tente de novo."})
        self.assertEqual(self.llm.pedidos_de_chat(), [])

    def test_formato_do_audio_vira_extensao_do_arquivo(self):
        self.llm.transcricao = "oi"
        casos = [("audio/mp4", "audio.m4a"), ("audio/ogg;codecs=opus", "audio.ogg"),
                 ("audio/wav", "audio.wav"), ("application/octet-stream", "audio.webm")]
        for content_type, arquivo in casos:
            with self.subTest(content_type=content_type):
                self.llm.programar(resposta_texto("Oi!"))
                r = self.enviar_audio(content_type)

                self.assertEqual(r.status_code, 200, r.get_json())
                self.assertIn(f'filename="{arquivo}"'.encode("utf-8"), self.llm.pedidos[0]["corpo"])

    def test_audio_curto_demais(self):
        r = self.cliente.post("/voz", data=b"123", content_type="audio/webm")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.llm.pedidos, [])

    def test_texto_vazio(self):
        self.assertEqual(self.enviar_texto("   ").status_code, 400)

    def test_corpo_malformado_em_texto_recebe_400_em_json(self):
        casos = [["abre"], "abre", 5, None, {"texto": None}, {"texto": 5}, {"texto": ["abre"]}, {"outro": "abre"}]
        for corpo in casos:
            with self.subTest(corpo=corpo):
                r = self.cliente.post("/texto", json=corpo)
                self.assertEqual((r.status_code, r.get_json()), (400, {"erro": "Digite um comando."}))
        r = self.cliente.post("/texto", data=b"{quebrado", content_type="application/json")
        self.assertEqual((r.status_code, r.get_json()), (400, {"erro": "Digite um comando."}))
        self.assertEqual(self.llm.pedidos, [])

    def test_erros_do_flask_saem_em_json_e_em_portugues(self):
        casos = [("GET", "/nao-existe", 404, "Endereço não encontrado no servidor."),
                 ("GET", "/texto", 405, "Esse endereço não aceita esse tipo de pedido.")]
        for metodo, caminho, status, mensagem in casos:
            with self.subTest(caminho=caminho):
                r = self.cliente.open(caminho, method=metodo)
                self.assertEqual((r.status_code, r.get_json()), (status, {"erro": mensagem}))

    def test_erro_inesperado_sai_em_json_e_em_portugues(self):
        with mock.patch.object(self.servidor, "conversar", side_effect=RuntimeError("falha de teste")), \
                contextlib.redirect_stderr(io.StringIO()) as terminal:
            r = self.enviar_texto("abre o programa de teste")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(),
                         {"erro": "Erro inesperado no servidor. Veja o terminal do Termux e tente de novo."})
        self.assertIn("RuntimeError: falha de teste", terminal.getvalue())

    def test_pagina_inicial(self):
        r = self.cliente.get("/")
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Segure para falar", r.get_data(as_text=True))


@PRECISA_DEPENDENCIAS
class TestServidorConfig(unittest.TestCase):
    # porta 0: se uma checagem regredir, o servidor sobe numa porta livre em vez de disputar a 8000
    VALIDO = {"groq_api_key": CHAVE_FALSA, "stt_model": "whisper-falso", "llm_base_url": "http://127.0.0.1:9",
              "llm_api_key": CHAVE_FALSA, "llm_model": "qwen-falso", "pc_url": "http://127.0.0.1:9",
              "pc_token": TOKEN, "porta": 0}

    def rodar_servidor(self, config=None) -> subprocess.CompletedProcess:
        """Roda `python servidor.py` com `config` (dict ou bytes crus) e devolve a saída."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pasta = copiar_componente(PASTA_SERVIDOR, Path(tmp) / "servidor")
            if config is not None:
                conteudo = config if isinstance(config, bytes) else json.dumps(config).encode("utf-8")
                (pasta / "config_servidor.json").write_bytes(conteudo)
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

    def test_valores_com_tipo_ou_formato_errado_encerram_com_mensagem(self):
        casos = [
            ({"pc_url": None}, '"pc_url" precisa ser um texto entre aspas'),
            ({"llm_base_url": 8}, '"llm_base_url" precisa ser um texto entre aspas'),
            ({"pc_token": 123}, '"pc_token" precisa ser um texto entre aspas'),
            ({"pc_url": "192.168.0.10:8765"}, '"pc_url" precisa ser um endereço completo, começando com http://'),
            ({"pc_url": "http://192.168.0.10]:8765"}, '"pc_url" precisa ser um endereço completo'),
            ({"pc_url": "http://:8765"}, '"pc_url" precisa ser um endereço completo'),
            ({"pc_url": "http://192.168.0..10:8765"}, '"pc_url" precisa ser um endereço completo'),  # ponto duplo
            ({"pc_url": "http://.192.168.0.10:8765"}, '"pc_url" precisa ser um endereço completo'),
            ({"pc_url": "http://192.168.0.10​:8765"}, '"pc_url" precisa ser um endereço completo'),
            ({"llm_base_url": "https://api..groq.com/openai/v1"}, '"llm_base_url" precisa ser um endereço completo'),
            ({"llm_base_url": "https://api.groq.com:99999/openai/v1"}, '"llm_base_url" precisa ser um endereço completo'),
            ({"porta": ""}, '"porta" precisa ser um número entre 0 e 65535'),
            ({"porta": 70000}, '"porta" precisa ser um número entre 0 e 65535'),
            ({"host": 127}, '"host" precisa ser um texto'),
        ]
        for mudanca, mensagem in casos:
            with self.subTest(mudanca=mudanca):
                saida = self.rodar_servidor({**self.VALIDO, **mudanca})
                self.assertEqual(saida.returncode, 1)
                self.assertIn(mensagem, saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)

    def test_chave_com_caractere_invalido_ou_vazia_encerra_com_mensagem(self):
        # Esses valores iam para o cabeçalho Authorization e davam UnicodeEncodeError em cada pedido.
        casos = [
            ({"pc_token": "\u201ctoken-colado\u201d"}, '"pc_token" tem um caractere que não pode ir numa chave'),
            ({"llm_api_key": "gsk_abc\u200b"}, '"llm_api_key" tem um caractere que não pode ir numa chave'),
            ({"groq_api_key": "chave-com-acentuação"}, '"groq_api_key" tem um caractere que não pode ir numa chave'),
            ({"pc_token": "   "}, '"pc_token" está vazio'),
        ]
        for mudanca, mensagem in casos:
            with self.subTest(mudanca=mudanca):
                saida = self.rodar_servidor({**self.VALIDO, **mudanca})
                self.assertEqual(saida.returncode, 1)
                self.assertIn(mensagem, saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)
                self.assertNotIn("token-colado", saida.stderr, "a mensagem não deve repetir o segredo")

    def test_sem_as_bibliotecas_explica_como_instalar(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                mock.patch.dict(sys.modules, {"requests": None}), self.assertRaises(SystemExit) as saida:
            carregar_servidor(Path(tmp) / "servidor")
        self.assertIn("Falta a biblioteca requests.", str(saida.exception.code))
        self.assertIn("pip install -r requirements.txt", str(saida.exception.code))

    def test_espacos_em_volta_das_chaves_sao_removidos(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            servidor = carregar_servidor(Path(tmp) / "servidor", pc_token=f"  {TOKEN} ", llm_api_key=" chave\n")
        self.assertEqual(servidor.PC_HEADERS, {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(servidor.LLM_HEADERS, {"Authorization": "Bearer chave"})

    def test_inicio_mostra_so_mensagens_em_portugues_e_serve_a_pagina(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pasta = copiar_componente(PASTA_SERVIDOR, Path(tmp) / "servidor")
            (pasta / "config_servidor.json").write_text(json.dumps(self.VALIDO), encoding="utf-8")
            processo = subprocess.Popen(
                [sys.executable, "-u", str(pasta / "servidor.py")], cwd=pasta, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            linhas = []
            leitor = threading.Thread(target=lambda: linhas.extend(processo.stdout), daemon=True)
            leitor.start()
            try:
                limite = time.monotonic() + 30
                while not any("[servidor] pronto" in linha for linha in linhas) and time.monotonic() < limite:
                    self.assertIsNone(processo.poll(), "".join(linhas))
                    time.sleep(0.05)
                pronto = next(linha for linha in linhas if "[servidor] pronto" in linha)
                url = re.search(r"http://localhost:\d+", pronto).group(0)
                sem_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with sem_proxy.open(url, timeout=10) as r:
                    self.assertIn("Segure para falar", r.read().decode("utf-8"))
            finally:
                processo.terminate()
                processo.wait(timeout=15)
                leitor.join(timeout=5)
                processo.stdout.close()
        saida = "".join(linhas)
        self.assertIn("[servidor] para parar, aperte Ctrl+C.", saida)
        for ingles in ("Serving Flask app", "development server", "Running on", "GET / HTTP"):
            self.assertNotIn(ingles, saida)

    def test_porta_ocupada_encerra_com_mensagem(self):
        # No Windows, o SO_REUSEADDR padrão do Werkzeug deixava o segundo servidor subir sem erro.
        with socket.socket() as ocupada:
            ocupada.bind(("127.0.0.1", 0))
            ocupada.listen()
            saida = self.rodar_servidor({**self.VALIDO, "porta": ocupada.getsockname()[1]})
        self.assertEqual(saida.returncode, 1)
        self.assertIn("Não consegui escutar em 127.0.0.1:", saida.stderr)
        self.assertIn('Outro servidor já usa essa porta: feche-o ou troque "porta"', saida.stderr)
        self.assertNotIn("is in use by another program", saida.stderr)  # aviso do Werkzeug, em inglês
        self.assertNotIn("Traceback", saida.stderr)

    def test_host_que_nao_e_deste_aparelho(self):
        saida = self.rodar_servidor({**self.VALIDO, "host": "192.0.2.123"})  # faixa reservada para exemplos
        self.assertEqual(saida.returncode, 1)
        self.assertIn('"192.0.2.123" não é um endereço deste aparelho', saida.stderr)
        self.assertNotIn("Traceback", saida.stderr)

    def test_config_invalido_encerra_com_mensagem(self):
        com_acento = {"groq_api_key": "chave-música"}
        casos = [
            (b'{"pc_url": "http://192.168.0.10:8765", }', "vírgula sobrando antes do }"),  # coluna muda no 3.13
            (b"[]", "precisa ser um objeto JSON"),
            (json.dumps(com_acento, ensure_ascii=False).encode("cp1252"), "não está em UTF-8"),  # ANSI
            (json.dumps(com_acento, ensure_ascii=False).encode("utf-16"), "não está em UTF-8"),
        ]
        for conteudo, mensagem in casos:
            with self.subTest(mensagem=mensagem):
                saida = self.rodar_servidor(conteudo)
                self.assertEqual(saida.returncode, 1)
                self.assertIn(mensagem, saida.stderr)
                self.assertNotIn("Traceback", saida.stderr)

    def test_aceita_bom_do_bloco_de_notas(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pasta = copiar_componente(PASTA_SERVIDOR, Path(tmp) / "servidor")
            exemplo = (PASTA_SERVIDOR / "config_servidor.example.json").read_text(encoding="utf-8")
            (pasta / "config_servidor.json").write_text(exemplo, encoding="utf-8-sig")
            self.assertEqual(importar_copia(pasta, "servidor").CFG["porta"], 8000)


class TestExemploDeConfigServidor(unittest.TestCase):
    def test_exemplo_tem_as_chaves_que_o_codigo_usa(self):
        exemplo = json.loads((PASTA_SERVIDOR / "config_servidor.example.json").read_text(encoding="utf-8"))
        chaves = {"groq_api_key", "stt_model", "llm_base_url", "llm_api_key", "llm_model",
                  "pc_url", "pc_token", "host", "porta"}
        self.assertLessEqual(chaves, set(exemplo))


if __name__ == "__main__":
    unittest.main()
