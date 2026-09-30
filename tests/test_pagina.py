"""
Testes do JavaScript da página. Rodam funções do index.html no Node (a pedir() com um fetch
falso) ou o <script> inteiro num navegador falso (DOM, fetch, voz do navegador e Web Audio falsos);
sem Node instalado, são pulados (o projeto não depende dele). O streaming usa o Response, o
ReadableStream e o TextDecoder de verdade do Node (18 ou mais novo).
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


def _versao_do_node() -> int:
    """A versão principal do Node (18 para v18.19.0), ou 0 sem Node."""
    if not NODE:
        return 0
    saida = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30)
    versao = re.match(r"v(\d+)", saida.stdout.strip())
    return int(versao.group(1)) if versao else 0


NODE_18 = _versao_do_node() >= 18
INDEX = PASTA_SERVIDOR / "static" / "index.html"

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


def _rodar_js(nome: str, fonte: str):
    """Roda `fonte` no Node e devolve o JSON impresso; se o Node falhar, o erro dele vai na mensagem."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        script = Path(tmp) / f"{nome}.js"
        script.write_text(fonte, encoding="utf-8")
        saida = subprocess.run([NODE, str(script)], capture_output=True, text=True,
                               encoding="utf-8", timeout=30)
    if saida.returncode != 0:
        raise AssertionError(f"o Node falhou (código {saida.returncode}):\n{saida.stderr}")
    return json.loads(saida.stdout)


def rodar_no_node(nome: str, codigo: str):
    """Roda a função `nome` do index.html seguida de `codigo` no Node e devolve o JSON impresso."""
    html = INDEX.read_text(encoding="utf-8")
    funcao = re.search(rf"^(async )?function {nome}\(.*?^}}$", html, flags=re.MULTILINE | re.DOTALL)
    assert funcao, f"não achei a função {nome}() no index.html"
    return _rodar_js(nome, funcao.group(0) + codigo)


# Um navegador falso, só com o que a página usa. `registro` guarda os efeitos na ordem (voz do
# navegador, Web Audio, <audio>) e estado() mostra o status, o botão e o histórico.
PAGINA_FALSA_JS = r"""
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.navigator ??= {};  // o Node 20 não tem navigator; sem mediaDevices, a página avisa que não grava

const registro = [];
const efeitos = () => registro.splice(0);

class Elemento {
  constructor(id = "") {
    this.id = id; this.filhos = []; this.ouvintes = {}; this.className = ""; this.value = ""; this.texto = "";
    const classes = new Set();
    this.classList = {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, ligar = !classes.has(c)) => { if (ligar) classes.add(c); else classes.delete(c); return ligar; },
    };
  }
  get textContent() { return this.texto + this.filhos.map(f => typeof f === "string" ? f : f.textContent).join(""); }
  set textContent(t) { this.texto = t == null ? "" : String(t); this.filhos = []; }
  append(...nos) { this.filhos.push(...nos); }
  prepend(...nos) { this.filhos.unshift(...nos); }
  addEventListener(tipo, f) { (this.ouvintes[tipo] ||= []).push(f); }
  setPointerCapture() {}
  disparar(tipo, dados = {}) {
    const ev = { pointerId: 1, pointerType: "mouse", preventDefault() {}, ...dados };
    (this.ouvintes[tipo] || []).forEach(f => f(ev));
  }
}
const elementos = Object.fromEntries(
  ["status", "falar", "ouvir", "formTexto", "campo", "historico"].map(id => [id, new Elemento(id)]));
const documento = new Elemento("document");  // os ouvintes de gesto da página inteira
globalThis.document = { getElementById: id => elementos[id], createElement: () => new Elemento(),
                        addEventListener: (tipo, f) => documento.addEventListener(tipo, f) };

function estado() {
  return {
    status: elementos.status.textContent,
    vermelho: elementos.status.classList.contains("erro"),
    pensando: elementos.falar.classList.contains("pensando"),
    historico: elementos.historico.filhos.map(li => li.filhos.map(p => [p.className, p.textContent])),
  };
}

globalThis.speechSynthesis = {
  getVoices: () => [],
  cancel: () => registro.push(["cancelar"]),
  speak: fala => registro.push(["navegador", fala.text]),
};
globalThis.SpeechSynthesisUtterance = class { constructor(texto) { this.text = texto; } };
globalThis.Audio = class {
  constructor(src) { registro.push(["audio", src]); }
  play() { return Promise.resolve(); }
  pause() { registro.push(["pausa"]); }
};

// A Web Audio falsa: o "WAV" de cada frase é o texto "frase:<duração em segundos>" em base64.
class ContextoFalso {
  static estado = "running";  // como o contexto nasce
  static libera = true;       // se o navegador aceita o resume(); se não, a promessa fica pendente (como no Chrome)
  static atraso = 0;          // quantos ms o decodeAudioData leva
  constructor() {
    this.state = ContextoFalso.estado; this.currentTime = 0; this.destination = { saida: true };
    registro.push(["contexto", this.state]);
  }
  resume() {
    registro.push(["resume"]);
    if (!ContextoFalso.libera) return new Promise(() => {});
    this.state = "running";
    return Promise.resolve();
  }
  decodeAudioData(dados) {
    const texto = new TextDecoder().decode(dados);
    const decodificar = () => {
      if (!texto.startsWith("frase:")) throw new DOMException("Unable to decode audio data", "EncodingError");
      return { duration: Number(texto.slice(6)) };
    };
    const espera = ContextoFalso.atraso ? new Promise(ok => setTimeout(ok, ContextoFalso.atraso)) : Promise.resolve();
    return espera.then(decodificar);
  }
  createBufferSource() {
    const saida = this.destination;
    return {
      buffer: null, ligada: null,
      connect(destino) { this.ligada = destino; },
      start(quando) {
        if (this.ligada !== saida) throw new Error("a frase não foi ligada na saída de som");
        registro.push(["tocar", quando, this.buffer.duration]);
      },
      stop() { registro.push(["parar"]); },
    };
  }
}
globalThis.AudioContext = ContextoFalso;

// fetch falso: devolve, em ordem, o que o teste pôs em `respostas` (um Error vira servidor fora do ar).
const chamadas = [], respostas = [];
globalThis.fetch = (url, opcoes) => {
  chamadas.push({ url, metodo: opcoes.method, cabecalhos: Object.fromEntries(new Headers(opcoes.headers)),
                  corpo: typeof opcoes.body === "string" ? opcoes.body : null });
  const r = respostas.shift();
  return r instanceof Error ? Promise.reject(r) : Promise.resolve(r);
};

const passar = (ms = 10) => new Promise(ok => setTimeout(ok, ms));
const codificar = texto => new TextEncoder().encode(texto);
const linha = evento => JSON.stringify(evento) + "\n";
const audioDe = duracao => btoa(`frase:${duracao}`);
const emJson = (status, dados) => new Response(JSON.stringify(dados), {
  status, headers: { "Content-Type": "application/json" } });

// Uma resposta NDJSON que o teste manda pedaço a pedaço (texto ou bytes), como o servidor faria.
// `cancelada` diz se a página fechou a conexão; depois disso, o que o "servidor" mandar se perde.
function emPartes() {
  let controle;
  const partes = {
    cancelada: false,
    resposta: new Response(new ReadableStream({ start(c) { controle = c; }, cancel() { partes.cancelada = true; } }),
                           { headers: { "Content-Type": "application/x-ndjson; charset=utf-8" } }),
    async mandar(...pedacos) {
      if (!partes.cancelada) for (const p of pedacos) controle.enqueue(typeof p === "string" ? codificar(p) : p);
      await passar();
    },
    async fechar() { if (!partes.cancelada) controle.close(); await passar(); },
    async quebrar() { controle.error(new TypeError("network error")); await passar(); },
  };
  return partes;
}

async function digitar(texto) {
  elementos.campo.value = texto;
  elementos.formTexto.disparar("submit");
  await passar();
}
// ---------- a página ----------
"""


