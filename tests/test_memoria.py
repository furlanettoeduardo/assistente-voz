"""
Testes da memória curta da conversa (servidor.py): o LLM vê os últimos pedidos da mesma conversa, para
entender "agora fecha ele". O LLM é o servidor falso local e o agente roda de verdade em 127.0.0.1, com
as funções que mexem no PC trocadas por mocks.
"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.auxiliares import LLMFalso, comando_de_teste, iniciar_agente, resposta_ferramenta, resposta_texto
from tests.test_servidor import PRECISA_DEPENDENCIAS, TOKEN, carregar_servidor

PROGRAMA = "programa de teste"


@PRECISA_DEPENDENCIAS
class TestMemoria(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        pasta = Path(cls.tmp.name)
        cls.agente, cls.servidor_agente = iniciar_agente(
            pasta / "agente", TOKEN, {PROGRAMA: comando_de_teste(pasta / "aberto.txt")},
            fechar={PROGRAMA: "teste.exe"}, acoes=["volume", "desligar"])
        cls.llm = LLMFalso()
        cls.servidor = carregar_servidor(pasta / "servidor", llm_base_url=cls.llm.url, pc_url=cls.servidor_agente.url)

    @classmethod
    def tearDownClass(cls):
        cls.llm.parar()
        cls.servidor_agente.parar()
        cls.tmp.cleanup()
        cls.sem_proxy.stop()

    def setUp(self):
        self.llm.zerar()
        self.servidor._memorias.clear()
        self.servidor._pendentes.clear()
        self.cliente = self.servidor.app.test_client()
        self.sistema = mock.Mock()
        self.sistema.fechar.return_value = "fechou"
        self.sistema.desligar.return_value = "30 segundos"
        for nome in ("volume_ler", "volume_definir", "volume_mudo", "fechar", "bloquear", "desligar",
                     "cancelar_desligamento"):
            patcher = mock.patch.object(self.agente.sistema, nome, getattr(self.sistema, nome))
            patcher.start()
            self.addCleanup(patcher.stop)
        popen = mock.patch.object(self.agente.subprocess, "Popen")  # "abrir" não roda nada de verdade
        popen.start()
        self.addCleanup(popen.stop)
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))
        pilha.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def enviar(self, texto: str, conversa="aba-1", **extra):
        corpo = {"texto": texto, **({"conversa": conversa} if conversa is not None else {}), **extra}
        return self.cliente.post("/texto", json=corpo).get_json()

    def mensagens(self, pedido: int = -1) -> list[dict]:
        """As mensagens que o LLM recebeu no pedido de chat `pedido`, sem o prompt do sistema."""
        return self.llm.pedidos_de_chat()[pedido]["json"]["messages"][1:]

    def test_o_llm_ve_o_pedido_anterior(self):
        self.llm.programar(resposta_ferramenta("c1", "abrir_programa", {"nome": PROGRAMA}),
                           resposta_texto("<think>ok</think>Abri o programa de teste."),
                           resposta_texto("Qual programa?"))
        self.enviar("abre o programa de teste")
        self.enviar("agora fecha ele")
        anteriores = self.mensagens()
        self.assertEqual([m["role"] for m in anteriores], ["user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(anteriores[0]["content"], "abre o programa de teste")
        self.assertEqual(anteriores[1]["tool_calls"][0]["function"]["name"], "abrir_programa")
        self.assertEqual(anteriores[2]["tool_call_id"], "c1")
        self.assertEqual(anteriores[3]["content"], "Abri o programa de teste.")  # sem o <think>
        self.assertEqual(anteriores[4], {"role": "user", "content": "agora fecha ele"})

    def test_conversas_diferentes_nao_se_misturam(self):
        self.llm.programar(resposta_texto("Oi."), resposta_texto("Oi."), resposta_texto("Oi."))
        self.enviar("meu nome é Ana", conversa="aba-1")
        self.enviar("como é meu nome?", conversa="aba-2")
        self.assertEqual(self.mensagens(), [{"role": "user", "content": "como é meu nome?"}])
        self.cliente.post("/texto", json={"texto": "e agora?"}, headers={"X-Conversa": "aba-1"})
        self.assertEqual(self.mensagens()[0], {"role": "user", "content": "meu nome é Ana"})

    def test_so_os_ultimos_pedidos(self):
        self.llm.programar(*[resposta_texto(f"resposta {n}") for n in range(7)])
        for n in range(7):
            self.enviar(f"pedido {n}")
        vistos = [m["content"] for m in self.mensagens() if m["role"] == "user"]
        self.assertEqual(vistos, ["pedido 2", "pedido 3", "pedido 4", "pedido 5", "pedido 6"])  # 4 + o atual

    def test_conversa_parada_recomeca(self):
        self.llm.programar(resposta_texto("Oi."), resposta_texto("Oi."))
        self.enviar("primeiro")
        with mock.patch.object(self.servidor, "MEMORIA_SEGUNDOS", -1):
            self.enviar("segundo")  # grava já vencido
        self.enviar("terceiro")
        self.assertEqual(self.mensagens(), [{"role": "user", "content": "terceiro"}])

    def test_falha_do_llm_nao_entra_na_memoria(self):
        self.llm.programar_com_status((500, {"error": {"message": "fora do ar"}}))
        self.enviar("primeiro")
        self.llm.programar(resposta_texto("Oi."))
        self.enviar("segundo")
        self.assertEqual(self.mensagens(), [{"role": "user", "content": "segundo"}])

    def test_confirmacao_entra_na_memoria_e_vale_so_na_mesma_conversa(self):
        self.llm.programar(resposta_ferramenta("c1", "energia_do_pc", {"acao": "desligar"}),
                           resposta_texto("Sim o quê?"), resposta_texto("Ok."))
        self.enviar("desliga o pc", conversa="sala")
        self.enviar("sim", conversa="quarto")  # outra conversa: vai para o LLM e não desliga
        self.sistema.desligar.assert_not_called()
        self.enviar("sim", conversa="sala")
        self.sistema.desligar.assert_called_once_with()
        self.enviar("obrigado", conversa="sala")
        conteudos = [m.get("content") for m in self.mensagens() if m["role"] in ("user", "assistant")]
        self.assertIn("Quer mesmo desligar o PC? Diga sim para confirmar.", conteudos)
        self.assertIn("sim", conteudos)
        self.assertTrue(any(c and c.startswith("Tudo bem, o PC vai desligar") for c in conteudos))

    def test_id_de_conversa_estranho_vira_o_padrao(self):
        for valor in (None, "", "a" * 65, "com espaço", "../x", 5, ["x"]):
            with self.subTest(valor=valor):
                self.assertEqual(self.servidor.ler_conversa(valor), self.servidor.CONVERSA_PADRAO)
        self.assertEqual(self.servidor.ler_conversa("9f1c-Aba_2"), "9f1c-Aba_2")

    def test_limite_de_conversas_guardadas(self):
        with mock.patch.object(self.servidor, "MEMORIA_CONVERSAS", 3):
            for n in range(5):
                self.servidor.guardar_pedido(f"c{n}", [{"role": "user", "content": str(n)}])
            self.assertEqual(list(self.servidor._memorias), ["c2", "c3", "c4"])
            self.assertEqual(self.servidor.lembrar("c0"), [])


if __name__ == "__main__":
    unittest.main()
