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
  { ferramenta: "fechar_programa", argumentos: { nome: "chrome" }, resultado: { ok: true, descricao: "fechei chrome" } },
  { ferramenta: "volume_do_pc", argumentos: { acao: "definir" }, resultado: { erro: "o PC não tem uma saída de som ativa" } },
  { ferramenta: "energia_do_pc", argumentos: { acao: "desligar" },
    resultado: { confirmar: true, pergunta: "Quer mesmo desligar o PC? Diga sim para confirmar." } },
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

FALAR_JS = """
let somAtual = null;
const registro = [];
function mostrar(texto, erro) { registro.push(["status", texto, !!erro]); }
function falarNoNavegador(texto) { registro.push(["navegador", texto]); }
class Audio {
  constructor(src) { registro.push(["audio", src.slice(0, 22)]); this.src = src; }
  play() { return Audio.recusar ? Promise.reject(Object.assign(new Error("x"), { name: "NotAllowedError" }))
                                : Promise.resolve(); }
  pause() { registro.push(["pausa"]); }
}
(async () => {
  const saida = {};
  falar("Oi.", "UklGRg==");
  await new Promise(r => setTimeout(r, 0));
  saida.tocou = registro.splice(0);
  Audio.recusar = true;
  falar("Oi.", "UklGRg==");
  await new Promise(r => setTimeout(r, 0));
  saida.recusado = registro.splice(0);
  falar("Oi.", null);
  saida.sem_audio = registro.splice(0);
  console.log(JSON.stringify(saida));
})();
"""


@unittest.skipUnless(NODE, "Node não está instalado; os testes do JavaScript da página foram pulados")
class TestFalarNaPagina(unittest.TestCase):
    def test_voz_do_servidor_e_reserva_do_navegador(self):
        resultado = rodar_no_node("falar", FALAR_JS)
        self.assertEqual(resultado["tocou"], [["audio", "data:audio/wav;base64,"]])
        self.assertEqual(resultado["recusado"], [
            ["pausa"], ["audio", "data:audio/wav;base64,"],
            ["status", "Não consegui tocar a voz do servidor (NotAllowedError); usei a do navegador.", True],
            ["navegador", "Oi."]])
        self.assertEqual(resultado["sem_audio"], [["pausa"], ["navegador", "Oi."]])


@unittest.skipUnless(NODE, "Node não está instalado; os testes do JavaScript da página foram pulados")
class TestAcoesNaPagina(unittest.TestCase):
    def test_programas_e_lampada(self):
        self.assertEqual(rodar_no_node("descreverAcao", ACOES_JS), [
            "Abriu: calculadora",
            "Não abriu: paint (não liberado)",
            "Deixei a lâmpada em azul",
            "A lâmpada não respondeu",
            "Algo deu errado",
            "Fechei chrome",
            "O PC não tem uma saída de som ativa",
            "Quer mesmo desligar o PC? Diga sim para confirmar.",
        ])


if __name__ == "__main__":
    unittest.main()