def rodar_pagina(codigo: str, antes: str = ""):
    """
    Roda o <script> inteiro do index.html no navegador falso, seguido de `codigo`, e devolve o JSON impresso.
    Antes dele vêm os scripts do próprio servidor (<script src="/static/...">), na ordem, como o navegador
    carregaria, e `antes`, que completa o navegador falso.
    """
    html = INDEX.read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    assert script, "não achei o <script> no index.html"
    externos = "".join((PASTA_SERVIDOR / src.removeprefix("/")).read_text(encoding="utf-8") + "\n"
                       for src in re.findall(r'<script src="(/static/[^"]+)"></script>', html))
    return _rodar_js("pagina", PAGINA_FALSA_JS + antes + externos + script.group(1)
                     + "\n// ---------- o teste ----------\n" + codigo)


def js(codigo: str) -> str:
    """Envolve o corpo de um teste numa função assíncrona que imprime `saida` em JSON no fim."""
    return "(async () => {\nconst saida = {};\n" + codigo + "\nconsole.log(JSON.stringify(saida));\n})();\n"


AVISO = "Não consegui tocar a voz do servidor ({}); usei a do navegador."
CAIU = "A conexão com o servidor caiu no meio da resposta. Tente de novo."
PRONTO = "Segure o botão e fale"


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


# Acentos de 2 bytes, um emoji de 4 bytes e uma linha grande, como o áudio em base64.
EVENTOS = [
    {"tipo": "transcricao", "texto": "ação às 10h, você disse"},
    {"tipo": "resposta", "texto": "São 14 horas e 53 minutos. Até já 🙂", "acoes": []},
    {"tipo": "audio", "audio": "UklGR" * 400},
    {"tipo": "fim"},
]

