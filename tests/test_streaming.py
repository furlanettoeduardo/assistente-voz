"""
Testes da resposta em streaming (NDJSON) do servidor: quem manda Accept: application/x-ndjson no /voz ou
no /texto recebe uma linha JSON por evento (transcricao, resposta, um audio por frase e fim, ou erro).
O LLM e a transcrição são o servidor falso local, a voz usa o Piper falso de test_voz.py e o PC fica
"desligado", sem rede. Nenhum teste toca áudio.
"""
import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.auxiliares import LLMFalso, resposta_ferramenta, resposta_texto
from tests.test_servidor import PRECISA_DEPENDENCIAS, LampadaDeMentira, carregar_servidor
from tests.test_voz import NOME, PiperFalso, amostras_da_frase, criar_arquivos, ler_wav, voz

try:
    import requests
    from werkzeug.serving import WSGIRequestHandler, make_server

    class RequisicaoSilenciosa(WSGIRequestHandler):
        """O servidor do Werkzeug sem a linha de log de cada pedido."""

        def log(self, *args, **kwargs):
            pass
except ImportError:  # sem Flask, os testes aparecem como skipped (PRECISA_DEPENDENCIAS)
    pass

NDJSON = "application/x-ndjson"
PC_DESLIGADO = {"programas": [], "fechaveis": [], "acoes": [], "fora_do_ar": True}
ERRO_INESPERADO = "Erro inesperado no servidor. Veja o terminal do Termux e tente de novo."
RESPOSTA = "Pronto, abri o Spotify. São 10h33! Quer mais alguma coisa?"
# O texto que o Piper recebe, dividido em frases como ele divide.
FRASES = ["Pronto, abri o Spotify.", "São 10 horas e 33!", "Quer mais alguma coisa?"]


