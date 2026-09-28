"""
Testes do JavaScript da página. Rodam funções do index.html no Node (a pedir() com um fetch
falso); sem Node instalado, são pulados (o projeto não depende dele).
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.auxiliares import PASTA_SERVIDOR

NODE = shutil.which("node")

CASOS_JS = """
const resposta = (ok, status, json) => Promise.resolve({ ok, status, json });
const casos = {
  servidor_fora: () => Promise.reject(new TypeError("Failed to fetch")),
  html_500: () => resposta(false, 500, () => Promise.reject(new SyntaxError("Unexpected token '<'"))),
  json_502: () => resposta(false, 502, () => Promise.resolve({ erro: "Sem conexão com a internet." })),
  json_sem_erro: () => resposta(false, 404, () => Promise.resolve({})),
  ok: () => resposta(true, 200, () => Promise.resolve({ resposta: "Abri." })),
  ok_invalido: () => resposta(true, 200, () => Promise.reject(new SyntaxError("Unexpected end of JSON"))),
};
(async () => {
  const saida = {};
  for (const [nome, falso] of Object.entries(casos)) {
    globalThis.fetch = falso;
    try { saida[nome] = { dados: await pedir("/texto", {}) }; }
    catch (e) { saida[nome] = { erro: e.message }; }
  }
  console.log(JSON.stringify(saida));
})();
"""

ACOES_JS = """
const acoes = [
  { ferramenta: "abrir_programa", argumentos: { nome: "calculadora" }, resultado: { ok: true } },
  { ferramenta: "abrir_programa", argumentos: { nome: "paint" }, resultado: { erro: "não liberado" } },
  { ferramenta: "controlar_lampada", argumentos: { cor: "azul" },
    resultado: { ok: true, descricao: "deixei a lâmpada em azul" } },
  { ferramenta: "controlar_lampada", argumentos: { ligar: true }, resultado: { erro: "a lâmpada não respondeu" } },
  { ferramenta: "controlar_lampada", argumentos: {}, resultado: {} },
];
console.log(JSON.stringify(acoes.map(descreverAcao)));
"""


def rodar_no_node(nome: str, codigo: str):
    """Roda a função `nome` do index.html seguida de `codigo` no Node e devolve o JSON impresso."""
    html = (PASTA_SERVIDOR / "static" / "index.html").read_text(encoding="utf-8")
    funcao = re.search(rf"^(async )?function {nome}\(.*?^}}$", html, flags=re.MULTILINE | re.DOTALL)
    assert funcao, f"não achei a função {nome}() no index.html"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        script = Path(tmp) / f"{nome}.js"
        script.write_text(funcao.group(0) + codigo, encoding="utf-8")
        saida = subprocess.run([NODE, str(script)], capture_output=True, text=True,
                               encoding="utf-8", timeout=30, check=True)
    return json.loads(saida.stdout)


@unittest.skipUnless(NODE, "Node não está instalado; os testes do JavaScript da página foram pulados")
class TestPedirDaPagina(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resultado = rodar_no_node("pedir", CASOS_JS)

    def test_servidor_fora_do_ar(self):
        self.assertEqual(self.resultado["servidor_fora"]["erro"],
                         "Sem conexão com o servidor do celular. Confira se ele ainda está rodando no Termux.")

    def test_erro_em_html(self):
        self.assertEqual(self.resultado["html_500"]["erro"], "O servidor respondeu com erro 500. Tente de novo.")

    def test_erro_em_json_usa_a_mensagem_do_servidor(self):
        self.assertEqual(self.resultado["json_502"]["erro"], "Sem conexão com a internet.")
        self.assertEqual(self.resultado["json_sem_erro"]["erro"], "O servidor respondeu com erro 404. Tente de novo.")

    def test_resposta_certa(self):
        self.assertEqual(self.resultado["ok"], {"dados": {"resposta": "Abri."}})

    def test_resposta_certa_mas_ilegivel(self):
        self.assertEqual(self.resultado["ok_invalido"]["erro"],
                         "O servidor mandou uma resposta que a página não entendeu. Tente de novo.")


@unittest.skipUnless(NODE, "Node não está instalado; os testes do JavaScript da página foram pulados")
class TestAcoesNaPagina(unittest.TestCase):
    def test_programas_e_lampada(self):
        self.assertEqual(rodar_no_node("descreverAcao", ACOES_JS), [
            "Abriu: calculadora",
            "Não abriu: paint (não liberado)",
            "Deixei a lâmpada em azul",
            "A lâmpada não respondeu",
            "Erro na lâmpada",
        ])


if __name__ == "__main__":
    unittest.main()