LER_JS = """
const eventos = __EVENTOS__;
const codificar = texto => new TextEncoder().encode(texto);
let cancelados = 0;  // quantas vezes a página fechou a conexão (leitor.cancel())
function corpo(pedacos, falhaNoFim = false) {
  let i = 0;
  return { getReader: () => ({
    read: () => {
      if (i < pedacos.length) return Promise.resolve({ value: pedacos[i++], done: false });
      return falhaNoFim ? Promise.reject(new TypeError("network error")) : Promise.resolve({ value: undefined, done: true });
    },
    cancel: () => { cancelados++; return Promise.resolve(); },
  }) };
}
async function cancelou(executar) {
  const antes = cancelados;
  await executar();
  return cancelados > antes;
}
function fatiar(bytes, tamanho) {
  const pedacos = [];
  for (let i = 0; i < bytes.length; i += tamanho) pedacos.push(bytes.slice(i, i + tamanho));
  return pedacos;
}
async function ler(pedacos, falhaNoFim) {
  const lidos = [];
  try { await lerEventos(corpo(pedacos, falhaNoFim), e => { lidos.push(e); }); }
  catch (e) { lidos.push({ erro: e.message }); }
  return lidos;
}
(async () => {
  const saida = { fatias: {} };
  const bytes = codificar(eventos.map(e => JSON.stringify(e)).join("\\n") + "\\n");
  for (const tamanho of [1, 2, 3, 5, 7, 64, 1000, bytes.length]) saida.fatias[tamanho] = await ler(fatiar(bytes, tamanho));
  const acento = codificar("ç")[0];
  saida.cortou_acento = fatiar(bytes, 3).some(p => p[p.length - 1] === acento);
  // sem o "\\n" no fim, com linhas em branco e com "\\r\\n"
  saida.sem_quebra_no_fim = await ler(fatiar(codificar('{"tipo":"fim"}'), 4));
  saida.linhas_em_branco = await ler([codificar('\\n{"tipo":"a"}\\r\\n\\r\\n  \\n{"tipo":"b"}\\r\\n')]);
  saida.linha_invalida = await ler([codificar('{"tipo":"transcricao","texto":"oi"}\\n<html>\\n{"tipo":"fim"}\\n')]);
  // JSON válido que não é um evento: sem isto, a página mostraria um TypeError em inglês
  saida.nao_objetos = [];
  for (const texto of ["null", "42", '"fim"', "true"]) saida.nao_objetos.push(await ler([codificar(texto + "\\n")]));
  saida.conexao_caiu = await ler(fatiar(codificar('{"tipo":"transcricao","texto":"oi"}\\n{"tipo":"res'), 9), true);
  // uma linha inválida, ou um evento que falha, fecha a conexão; uma resposta normal, não
  saida.cancela = {
    linha_invalida: await cancelou(() => ler([codificar('<html>\\n{"tipo":"fim"}\\n')])),
    evento_que_falha: await cancelou(() => lerEventos(corpo([codificar('{"tipo":"a"}\\n')]), () => {
      throw new Error("falhou"); }).catch(() => {})),
    resposta_normal: await cancelou(() => ler([codificar('{"tipo":"fim"}\\n')])),
    conexao_caiu: await cancelou(() => ler([codificar('{"tipo":"a"}\\n')], true)),
  };
  // cada evento termina (até o decodeAudioData de um áudio) antes do próximo começar
  const ordem = [];
  await lerEventos(corpo([codificar('{"n":1}\\n{"n":2}\\n'), codificar('{"n":3}\\n')]), async e => {
    ordem.push(`começou ${e.n}`);
    await new Promise(ok => setTimeout(ok, 5));
    ordem.push(`terminou ${e.n}`);
  });
  saida.ordem = ordem;
  console.log(JSON.stringify(saida));
})();
"""


@unittest.skipUnless(NODE, "Node não está instalado; os testes do JavaScript da página foram pulados")
class TestLeituraDoStreaming(unittest.TestCase):
    """lerEventos(): as linhas do NDJSON chegam em pedaços de qualquer tamanho."""

    @classmethod
    def setUpClass(cls):
        cls.resultado = rodar_no_node("lerEventos",
                                      LER_JS.replace("__EVENTOS__", json.dumps(EVENTOS, ensure_ascii=False)))

    def test_linhas_e_acentos_cortados_entre_pedacos(self):
        self.assertTrue(self.resultado["cortou_acento"])  # o teste corta mesmo um caractere ao meio
        for tamanho, lidos in self.resultado["fatias"].items():
            with self.subTest(tamanho_do_pedaco=tamanho):
                self.assertEqual(lidos, EVENTOS)

    def test_ultima_linha_sem_quebra_e_linhas_em_branco(self):
        self.assertEqual(self.resultado["sem_quebra_no_fim"], [{"tipo": "fim"}])
        self.assertEqual(self.resultado["linhas_em_branco"], [{"tipo": "a"}, {"tipo": "b"}])

    def test_linha_que_nao_e_json(self):
        self.assertEqual(self.resultado["linha_invalida"], [
            {"tipo": "transcricao", "texto": "oi"},
            {"erro": "O servidor mandou uma resposta que a página não entendeu. Tente de novo."}])

    def test_json_que_nao_e_um_evento(self):
        erro = [{"erro": "O servidor mandou uma resposta que a página não entendeu. Tente de novo."}]
        self.assertEqual(self.resultado["nao_objetos"], [erro] * 4)

    def test_fecha_a_conexao_quando_nao_serve_mais(self):
        self.assertEqual(self.resultado["cancela"], {"linha_invalida": True, "evento_que_falha": True,
                                                     "resposta_normal": False, "conexao_caiu": False})

    def test_conexao_que_cai_no_meio(self):
        self.assertEqual(self.resultado["conexao_caiu"], [{"tipo": "transcricao", "texto": "oi"}, {"erro": CAIU}])

    def test_um_evento_de_cada_vez(self):
        self.assertEqual(self.resultado["ordem"], ["começou 1", "terminou 1", "começou 2", "terminou 2",
                                                   "começou 3", "terminou 3"])