@PRECISA_DEPENDENCIAS
class TestStreaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.llm = LLMFalso()
        cls.servidor = carregar_servidor(Path(cls.tmp.name) / "servidor", llm_base_url=cls.llm.url)

    @classmethod
    def tearDownClass(cls):
        cls.llm.parar()
        cls.tmp.cleanup()
        cls.sem_proxy.stop()

    def setUp(self):
        self.llm.zerar()
        self.servidor._memorias.clear()
        self.servidor._pendentes.clear()
        self.cliente = self.servidor.app.test_client()
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        # O PC fica "desligado" sem ir à rede: no Windows, conectar numa porta fechada leva 1 s ou mais.
        pilha.enter_context(mock.patch.object(self.servidor, "consultar_pc",
                                              side_effect=lambda *a, **k: dict(PC_DESLIGADO)))
        pilha.enter_context(mock.patch.object(self.servidor, "GROQ_URL", self.llm.url))  # a transcrição falsa
        self.saida = pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))  # a linha de tempos
        self.terminal = pilha.enter_context(contextlib.redirect_stderr(io.StringIO()))

    # ---------- auxiliares ----------

    def com_voz(self) -> PiperFalso:
        piper = PiperFalso()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        criar_arquivos(Path(tmp.name))
        patcher = mock.patch.object(self.servidor, "VOZ", voz.Voz(NOME, Path(tmp.name), carregar=lambda arquivo: piper))
        patcher.start()
        self.addCleanup(patcher.stop)
        return piper

    def texto(self, texto: str = "abre o Spotify", accept: str | None = NDJSON, **extra):
        """POST /texto; o corpo da resposta é lido aqui, com os mocks do teste ainda valendo."""
        headers = {"Accept": accept} if accept else {}
        r = self.cliente.post("/texto", json={"texto": texto, **extra}, headers=headers)
        r.get_data()
        r.close()
        return r

    def audio(self, accept: str | None = NDJSON, dados: bytes = b"\x1a\x45\xdf\xa3" + b"\0" * 2000, **headers):
        if accept:
            headers["Accept"] = accept
        r = self.cliente.post("/voz", data=dados, content_type="audio/webm;codecs=opus", headers=headers)
        r.get_data()
        r.close()
        return r

    def eventos(self, r) -> list[dict]:
        """Os eventos de uma resposta em streaming, conferindo o formato: uma linha JSON por evento."""
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "application/x-ndjson; charset=utf-8")
        self.assertIn("Accept", r.vary)
        self.assertNotIn("Content-Length", r.headers)  # o Werkzeug manda em partes (chunked)
        corpo = r.get_data()
        self.assertTrue(corpo.endswith(b"\n"), corpo[-100:])
        linhas = corpo.decode("utf-8").split("\n")[:-1]
        self.assertNotIn("", linhas)
        return [json.loads(linha) for linha in linhas]

    def assertJSON(self, r, status: int, dados: dict):
        self.assertEqual((r.status_code, r.get_json()), (status, dados))
        self.assertEqual(r.mimetype, "application/json")
        self.assertIn("Accept", r.vary)

    # ---------- o streaming ----------

    def test_eventos_na_ordem_com_um_audio_por_frase(self):
        piper = self.com_voz()
        self.llm.programar(resposta_texto(RESPOSTA))
        r = self.texto("abre o Spotify e diz que horas são")
        eventos = self.eventos(r)

        self.assertEqual([e["tipo"] for e in eventos], ["transcricao", "resposta", "audio", "audio", "audio", "fim"])
        self.assertEqual(eventos[0], {"tipo": "transcricao", "texto": "abre o Spotify e diz que horas são"})
        self.assertEqual(eventos[1], {"tipo": "resposta", "texto": RESPOSTA, "acoes": []})
        self.assertEqual(eventos[-1], {"tipo": "fim"})
        audios = [base64.b64decode(e["audio"], validate=True) for e in eventos[2:5]]
        self.assertEqual([set(e) for e in eventos[2:5]], [{"tipo", "audio"}] * 3)
        # Cada áudio é um WAV completo (16 bits, mono, taxa da voz), na ordem das frases.
        self.assertEqual([ler_wav(dados) for dados in audios], [(1, 2, 22050, amostras_da_frase(f)) for f in FRASES])
        self.assertEqual(piper.textos[-1], " ".join(FRASES))
        self.assertIn("São 10h33".encode("utf-8"), r.get_data())  # o acento vai em UTF-8, sem o escape ã

    def test_sem_voz_nao_manda_audio(self):
        self.assertIsNone(self.servidor.VOZ)
        self.llm.programar(resposta_texto(RESPOSTA))
        eventos = self.eventos(self.texto())
        self.assertEqual([e["tipo"] for e in eventos], ["transcricao", "resposta", "fim"])

    def test_resposta_vazia_nao_manda_audio(self):
        self.com_voz()
        self.llm.programar(resposta_texto("<think>só pensei</think>"))
        eventos = self.eventos(self.texto())
        self.assertEqual([e["tipo"] for e in eventos], ["transcricao", "resposta", "fim"])
        self.assertEqual(eventos[1]["texto"], "")

    def test_resposta_traz_os_mesmos_campos_do_json(self):
        lampada = mock.patch.object(self.servidor, "LAMPADA", LampadaDeMentira())
        lampada.start()
        self.addCleanup(lampada.stop)
        chamada = resposta_ferramenta("c1", "controlar_lampada", {"cor": "azul"})
        self.llm.programar(chamada)
        esperado = self.texto("deixa a luz azul", accept=None).get_json()
        self.llm.programar(chamada)
        eventos = self.eventos(self.texto("deixa a luz azul"))

        self.assertEqual(esperado["acoes"], [{"ferramenta": "controlar_lampada", "argumentos": {"cor": "azul"},
                                              "resultado": {"ok": True, "descricao": "deixei a lâmpada em azul"}}])
        self.assertEqual(eventos[0], {"tipo": "transcricao", "texto": esperado["transcricao"]})
        self.assertEqual(eventos[1], {"tipo": "resposta", "texto": esperado["resposta"], "acoes": esperado["acoes"]})

    def test_falha_do_llm_vira_evento_erro(self):
        casos = [(401, "A API do LLM recusou a chave. Confira a chave no config_servidor.json."),
                 (429, "A API do LLM avisou que o limite de uso acabou por enquanto. Espere um pouco e tente de novo."),
                 (503, "A API do LLM está com problemas agora. Tente de novo em instantes.")]
        for status, mensagem in casos:
            with self.subTest(status=status):
                self.llm.programar({"error": {"message": f"erro {status} em inglês"}}, status=status)
                eventos = self.eventos(self.texto("abre o Spotify"))
                self.assertEqual(eventos, [{"tipo": "transcricao", "texto": "abre o Spotify"},
                                           {"tipo": "erro", "erro": mensagem}])
                self.assertIn(f"erro {status} em inglês", self.terminal.getvalue())  # o detalhe fica no terminal
        self.assertEqual(self.servidor.lembrar("padrao"), [])  # a falha não entra na memória

    def test_sem_conexao_com_o_llm_vira_evento_erro(self):
        erro = requests.ConnectionError("recusou")
        with mock.patch.object(self.servidor.SESSAO, "post", side_effect=erro):
            eventos = self.eventos(self.texto())
        self.assertEqual(eventos[1:], [{"tipo": "erro", "erro": "Sem conexão com a API do LLM. Confira a internet "
                                                                  "do celular e tente de novo."}])

    def test_voz_que_falha_no_meio_segue_ate_o_fim(self):
        piper = self.com_voz()
        piper.erro_na_frase = 1
        self.llm.programar(resposta_texto(RESPOSTA))
        eventos = self.eventos(self.texto())
        self.assertEqual([e["tipo"] for e in eventos], ["transcricao", "resposta", "audio", "fim"])
        self.assertEqual(ler_wav(base64.b64decode(eventos[2]["audio"]))[3], amostras_da_frase(FRASES[0]))
        self.assertIn("a voz do servidor falhou depois de 1 frase; as seguintes ficam sem a voz do servidor",
                      self.terminal.getvalue())
        self.assertIn("onnx quebrou na frase 1", self.terminal.getvalue())

    def test_voz_que_falha_logo_na_primeira_frase(self):
        piper = self.com_voz()
        piper.erro_na_sintese = RuntimeError("onnx quebrou")
        self.llm.programar(resposta_texto(RESPOSTA))
        eventos = self.eventos(self.texto())
        self.assertEqual([e["tipo"] for e in eventos], ["transcricao", "resposta", "fim"])
        self.assertIn("a voz do servidor falhou; a página usa a do navegador", self.terminal.getvalue())

    def test_excecao_inesperada_vira_evento_erro(self):
        with mock.patch.object(self.servidor, "conversar", side_effect=RuntimeError("falha de teste")):
            eventos = self.eventos(self.texto("abre o Spotify"))
        self.assertEqual(eventos, [{"tipo": "transcricao", "texto": "abre o Spotify"},
                                   {"tipo": "erro", "erro": ERRO_INESPERADO}])
        self.assertIn("RuntimeError: falha de teste", self.terminal.getvalue())
        self.assertIn("[servidor] erro inesperado ao atender o pedido; detalhes acima.", self.terminal.getvalue())
        # O servidor segue atendendo.
        self.llm.programar(resposta_texto("Oi."))
        self.assertEqual(self.eventos(self.texto("oi"))[-1], {"tipo": "fim"})

    def test_falha_inesperada_depois_da_resposta_vira_erro_no_lugar_do_fim(self):
        self.llm.programar(resposta_texto("Oi."))
        with mock.patch.object(self.servidor, "mostrar_tempos", side_effect=RuntimeError("falha de teste")):
            eventos = self.eventos(self.texto("oi"))
        self.assertEqual(eventos, [{"tipo": "transcricao", "texto": "oi"},
                                   {"tipo": "resposta", "texto": "Oi.", "acoes": []},
                                   {"tipo": "erro", "erro": ERRO_INESPERADO}])
        self.assertIn("RuntimeError: falha de teste", self.terminal.getvalue())

    def test_resposta_malformada_do_llm_vira_evento_erro(self):
        self.llm.programar({"sem": "choices"})  # no JSON, vira o 500 do erro inesperado
        eventos = self.eventos(self.texto())
        self.assertEqual(eventos[1:], [{"tipo": "erro", "erro": ERRO_INESPERADO}])
        self.assertIn("KeyError", self.terminal.getvalue())

    def test_cada_evento_fica_numa_linha_so(self):
        # Quebras no texto, inclusive as do Unicode (que um str.splitlines() do outro lado cortaria), e a metade
        # solta de um emoji (que o UTF-8 não aceita) não partem o evento nem derrubam o streaming.
        resposta = "Linha um.\nLinha dois.\r\nTrês quatro cinco\x85seis."
        self.llm.programar(resposta_texto(resposta))
        r = self.texto("oi \ud83d tudo bem?")
        self.assertEqual(self.eventos(r), [{"tipo": "transcricao", "texto": "oi \ud83d tudo bem?"},
                                           {"tipo": "resposta", "texto": resposta, "acoes": []},
                                           {"tipo": "fim"}])
        self.assertEqual(len(r.get_data().decode("utf-8").splitlines()), 3)
        self.assertEqual(self.terminal.getvalue(), "")

    def test_cliente_que_desconecta_no_meio_nao_prende_nada(self):
        # O Werkzeug só percebe a queda na escrita seguinte e aí fecha o gerador: tudo precisa ser solto.
        piper = self.com_voz()
        synthesize, fechados = piper.synthesize, []

        def synthesize_vigiado(texto):
            try:
                yield from synthesize(texto)
            finally:
                fechados.append(texto)

        piper.synthesize = synthesize_vigiado
        self.llm.programar(resposta_texto(RESPOSTA), resposta_texto("Oi."))
        r = self.cliente.post("/texto", json={"texto": "abre o Spotify", "conversa": "aba-1"},
                              headers={"Accept": NDJSON})
        linhas = iter(r.response)
        self.assertEqual([json.loads(next(linhas))["tipo"] for _ in range(3)], ["transcricao", "resposta", "audio"])
        r.close()  # o que o Werkzeug faz quando a escrita no socket falha

        self.assertEqual(fechados, [" ".join(FRASES)])  # a síntese parou ali, sem ficar suspensa
        self.assertFalse(self.servidor.VOZ._trava.locked())
        self.assertEqual(self.servidor.lembrar("aba-1")[-1], {"role": "assistant", "content": RESPOSTA})
        self.assertEqual(self.terminal.getvalue(), "")  # nem erro nem traceback
        self.assertNotIn("tempos:", self.saida.getvalue())
        # O próximo pedido tem a voz inteira.
        self.assertEqual([e["tipo"] for e in self.eventos(self.texto("oi"))], ["transcricao", "resposta", "audio", "fim"])

    def test_linha_de_tempos_sai_no_fim(self):
        self.com_voz()
        self.llm.programar(resposta_texto(RESPOSTA))
        self.llm.transcricao = "que horas são?"
        self.eventos(self.audio())
        linha, = [linha for linha in self.saida.getvalue().splitlines() if linha.startswith("[servidor] tempos:")]
        for trecho in ("transcrição ", "LLM ", "em 1 rodada", "voz ", "até a 1ª de 3 frases", "total "):
            self.assertIn(trecho, linha)

    def test_tempo_da_voz_e_ate_a_primeira_frase(self):
        relogio = SimpleNamespace(agora=100.0)
        piper = self.com_voz()
        synthesize = piper.synthesize

        def synthesize_demorado(texto):  # cada frase leva 1 s no relógio falso
            for pedaco in synthesize(texto):
                relogio.agora += 1.0
                yield pedaco

        piper.synthesize = synthesize_demorado
        tempos = {"inicio": 99.5, "llm": 0.3, "rodadas": 1}
        with mock.patch.object(self.servidor, "time", SimpleNamespace(monotonic=lambda: relogio.agora)):
            audios = list(self.servidor.audios_por_frase(RESPOSTA, tempos))
            self.assertEqual(len(audios), 3)
            self.assertEqual((tempos["voz"], tempos["frases"]), (1.0, 3))
            self.servidor.mostrar_tempos(tempos)
        self.assertEqual(self.saida.getvalue().splitlines()[-1],
                         "[servidor] tempos: LLM 0,3 s em 1 rodada · voz 1,0 s até a 1ª de 3 frases · total 3,5 s")

    # ---------- quem não pede NDJSON recebe o JSON de sempre ----------

    def test_sem_pedir_ndjson_vale_o_json(self):
        for accept in (None, "*/*", "application/json", "text/html,application/xhtml+xml,*/*;q=0.8",
                       "application/json, application/x-ndjson;q=0.5"):
            with self.subTest(accept=accept):
                self.llm.programar(resposta_texto("Oi."))
                r = self.texto("oi", accept=accept)
                self.assertJSON(r, 200, {"transcricao": "oi", "resposta": "Oi.", "acoes": []})

    def test_ndjson_preferido_no_accept_vale_o_streaming(self):
        for accept in (NDJSON, "application/x-ndjson, application/json;q=0.9",
                       "application/json;q=0.5, application/x-ndjson"):
            with self.subTest(accept=accept):
                self.llm.programar(resposta_texto("Oi."))
                self.assertEqual(self.eventos(self.texto("oi", accept=accept))[-1], {"tipo": "fim"})

    def test_json_com_a_voz_continua_com_um_audio_so(self):
        self.com_voz()
        self.llm.programar(resposta_texto(RESPOSTA))
        dados = self.texto(accept="application/json").get_json()
        self.assertEqual(set(dados), {"transcricao", "resposta", "acoes", "audio"})
        self.assertEqual(ler_wav(base64.b64decode(dados["audio"]))[3], 2205)  # a síntese inteira, de hoje

    def test_json_da_voz_tambem_varia_com_o_accept(self):
        self.llm.transcricao = "oi"
        self.llm.programar(resposta_texto("Oi."))
        self.assertJSON(self.audio(accept=None), 200, {"transcricao": "oi", "resposta": "Oi.", "acoes": []})

    # ---------- erros antes do stream continuam em JSON ----------

    def test_erros_antes_do_stream_continuam_json_com_o_status_de_hoje(self):
        self.assertJSON(self.audio(dados=b"123"), 400, {"erro": "Áudio curto demais. Segure o botão enquanto fala."})
        self.llm.transcricao = "   "
        self.assertJSON(self.audio(), 400, {"erro": "Não entendi nada no áudio. Tente de novo."})
        self.llm.status_transcricao = 401
        self.assertJSON(self.audio(), 502,
                        {"erro": "A API de transcrição recusou a chave. Confira a chave no config_servidor.json."})
        self.assertJSON(self.texto("   "), 400, {"erro": "Digite um comando."})
        self.assertEqual(self.llm.pedidos_de_chat(), [])

    def test_erros_do_flask_continuam_json(self):
        r = self.cliente.get("/texto", headers={"Accept": NDJSON})
        self.assertEqual((r.status_code, r.get_json()),
                         (405, {"erro": "Esse endereço não aceita esse tipo de pedido."}))
        r = self.cliente.post("/nao-existe", headers={"Accept": NDJSON})
        self.assertEqual((r.status_code, r.get_json()), (404, {"erro": "Endereço não encontrado no servidor."}))

    # ---------- /voz e a memória da conversa ----------

    def test_voz_com_ndjson(self):
        self.com_voz()
        self.llm.transcricao = " que horas são? "
        self.llm.programar(resposta_texto("São 10h33."))
        eventos = self.eventos(self.audio())
        self.assertEqual([e["tipo"] for e in eventos], ["transcricao", "resposta", "audio", "fim"])
        self.assertEqual(eventos[0]["texto"], "que horas são?")
        self.assertEqual(self.llm.pedidos[0]["caminho"], "/audio/transcriptions")

    def test_memoria_da_conversa_no_streaming(self):
        self.llm.programar(*[resposta_texto(f"Resposta {n}.") for n in range(4)])
        self.eventos(self.texto("meu nome é Ana", conversa="aba-1"))
        self.eventos(self.texto("como é meu nome?", conversa="aba-1"))
        mensagens = self.llm.pedidos_de_chat()[-1]["json"]["messages"][1:]
        self.assertEqual(mensagens, [{"role": "user", "content": "meu nome é Ana"},
                                     {"role": "assistant", "content": "Resposta 0."},
                                     {"role": "user", "content": "como é meu nome?"}])
        # Pelo /voz, a conversa vem no cabeçalho X-Conversa; outra conversa não vê nada.
        self.llm.transcricao = "e agora?"
        self.eventos(self.audio(**{"X-Conversa": "aba-1"}))
        self.assertEqual(self.llm.pedidos_de_chat()[-1]["json"]["messages"][1],
                         {"role": "user", "content": "meu nome é Ana"})
        self.eventos(self.audio(**{"X-Conversa": "aba-2"}))
        self.assertEqual(self.llm.pedidos_de_chat()[-1]["json"]["messages"][1:],
                         [{"role": "user", "content": "e agora?"}])

    # ---------- no servidor HTTP de verdade ----------

    def test_cada_linha_sai_assim_que_fica_pronta(self):
        # O Werkzeug manda a resposta em partes: a transcrição chega enquanto o LLM ainda nem respondeu.
        liberar, respondeu = threading.Event(), threading.Event()

        def conversar_devagar(frase, conversa, tempos):
            liberar.wait(5)  # com o stream preso num buffer, a 1ª linha só chegaria depois disto
            respondeu.set()
            return "Pronto.", []

        servidor_http = make_server("127.0.0.1", 0, self.servidor.app, threaded=True,
                                    request_handler=RequisicaoSilenciosa)
        thread = threading.Thread(target=servidor_http.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(self.servidor, "conversar", side_effect=conversar_devagar), \
                    requests.Session() as sessao, \
                    sessao.post(f"http://127.0.0.1:{servidor_http.server_port}/texto", json={"texto": "oi"},
                                headers={"Accept": NDJSON}, stream=True, timeout=10) as r:
                self.assertEqual(r.headers["Content-Type"], "application/x-ndjson; charset=utf-8")
                self.assertEqual(r.headers.get("Transfer-Encoding"), "chunked")
                self.assertNotIn("Content-Length", r.headers)
                self.assertEqual(r.headers.get("Vary"), "Accept")
                linhas = r.iter_lines()
                self.assertEqual(json.loads(next(linhas)), {"tipo": "transcricao", "texto": "oi"})
                self.assertFalse(respondeu.is_set(), "a transcrição só chegou depois da resposta do LLM")
                liberar.set()
                resto = [json.loads(linha) for linha in linhas if linha]
        finally:
            liberar.set()
            servidor_http.shutdown()
            servidor_http.server_close()
            thread.join(timeout=5)
        self.assertEqual(resto, [{"tipo": "resposta", "texto": "Pronto.", "acoes": []}, {"tipo": "fim"}])


if __name__ == "__main__":
    unittest.main()