FLUXO_JS = js("""
const s = emPartes();
respostas.push(s.resposta);
const passos = saida.passos = [];
const passo = nome => passos.push({ nome, efeitos: efeitos(), ...estado() });
saida.ao_carregar = efeitos();
await digitar("vai chover amanhã?");
passo("pedido");
saida.chamada = chamadas[0];
saida.conversa_certa = JSON.parse(chamadas[0].corpo).conversa === CONVERSA;
const transcricao = codificar(linha({ tipo: "transcricao", texto: "vai chover amanhã?" }));
const corte = transcricao.indexOf(0xC3) + 1;  // no meio do "ã"
await s.mandar(transcricao.slice(0, corte));
passo("meia transcrição");
await s.mandar(transcricao.slice(corte));
passo("transcrição");
await s.mandar(linha({ tipo: "resposta", texto: "Amanhã deve chover. Leve guarda-chuva.", acoes: [
  { ferramenta: "previsao_do_tempo", argumentos: { dias_a_frente: 1 },
    resultado: { ok: true, descricao: "previsão de amanhã" } }] }));
passo("resposta");
const segundo = linha({ tipo: "audio", audio: audioDe(2.25) });
await s.mandar(linha({ tipo: "audio", audio: audioDe(1.5) }) + segundo.slice(0, 12));
passo("1º áudio");
await s.mandar(segundo.slice(12) + linha({ tipo: "audio", audio: audioDe(0.75) }));
passo("2º e 3º áudios");
await s.mandar(linha({ tipo: "fim" }));
passo("fim");
await s.fechar();
passo("fechou");
""")

RELOGIO_JS = js("""
const [a, b] = [emPartes(), emPartes()];
respostas.push(a.resposta, b.resposta);
await digitar("conta uma história");
await a.mandar(linha({ tipo: "transcricao", texto: "conta uma história" }),
               linha({ tipo: "resposta", texto: "Era uma vez. Fim.", acoes: [] }));
efeitos();
contexto.currentTime = 10;  // o contexto já existia: a 1ª frase começa agora
await a.mandar(linha({ tipo: "audio", audio: audioDe(1.5) }));
contexto.currentTime = 10.5;  // a 1ª ainda está tocando: a 2ª entra logo depois dela
await a.mandar(linha({ tipo: "audio", audio: audioDe(1) }));
contexto.currentTime = 20;  // a 3ª chegou depois que a 2ª acabou: começa agora, sem esperar
await a.mandar(linha({ tipo: "audio", audio: audioDe(0.5) }), linha({ tipo: "fim" }));
await a.fechar();
saida.primeiro = efeitos();
contexto.currentTime = 20.25;  // um pedido novo cala as frases e o navegador e recomeça a fila
await digitar("e depois?");
saida.pedido_novo = efeitos();
await b.mandar(linha({ tipo: "transcricao", texto: "e depois?" }),
               linha({ tipo: "resposta", texto: "Depois acabou.", acoes: [] }),
               linha({ tipo: "audio", audio: audioDe(1) }), linha({ tipo: "fim" }));
await b.fechar();
saida.segundo = efeitos();
""")

SEM_AUDIO_JS = js("""
const s = emPartes();
respostas.push(s.resposta);
enviar("/voz", { method: "POST", headers: { "Content-Type": "audio/webm;codecs=opus", "X-Conversa": CONVERSA },
                 body: new Uint8Array(2000) });
await passar();
saida.cabecalhos = chamadas[0].cabecalhos;
saida.conversa = CONVERSA;
efeitos();
await s.mandar(linha({ tipo: "transcricao", texto: "que horas são?" }),
               linha({ tipo: "resposta", texto: "São 14 horas e 53 minutos.", acoes: [] }));
saida.antes_do_fim = efeitos();
await s.mandar(linha({ tipo: "fim" }));
saida.fim = efeitos();
await s.fechar();
saida.estado = estado();
""")

JSON_JS = js("""
const envia = () => { enviar("/voz", { method: "POST", headers: { "Content-Type": "audio/webm" }, body: "x" });
                      return passar(); };
const rodada = async nome => { await envia(); saida[nome] = { efeitos: efeitos(), ...estado() }; };
respostas.push(emJson(400, { erro: "Áudio curto demais. Segure o botão enquanto fala." }));
await rodada("erro_400");
respostas.push(new Response("<h1>Bad Gateway</h1>", { status: 502, headers: { "Content-Type": "text/html" } }));
await rodada("erro_html");
respostas.push(emJson(200, { transcricao: "abre a calculadora", resposta: "Pronto.", audio: "UklGRg==",
  acoes: [{ ferramenta: "abrir_programa", argumentos: { nome: "calculadora" }, resultado: { ok: true } }] }));
await rodada("json_com_audio");
elementos.historico.filhos = [];
respostas.push(emJson(200, { transcricao: "que horas são?", resposta: "São 15 horas.", acoes: [] }));
await rodada("json_sem_audio");
elementos.historico.filhos = [];
respostas.push(new TypeError("Failed to fetch"));
await rodada("servidor_fora");
""")

ERRO_JS = js("""
const s = emPartes();
respostas.push(s.resposta);
await digitar("acende a luz");
efeitos();
await s.mandar(linha({ tipo: "transcricao", texto: "acende a luz" }),
               linha({ tipo: "erro", erro: "A API do LLM está com problemas agora. Tente de novo em instantes." }));
saida.erro = { efeitos: efeitos(), ...estado() };
await s.mandar(linha({ tipo: "fim" }));  // depois do erro, nada mais vale
await s.fechar();
saida.fechou = { efeitos: efeitos(), ...estado() };
""")

DECODIFICACAO_JS = js("""
const s = emPartes();
respostas.push(s.resposta);
await digitar("previsão da semana");
await s.mandar(linha({ tipo: "transcricao", texto: "previsão da semana" }),
               linha({ tipo: "resposta", texto: "Primeira frase. Segunda frase. Terceira.", acoes: [] }));
efeitos();
await s.mandar(linha({ tipo: "audio", audio: audioDe(1.5) }));
saida.primeira = efeitos();
await s.mandar(linha({ tipo: "audio", audio: btoa("ruim") }));
saida.falhou = { efeitos: efeitos(), ...estado() };
await s.mandar(linha({ tipo: "audio", audio: audioDe(1) }), linha({ tipo: "fim" }));
await s.fechar();
saida.depois = { efeitos: efeitos(), ...estado() };
""")

SEM_WEB_AUDIO_JS = js("""
globalThis.AudioContext = undefined;
const s = emPartes();
respostas.push(s.resposta);
await digitar("que horas são?");
saida.pedido = efeitos();
await s.mandar(linha({ tipo: "transcricao", texto: "que horas são?" }),
               linha({ tipo: "resposta", texto: "São 16 horas. Boa tarde.", acoes: [] }),
               linha({ tipo: "audio", audio: audioDe(1) }), linha({ tipo: "audio", audio: audioDe(1) }),
               linha({ tipo: "fim" }));
await s.fechar();
saida.depois = { efeitos: efeitos(), ...estado() };
""")

SOM_BLOQUEADO_JS = js("""
ContextoFalso.estado = "suspended";
ContextoFalso.libera = false;  // o navegador não aceitou o gesto: o resume() não libera o som
const s = emPartes();
respostas.push(s.resposta);
await digitar("que horas são?");
saida.pedido = efeitos();
await s.mandar(linha({ tipo: "transcricao", texto: "que horas são?" }),
               linha({ tipo: "resposta", texto: "São 16 horas.", acoes: [] }),
               linha({ tipo: "audio", audio: audioDe(1) }));
await passar(900);  // a página espera o resume() só meio segundo
await s.mandar(linha({ tipo: "fim" }));
await s.fechar();
saida.depois = { efeitos: efeitos(), ...estado() };
""")

GESTOS_JS = js("""
ContextoFalso.estado = "suspended";
ContextoFalso.libera = false;
saida.ao_carregar = efeitos();
elementos.falar.disparar("pointerdown", { pointerType: "touch" });  // no toque, o pointerdown não é gesto
await passar();
saida.pointerdown = efeitos();
ContextoFalso.libera = true;  // o pointerup do toque é
elementos.falar.disparar("pointerup", { pointerType: "touch" });
saida.pointerup = efeitos();
saida.estado_depois_do_toque = contexto.state;
elementos.falar.disparar("pointerup");  // já liberado: nada a fazer
saida.pointerup_de_novo = efeitos();
contexto.state = "suspended";  // o navegador suspendeu o som (outra aba, ligação...)
elementos.falar.disparar("keydown", { code: "Space", repeat: false });
await passar();
saida.espaco = efeitos();
contexto.state = "suspended";
respostas.push(emJson(200, { transcricao: "oi", resposta: "Oi!", acoes: [] }));
await digitar("oi");
saida.formulario = efeitos();
""")

PEDIDO_NOVO_JS = js("""
const [a, b] = [emPartes(), emPartes()];
respostas.push(a.resposta, b.resposta);
await digitar("primeiro");
await a.mandar(linha({ tipo: "transcricao", texto: "primeiro" }),
               linha({ tipo: "resposta", texto: "Resposta A. Com duas frases.", acoes: [] }),
               linha({ tipo: "audio", audio: audioDe(1.5) }), linha({ tipo: "audio", audio: audioDe(2.25) }));
efeitos();
ContextoFalso.atraso = 300;
await a.mandar(linha({ tipo: "audio", audio: audioDe(4) }));  // ainda decodificando quando o pedido novo chega
await digitar("segundo");
saida.pedido_novo = efeitos();
await passar(400);
ContextoFalso.atraso = 0;
await a.mandar(linha({ tipo: "audio", audio: audioDe(1) }), linha({ tipo: "fim" }));
await a.fechar();
saida.fim_do_antigo = { efeitos: efeitos(), ...estado() };
await b.mandar(linha({ tipo: "transcricao", texto: "segundo" }),
               linha({ tipo: "resposta", texto: "Resposta B.", acoes: [] }), linha({ tipo: "fim" }));
await b.fechar();
saida.fim_do_novo = { efeitos: efeitos(), ...estado() };
""")

GRAVAR_CALA_JS = js("""
const a = emPartes();
respostas.push(a.resposta);
await digitar("previsão");
await a.mandar(linha({ tipo: "transcricao", texto: "previsão" }),
               linha({ tipo: "resposta", texto: "Amanhã chove. Leve guarda-chuva.", acoes: [] }),
               linha({ tipo: "audio", audio: audioDe(1.5) }));
efeitos();
elementos.falar.disparar("pointerdown");  // a pessoa começa a falar enquanto a assistente ainda fala
await passar();
saida.ao_gravar = efeitos();
await a.mandar(linha({ tipo: "audio", audio: audioDe(2) }), linha({ tipo: "fim" }));
await a.fechar();
saida.depois = efeitos();
""")

FALHA_CALADA_JS = js("""
const [a, b] = [emPartes(), emPartes()];
respostas.push(a.resposta, b.resposta);
await digitar("primeiro");
await a.mandar(linha({ tipo: "transcricao", texto: "primeiro" }),
               linha({ tipo: "resposta", texto: "Resposta A.", acoes: [] }));
ContextoFalso.atraso = 300;
await a.mandar(linha({ tipo: "audio", audio: btoa("ruim") }));  // vai falhar, mas só depois do pedido novo
await digitar("segundo");
efeitos();
await passar(400);
saida.depois_da_falha = { efeitos: efeitos(), ...estado() };
""")

CORTES_JS = js("""
const rodada = async (nome, montar) => {
  const s = emPartes();
  respostas.push(s.resposta);
  await digitar("abre o spotify");
  await montar(s);
  saida[nome] = { ...estado(), fechou_a_conexao: s.cancelada };
  elementos.historico.filhos = [];
};
await rodada("conexao_caiu", async s => {
  await s.mandar(linha({ tipo: "transcricao", texto: "abre o spotify" }));
  await s.quebrar();
});
await rodada("fechou_sem_fim", async s => {
  await s.mandar(linha({ tipo: "transcricao", texto: "abre o spotify" }),
                 linha({ tipo: "resposta", texto: "Abri.", acoes: [] }));
  await s.fechar();
});
await rodada("linha_invalida", async s => {
  await s.mandar(linha({ tipo: "transcricao", texto: "abre o spotify" }) + "<html>erro</html>\\n");
  await s.mandar(linha({ tipo: "resposta", texto: "Abri.", acoes: [] }), linha({ tipo: "fim" }));  // já não vale
  await s.fechar();
});
saida.efeitos = efeitos();
""")

# O fim já chegou (e a resposta foi falada) quando a conexão cai sem o pedaço final do chunked.
QUEDA_DEPOIS_DO_FIM_JS = js("""
const s = emPartes();
respostas.push(s.resposta);
await digitar("abre o spotify");
efeitos();
await s.mandar(linha({ tipo: "transcricao", texto: "abre o spotify" }),
               linha({ tipo: "resposta", texto: "Abri o Spotify.", acoes: [] }), linha({ tipo: "fim" }));
await s.quebrar();
saida.depois = { efeitos: efeitos(), ...estado() };
""")

# pedir() com aoEvento: só um Content-Type NDJSON vira streaming; o resto segue o caminho do JSON.
TIPOS_JS = js("""
const tipos = { ndjson: "application/x-ndjson", maiusculas: "Application/X-NDJSON ; charset=UTF-8",
                parecido: "application/x-ndjsonx", json: "application/json; charset=utf-8" };
for (const [nome, tipo] of Object.entries(tipos)) {
  respostas.push(new Response('{"tipo":"fim"}\\n', { headers: { "Content-Type": tipo } }));
  const eventos = [];
  saida[nome] = { dados: await pedir("/texto", { method: "POST" }, e => { eventos.push(e); }), eventos };
}
""")

# O servidor manda as ações fora do padrão (um objeto no lugar da lista): a resposta aparece mesmo assim.
ACOES_FORA_DO_PADRAO_JS = js("""
const s = emPartes();
respostas.push(s.resposta);
await digitar("oi");
await s.mandar(linha({ tipo: "transcricao", texto: "oi" }), linha({ tipo: "resposta", texto: "Oi!", acoes: {} }),
               linha({ tipo: "fim" }));
await s.fechar();
saida.estado = estado();
""")


@unittest.skipUnless(NODE_18,"sem o Node 18 ou mais novo (Response e ReadableStream); o streaming da página foi pulado")
class TestStreamingNaPagina(unittest.TestCase):
    """A página inteira no navegador falso, com a resposta chegando em NDJSON pedaço a pedaço."""

    def test_ordem_dos_efeitos(self):
        r = rodar_pagina(FLUXO_JS)
        self.assertEqual(r["ao_carregar"], [])  # sem gesto, a página não cria o contexto de áudio
        self.assertEqual(r["chamada"]["url"], "/texto")
        self.assertEqual(r["chamada"]["metodo"], "POST")
        self.assertEqual(r["chamada"]["cabecalhos"], {"accept": "application/x-ndjson",
                                                      "content-type": "application/json"})
        self.assertEqual(json.loads(r["chamada"]["corpo"])["texto"], "vai chover amanhã?")
        self.assertTrue(r["conversa_certa"])

        voce = ["voce", "vai chover amanhã?"]
        completo = [voce, ["ela", "Amanhã deve chover. Leve guarda-chuva."], ["acao", "Previsão de amanhã"],
                    ["acao", "Dados de Open-Meteo.com (CC BY 4.0), adaptados."]]
        pensando = {"status": "Pensando…", "vermelho": False, "pensando": True}
        self.assertEqual(r["passos"], [
            {"nome": "pedido", "efeitos": [["contexto", "running"], ["cancelar"]], **pensando, "historico": []},
            {"nome": "meia transcrição", "efeitos": [], **pensando, "historico": []},
            {"nome": "transcrição", "efeitos": [], **pensando, "historico": [[voce]]},
            {"nome": "resposta", "efeitos": [], **pensando, "historico": [completo]},
            {"nome": "1º áudio", "efeitos": [["tocar", 0, 1.5]], **pensando, "historico": [completo]},
            {"nome": "2º e 3º áudios", "efeitos": [["tocar", 1.5, 2.25], ["tocar", 3.75, 0.75]], **pensando,
             "historico": [completo]},
            {"nome": "fim", "efeitos": [], "status": PRONTO, "vermelho": False, "pensando": True,
             "historico": [completo]},
            {"nome": "fechou", "efeitos": [], "status": PRONTO, "vermelho": False, "pensando": False,
             "historico": [completo]},
        ])

    def test_frases_em_sequencia_sem_sobreposicao(self):
        r = rodar_pagina(RELOGIO_JS)
        # a 2ª começa quando a 1ª acaba (10 + 1,5); a 3ª chegou com o som parado e começa na hora
        self.assertEqual(r["primeiro"], [["tocar", 10, 1.5], ["tocar", 11.5, 1], ["tocar", 20, 0.5]])
        self.assertEqual(r["pedido_novo"], [["parar"], ["parar"], ["parar"], ["cancelar"]])
        self.assertEqual(r["segundo"], [["tocar", 20.25, 1]])

    def test_fim_sem_audio_fala_com_a_voz_do_navegador(self):
        r = rodar_pagina(SEM_AUDIO_JS)
        self.assertEqual(r["cabecalhos"], {"accept": "application/x-ndjson", "content-type": "audio/webm;codecs=opus",
                                           "x-conversa": r["conversa"]})
        self.assertEqual(r["antes_do_fim"], [])  # só no fim dá para saber que não vem áudio
        self.assertEqual(r["fim"], [["cancelar"], ["navegador", "São 14 horas e 53 minutos."]])
        self.assertEqual(r["estado"], {"status": PRONTO, "vermelho": False, "pensando": False, "historico": [
            [["voce", "que horas são?"], ["ela", "São 14 horas e 53 minutos."]]]})

    def test_resposta_em_json_segue_o_caminho_de_hoje(self):
        r = rodar_pagina(JSON_JS)
        self.assertEqual(r["erro_400"], {"efeitos": [["cancelar"]], "status": "Áudio curto demais. Segure o botão enquanto fala.",
                                         "vermelho": True, "pensando": False, "historico": []})
        self.assertEqual(r["erro_html"]["status"], "O servidor respondeu com erro 502. Tente de novo.")
        self.assertTrue(r["erro_html"]["vermelho"])
        self.assertEqual(r["json_com_audio"], {
            "efeitos": [["cancelar"], ["audio", "data:audio/wav;base64,UklGRg=="]],
            "status": PRONTO, "vermelho": False, "pensando": False,
            "historico": [[["voce", "abre a calculadora"], ["ela", "Pronto."], ["acao", "Abriu: calculadora"]]]})
        self.assertEqual(r["json_sem_audio"], {
            "efeitos": [["pausa"], ["cancelar"], ["cancelar"], ["navegador", "São 15 horas."]],
            "status": PRONTO, "vermelho": False, "pensando": False,
            "historico": [[["voce", "que horas são?"], ["ela", "São 15 horas."]]]})
        self.assertEqual(r["servidor_fora"], {
            "efeitos": [["cancelar"]],
            "status": "Sem conexão com o servidor do celular. Confira se ele ainda está rodando no Termux.",
            "vermelho": True, "pensando": False, "historico": []})

    def test_evento_de_erro_fica_em_vermelho(self):
        r = rodar_pagina(ERRO_JS)
        mensagem = "A API do LLM está com problemas agora. Tente de novo em instantes."
        self.assertEqual(r["erro"], {"efeitos": [], "status": mensagem, "vermelho": True, "pensando": True,
                                     "historico": [[["voce", "acende a luz"]]]})
        self.assertEqual(r["fechou"], {"efeitos": [], "status": mensagem, "vermelho": True, "pensando": False,
                                       "historico": [[["voce", "acende a luz"]]]})

    def test_frase_que_nao_decodifica_usa_a_voz_do_navegador_uma_vez(self):
        r = rodar_pagina(DECODIFICACAO_JS)
        texto = "Primeira frase. Segunda frase. Terceira."
        self.assertEqual(r["primeira"], [["tocar", 0, 1.5]])
        # para a frase que já tocava, avisa e fala o texto inteiro no navegador
        self.assertEqual(r["falhou"]["efeitos"], [["parar"], ["cancelar"], ["cancelar"], ["navegador", texto]])
        self.assertEqual(r["falhou"]["status"], AVISO.format("EncodingError"))
        self.assertTrue(r["falhou"]["vermelho"])
        # as frases seguintes não tocam, o fim não fala de novo e o aviso continua na tela
        self.assertEqual(r["depois"]["efeitos"], [])
        self.assertEqual(r["depois"]["status"], AVISO.format("EncodingError"))
        self.assertTrue(r["depois"]["vermelho"])
        self.assertFalse(r["depois"]["pensando"])

    def test_sem_web_audio_usa_a_voz_do_navegador_uma_vez(self):
        r = rodar_pagina(SEM_WEB_AUDIO_JS)
        self.assertEqual(r["pedido"], [["cancelar"]])
        self.assertEqual(r["depois"]["efeitos"], [["cancelar"], ["cancelar"], ["navegador", "São 16 horas. Boa tarde."]])
        self.assertEqual(r["depois"]["status"], AVISO.format("NotSupportedError"))
        self.assertFalse(r["depois"]["pensando"])

    def test_som_bloqueado_pelo_navegador_usa_a_voz_do_navegador(self):
        r = rodar_pagina(SOM_BLOQUEADO_JS)
        self.assertEqual(r["pedido"], [["contexto", "suspended"], ["resume"], ["cancelar"]])
        self.assertEqual(r["depois"]["efeitos"], [["resume"], ["cancelar"], ["cancelar"], ["navegador", "São 16 horas."]])
        self.assertEqual(r["depois"]["status"], AVISO.format("NotAllowedError"))

    def test_gestos_liberam_o_som(self):
        r = rodar_pagina(GESTOS_JS)
        self.assertEqual(r["ao_carregar"], [])
        self.assertEqual(r["pointerdown"], [["contexto", "suspended"], ["resume"], ["cancelar"]])  # e cala
        self.assertEqual(r["pointerup"], [["resume"]])
        self.assertEqual(r["estado_depois_do_toque"], "running")
        self.assertEqual(r["pointerup_de_novo"], [])
        self.assertEqual(r["espaco"], [["resume"], ["cancelar"]])  # o espaço também começa a gravar
        self.assertEqual(r["formulario"], [["resume"], ["cancelar"], ["cancelar"], ["navegador", "Oi!"]])

    def test_pedido_novo_cala_o_anterior(self):
        r = rodar_pagina(PEDIDO_NOVO_JS)
        # as duas frases agendadas param, a voz do navegador também, e a frase que decodificava não toca
        self.assertEqual(r["pedido_novo"], [["parar"], ["parar"], ["cancelar"]])
        # o pedido antigo termina sem tocar nem falar e sem mexer no status do novo
        self.assertEqual(r["fim_do_antigo"], {"efeitos": [], "status": "Pensando…", "vermelho": False, "pensando": True,
                                              "historico": [[["voce", "primeiro"], ["ela", "Resposta A. Com duas frases."]]]})
        self.assertEqual(r["fim_do_novo"], {
            "efeitos": [["cancelar"], ["navegador", "Resposta B."]], "status": PRONTO, "vermelho": False,
            "pensando": False, "historico": [[["voce", "segundo"], ["ela", "Resposta B."]],
                                             [["voce", "primeiro"], ["ela", "Resposta A. Com duas frases."]]]})

    def test_comecar_a_gravar_cala_a_assistente(self):
        r = rodar_pagina(GRAVAR_CALA_JS)
        self.assertEqual(r["ao_gravar"][:2], [["parar"], ["cancelar"]])  # a frase agendada para
        # as frases que ainda chegam não tocam, e o fim não fala nada com a voz do navegador
        self.assertNotIn("tocar", [efeito[0] for efeito in r["depois"]])
        self.assertNotIn("navegador", [efeito[0] for efeito in r["depois"]])

    def test_falha_de_um_pedido_calado_nao_atrapalha_o_novo(self):
        r = rodar_pagina(FALHA_CALADA_JS)
        # sem aviso e sem a voz do navegador falando a resposta antiga por cima do pedido novo
        self.assertEqual(r["depois_da_falha"]["efeitos"], [])
        self.assertEqual(r["depois_da_falha"]["status"], "Pensando…")
        self.assertFalse(r["depois_da_falha"]["vermelho"])

    def test_streaming_que_para_no_meio(self):
        r = rodar_pagina(CORTES_JS)
        self.assertEqual(r["conexao_caiu"], {"status": CAIU, "vermelho": True, "pensando": False,
                                             "historico": [[["voce", "abre o spotify"]]], "fechou_a_conexao": False})
        self.assertEqual(r["fechou_sem_fim"]["status"], CAIU)
        self.assertTrue(r["fechou_sem_fim"]["vermelho"])
        self.assertFalse(r["fechou_sem_fim"]["fechou_a_conexao"])
        # uma linha inválida: a página desiste da resposta e fecha a conexão (o servidor para de gerar a voz)
        self.assertEqual(r["linha_invalida"], {
            "status": "O servidor mandou uma resposta que a página não entendeu. Tente de novo.", "vermelho": True,
            "pensando": False, "historico": [[["voce", "abre o spotify"]]], "fechou_a_conexao": True})
        self.assertNotIn(["navegador", "Abri."], r["efeitos"])  # sem o fim, não fala

    def test_queda_depois_do_fim_nao_apaga_o_status(self):
        r = rodar_pagina(QUEDA_DEPOIS_DO_FIM_JS)
        self.assertEqual(r["depois"], {"efeitos": [["cancelar"], ["navegador", "Abri o Spotify."]],
                                       "status": PRONTO, "vermelho": False, "pensando": False,
                                       "historico": [[["voce", "abre o spotify"], ["ela", "Abri o Spotify."]]]})

    def test_so_o_content_type_ndjson_vira_streaming(self):
        r = rodar_pagina(TIPOS_JS)
        fim = {"tipo": "fim"}
        self.assertEqual(r["ndjson"], {"dados": None, "eventos": [fim]})
        self.assertEqual(r["maiusculas"], {"dados": None, "eventos": [fim]})
        self.assertEqual(r["parecido"], {"dados": fim, "eventos": []})
        self.assertEqual(r["json"], {"dados": fim, "eventos": []})

    def test_acoes_fora_do_padrao_nao_viram_erro_em_ingles(self):
        r = rodar_pagina(ACOES_FORA_DO_PADRAO_JS)
        self.assertEqual(r["estado"], {"status": PRONTO, "vermelho": False, "pensando": False,
                                       "historico": [[["voce", "oi"], ["ela", "Oi!"]]]})


if __name__ == "__main__":
    unittest.main()
