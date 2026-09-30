"""
Testes da palavra de ativação "hey Jarvis" (celular_servidor/static/ativacao/jarvis.js, a integração no
index.html e o baixar_ativacao.py). O JavaScript roda no Node, como os testes da página, com sessões ONNX,
microfone, AudioWorklet e onnxruntime-web falsos: a suíte não precisa de internet nem dos modelos .onnx.
Os downloads vão para um servidor HTTP falso em 127.0.0.1.
"""
import array
import base64
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import wave
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from tests.auxiliares import PASTA_SERVIDOR, RAIZ, ServidorLocal, importar_copia
from tests.test_pagina import INDEX, NODE, NODE_18, _rodar_js, js, rodar_pagina

JARVIS = PASTA_SERVIDOR / "static" / "ativacao" / "jarvis.js"
DIGA = "Diga “hey Jarvis”…"
PRONTO = "Segure o botão e fale"
GESTO = "Toque na página para ligar a escuta: o navegador só libera o som depois de um toque."
SEM_FALA = "Não ouvi nada. Diga “hey Jarvis” de novo."
PRECISA_NODE = unittest.skipUnless(NODE, "Node não está instalado; os testes do JavaScript foram pulados")
PRECISA_NODE_18 = unittest.skipUnless(NODE_18, "sem o Node 18 ou mais novo (Response, Blob e ReadableStream)")


def rodar_jarvis(codigo: str):
    """Roda o jarvis.js seguido de `codigo` no Node e devolve o JSON impresso."""
    return _rodar_js("jarvis", JARVIS.read_text(encoding="utf-8") + "\n" + codigo)


def mensagens() -> dict:
    return rodar_jarvis("console.log(JSON.stringify(Jarvis.MENSAGENS));")


def ler_wav(dados: bytes) -> tuple[dict, array.array]:
    with wave.open(io.BytesIO(dados), "rb") as w:
        info = {"taxa": w.getframerate(), "canais": w.getnchannels(), "bytes_por_amostra": w.getsampwidth(),
                "amostras": w.getnframes()}
        return info, array.array("h", w.readframes(w.getnframes()))


def rms(amostras) -> float:
    return math.sqrt(sum(x * x for x in amostras) / len(amostras)) if len(amostras) else 0.0


# ---------------------------------------------------------------------------------------------------
# Reamostragem, PCM e WAV
# ---------------------------------------------------------------------------------------------------

REAMOSTRAGEM_JS = js("""
const { Reamostrador, juntar } = Jarvis;
const seno = (taxa, freq, n, amp = 0.5) => Float32Array.from({ length: n }, (_, i) => amp * Math.sin(2 * Math.PI * freq * i / taxa));
const rms = x => Math.sqrt(x.reduce((s, v) => s + v * v, 0) / x.length);
function tudo(r, x, tamanhos) {
  const partes = [];
  for (let i = 0, k = 0; i < x.length; k++) {
    const n = tamanhos[k % tamanhos.length];
    partes.push(r.processar(x.subarray(i, i + n)));
    i += n;
  }
  partes.push(r.terminar());
  return juntar(partes, Float32Array);
}
// Frequência pelas subidas por zero, com interpolação linear entre as amostras.
function frequencia(x, taxa) {
  const cruzamentos = [];
  for (let i = 1; i < x.length; i++) if (x[i - 1] < 0 && x[i] >= 0) cruzamentos.push(i - 1 + x[i - 1] / (x[i - 1] - x[i]));
  return (cruzamentos.length - 1) * taxa / (cruzamentos[cruzamentos.length - 1] - cruzamentos[0]);
}
for (const taxa of [48000, 44100]) {
  const x = seno(taxa, 1000, taxa);  // 1 s de 1 kHz
  const y = tudo(new Reamostrador(taxa), x, [128]);
  const meio = y.subarray(800, 15200);
  let erroDoIdeal = 0;  // contra o seno ideal a 16 kHz: frequência, amplitude e alinhamento de uma vez
  for (let n = 800; n < 15200; n++) erroDoIdeal = Math.max(erroDoIdeal, Math.abs(y[n] - 0.5 * Math.sin(2 * Math.PI * 1000 * n / 16000)));
  const acima = tudo(new Reamostrador(taxa), seno(taxa, 10000, taxa), [2048]).subarray(800, 15200);
  // A fala vai até perto de 8 kHz: 6 kHz passa inteiro; 9 kHz (que viraria 7 kHz) some.
  const ganho = f => rms(tudo(new Reamostrador(taxa), seno(taxa, f, taxa), [2048]).subarray(800, 15200)) / (0.5 / Math.SQRT2);
  const ganhos = { "6000": ganho(6000), "9000": ganho(9000) };
  const iguais = [[1, 7, 2048, 333], [taxa]].map(t => {
    const z = tudo(new Reamostrador(taxa), x, t);
    return z.length === y.length && z.every((v, i) => Math.abs(v - y[i]) < 1e-6);
  });
  saida[taxa] = { comprimento: y.length, frequencia: frequencia(meio, 16000), rms: rms(meio), erro_do_ideal: erroDoIdeal,
                  rms_10khz: rms(acima), ganhos, pedacos_dao_o_mesmo: iguais };
}
saida.comprimentos = [1, 2, 3, 441, 1000, 12345].map(n => tudo(new Reamostrador(44100), new Float32Array(n), [4096]).length);
const direto = new Reamostrador(16000);
const x16 = seno(16000, 440, 1000);
const y16 = juntar([direto.processar(x16.subarray(0, 300)), direto.processar(x16.subarray(300)), direto.terminar()], Float32Array);
saida.direto = { igual: y16.length === 1000 && y16.every((v, i) => v === x16[i]) };
saida.invalidas = [0, -1, 44100.5, NaN].map(t => { try { new Reamostrador(t); return "aceitou"; } catch (e) { return e.name; } });
""")


@PRECISA_NODE
class TestReamostragem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = rodar_jarvis(REAMOSTRAGEM_JS)

    def test_senoide_mantem_a_frequencia_e_a_amplitude(self):
        for taxa in ("48000", "44100"):
            with self.subTest(taxa=taxa):
                self.assertAlmostEqual(self.r[taxa]["frequencia"], 1000, delta=0.5)
                self.assertAlmostEqual(self.r[taxa]["rms"], 0.5 / math.sqrt(2), delta=0.002)
                self.assertLess(self.r[taxa]["erro_do_ideal"], 0.005)  # sem atraso: a amostra n é o instante n/16000

    def test_comprimento_certo(self):
        self.assertEqual(self.r["48000"]["comprimento"], 16000)
        self.assertEqual(self.r["44100"]["comprimento"], 16000)
        self.assertEqual(self.r["comprimentos"], [math.ceil(n * 160 / 441) for n in (1, 2, 3, 441, 1000, 12345)])

    def test_passa_baixa_corta_o_que_passa_de_8_khz(self):
        # 10 kHz não existe a 16 kHz: sem o filtro, viraria um tom de 6 kHz com a amplitude inteira (0,35 de RMS)
        for taxa in ("48000", "44100"):
            with self.subTest(taxa=taxa):
                self.assertLess(self.r[taxa]["rms_10khz"], 0.001)
                self.assertLess(self.r[taxa]["ganhos"]["9000"], 0.001)  # abaixo de -60 dB

    def test_passa_baixa_nao_corta_a_fala(self):
        # um filtro baixo demais (4 kHz, por exemplo) mudaria o melspectrogram e as pontuações
        for taxa in ("48000", "44100"):
            with self.subTest(taxa=taxa):
                self.assertAlmostEqual(self.r[taxa]["ganhos"]["6000"], 1, delta=0.02)

    def test_pedacos_de_qualquer_tamanho_dao_o_mesmo_resultado(self):
        for taxa in ("48000", "44100"):
            with self.subTest(taxa=taxa):
                self.assertEqual(self.r[taxa]["pedacos_dao_o_mesmo"], [True, True])

    def test_16_khz_passa_direto_e_taxa_invalida_e_recusada(self):
        self.assertTrue(self.r["direto"]["igual"])
        self.assertEqual(self.r["invalidas"], ["RangeError"] * 4)


WAV_JS = js("""
const amostras = Int16Array.from([0, 1, -1, 32767, -32768, 1234, -4321]);
saida.wav = Buffer.from(Jarvis.criarWav(amostras)).toString("base64");
saida.wav_8k = Buffer.from(Jarvis.criarWav(amostras, 8000)).toString("base64");
saida.int16 = Array.from(Jarvis.paraInt16(Float32Array.from([0, 0.5, -0.5, 1, -1, 1.5, -2, 1 / 32768, -1 / 65536, 0.99999])));
""")


@PRECISA_NODE
class TestWav(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = rodar_jarvis(WAV_JS)

    def test_cabecalho_de_44_bytes(self):
        dados = base64.b64decode(self.r["wav"])
        self.assertEqual(len(dados), 44 + 7 * 2)
        self.assertEqual(dados[:4], b"RIFF")
        self.assertEqual(int.from_bytes(dados[4:8], "little"), 36 + 14)
        self.assertEqual(dados[8:16], b"WAVEfmt ")
        campos = [int.from_bytes(dados[i:i + n], "little") for i, n in ((16, 4), (20, 2), (22, 2), (24, 4), (28, 4), (32, 2), (34, 2))]
        self.assertEqual(campos, [16, 1, 1, 16000, 32000, 2, 16])  # PCM, mono, 16 kHz, 16 bits
        self.assertEqual(dados[36:40], b"data")
        self.assertEqual(int.from_bytes(dados[40:44], "little"), 14)

    def test_amostras_em_little_endian_e_o_modulo_wave_le(self):
        info, amostras = ler_wav(base64.b64decode(self.r["wav"]))
        self.assertEqual(info, {"taxa": 16000, "canais": 1, "bytes_por_amostra": 2, "amostras": 7})
        self.assertEqual(list(amostras), [0, 1, -1, 32767, -32768, 1234, -4321])
        self.assertEqual(ler_wav(base64.b64decode(self.r["wav_8k"]))[0]["taxa"], 8000)

    def test_float_para_int16(self):
        self.assertEqual(self.r["int16"], [0, 16384, -16384, 32767, -32768, 32767, -32768, 1, 0, 32767])


# ---------------------------------------------------------------------------------------------------
# Detector de voz por energia
# ---------------------------------------------------------------------------------------------------

VOZ_JS = js("""
const { DetectorDeVoz } = Jarvis;
let semente = 1;
const aleatorio = () => ((semente = (semente * 1103515245 + 12345) % 2147483648) / 2147483648) * 2 - 1;
// Pedaços de 1280 amostras (80 ms), como a escuta entrega: ruído de fundo ou "fala" (um tom com o RMS dado).
function audio(segundos, nivel, tom = 0) {
  const n = Math.round(segundos * 16000);
  return Int16Array.from({ length: n }, (_, i) => Math.round(tom ? tom * Math.SQRT2 * Math.sin(2 * Math.PI * 220 * i / 16000)
                                                                  : nivel * Math.sqrt(3) * aleatorio()));
}
function pedacos(amostras) {
  const saida = [];
  for (let i = 0; i < amostras.length; i += 1280) saida.push(amostras.subarray(i, i + 1280));
  return saida;
}
// Grava `partes` (uma lista de áudios) e diz em que segundo e por quê a gravação acabou.
function gravar(d, partes) {
  let lidas = 0;
  for (const p of pedacos(Jarvis.juntar(partes))) {
    lidas += p.length;
    const fim = d.ouvir(p);
    if (fim) return { fim, segundos: lidas / 16000 };
  }
  return { fim: null, segundos: lidas / 16000 };
}
function preparado(ruidoDeFundo = 30) {
  const d = new DetectorDeVoz();
  for (const p of pedacos(audio(3, ruidoDeFundo))) d.ouvirRuido(p);
  d.comecar();
  return d;
}
const d = preparado();
saida.limiares = { ruido: d.ruido(), fala: d.limiarFala, silencio: d.limiarSilencio };
saida.fala_e_silencio = gravar(preparado(), [audio(0.3, 30), audio(1.2, 0, 3000), audio(3, 30)]);
saida.pausa_curta = gravar(preparado(), [audio(0.5, 0, 3000), audio(0.5, 30), audio(0.5, 0, 3000), audio(3, 30)]);
saida.sem_fala = gravar(preparado(), [audio(6, 30)]);
saida.maximo = gravar(preparado(), [audio(10, 0, 3000)]);
saida.bipe = gravar(preparado(), [audio(0.12, 0, 3000), audio(6, 30)]);
// Ruído de fundo alto: a fala precisa passar de 3x ele.
const barulho = preparado(400);
saida.barulho = { fala: barulho.limiarFala, silencio: barulho.limiarSilencio };
saida.barulho_fala_baixa = gravar(preparado(400), [audio(1, 0, 900), audio(5, 400)]);
saida.barulho_fala_alta = gravar(preparado(400), [audio(1, 0, 3000), audio(3, 400)]);
// O "hey Jarvis" enquanto espera não sobe a estimativa do ruído (é o 20º percentil dos últimos 5 s).
const comFala = new DetectorDeVoz();
for (const p of pedacos(Jarvis.juntar([audio(3.5, 30), audio(1.5, 0, 3000)]))) comFala.ouvirRuido(p);
saida.ruido_com_fala = comFala.ruido();
const sala = new DetectorDeVoz();  // sala muito quieta: valem os mínimos
for (const p of pedacos(audio(2, 0))) sala.ouvirRuido(p);
sala.comecar();
saida.sala_quieta = { fala: sala.limiarFala, silencio: sala.limiarSilencio };
saida.sem_estimativa = new DetectorDeVoz().ruido();
// Pedaços de qualquer tamanho: a sobra de um quadro espera o pedaço seguinte.
const irregular = preparado();
const tudo = Jarvis.juntar([audio(0.3, 30), audio(1.2, 0, 3000), audio(3, 30)]);
let lidas = 0, fimIrregular = null;
for (const n of [100, 333, 7, 2000, 1280, 999].flatMap(x => Array(40).fill(x))) {
  if (lidas >= tudo.length) break;
  const p = tudo.subarray(lidas, lidas + n);
  lidas += p.length;
  const fim = irregular.ouvir(p);
  if (fim) { fimIrregular = { fim, segundos: lidas / 16000 }; break; }
}
saida.irregular = fimIrregular;
""")


@PRECISA_NODE
class TestDetectorDeVoz(unittest.TestCase):
    """O fim do comando: ~0,8 s de silêncio depois de fala, no máximo ~8 s, e desiste em ~4 s sem fala."""

    @classmethod
    def setUpClass(cls):
        cls.r = rodar_jarvis(VOZ_JS)

    def test_limiares_vem_do_ruido_estimado_enquanto_espera(self):
        self.assertAlmostEqual(self.r["limiares"]["ruido"], 30, delta=8)
        self.assertEqual(self.r["limiares"]["fala"], 200)  # 3x o ruído ficaria abaixo do mínimo
        self.assertEqual(self.r["limiares"]["silencio"], 120)
        self.assertAlmostEqual(self.r["barulho"]["fala"], 1200, delta=250)
        self.assertAlmostEqual(self.r["barulho"]["silencio"], 800, delta=170)
        self.assertEqual(self.r["sala_quieta"], {"fala": 200, "silencio": 120})
        self.assertEqual(self.r["sem_estimativa"], 0)

    def test_termina_depois_de_08_s_de_silencio_que_vem_depois_da_fala(self):
        self.assertEqual(self.r["fala_e_silencio"]["fim"], "silencio")
        self.assertAlmostEqual(self.r["fala_e_silencio"]["segundos"], 0.3 + 1.2 + 0.8, delta=0.09)
        self.assertEqual(self.r["irregular"]["fim"], "silencio")
        self.assertAlmostEqual(self.r["irregular"]["segundos"], 2.3, delta=0.13)

    def test_pausa_curta_no_meio_da_fala_nao_termina(self):
        self.assertEqual(self.r["pausa_curta"]["fim"], "silencio")
        self.assertAlmostEqual(self.r["pausa_curta"]["segundos"], 1.5 + 0.8, delta=0.09)

    def test_sem_fala_desiste_em_4_s(self):
        self.assertEqual(self.r["sem_fala"], {"fim": "sem_fala", "segundos": 4.0})

    def test_fala_sem_parar_termina_em_8_s(self):
        self.assertEqual(self.r["maximo"], {"fim": "maximo", "segundos": 8.0})

    def test_bipe_curto_nao_conta_como_fala(self):
        self.assertEqual(self.r["bipe"], {"fim": "sem_fala", "segundos": 4.0})

    def test_com_ruido_alto_so_a_fala_bem_acima_dele_conta(self):
        self.assertEqual(self.r["barulho_fala_baixa"]["fim"], "sem_fala")
        self.assertEqual(self.r["barulho_fala_alta"]["fim"], "silencio")
        self.assertAlmostEqual(self.r["barulho_fala_alta"]["segundos"], 1.8, delta=0.09)

    def test_a_palavra_dita_enquanto_espera_nao_sobe_o_ruido(self):
        self.assertAlmostEqual(self.r["ruido_com_fala"], 30, delta=8)


# ---------------------------------------------------------------------------------------------------
# O pipeline do openWakeWord, com sessões ONNX falsas
# ---------------------------------------------------------------------------------------------------

PIPELINE_JS = js("""
class TensorFalso { constructor(tipo, dados, formato) { this.type = tipo; this.data = dados; this.dims = formato; } }
const chamadas = { mel: [], embedding: [], classificador: [] };
const pontuacoes = [];
let atrasoDoMel = 0;
// O melspectrogram falso devolve os quadros que o de verdade devolveria para o tamanho da entrada; o valor de
// cada banda é 10 * (100 * chamada + quadro) + 30, que vira 100 * chamada + quadro + 5 depois de x / 10 + 2.
function sessao(nome, entrada, saidaNome, calcular) {
  return { inputNames: [entrada], outputNames: [saidaNome, "outra"], run: async feeds => {
    const t = feeds[entrada];
    if (!t || Object.keys(feeds).length !== 1) throw new Error("entrada com o nome errado");
    chamadas[nome].push({ tipo: t.type, formato: t.dims, dados: t.data, float32: t.data instanceof Float32Array });
    if (nome === "mel" && atrasoDoMel) await new Promise(ok => setTimeout(ok, atrasoDoMel));
    return { [saidaNome]: { data: calcular(t, chamadas[nome].length - 1) }, outra: { data: new Float32Array([-1]) } };
  } };
}
const sessoes = {
  mel: sessao("mel", "input", "output", (t, k) => {
    const quadros = Math.floor((t.dims[1] - 512) / 160) + 1;
    const d = new Float32Array(quadros * 32);
    for (let q = 0; q < quadros; q++) d.fill(10 * (100 * k + q) + 30, q * 32, (q + 1) * 32);
    return d;
  }),
  embedding: sessao("embedding", "input_1", "conv2d_19", (t, k) => new Float32Array(96).fill(k + 1)),
  classificador: sessao("classificador", "x.1", "53", () => new Float32Array([pontuacoes.length ? pontuacoes.shift() : 0.125])),
};
const p = new Jarvis.PipelineJarvis({ sessoes, Tensor: TensorFalso });
const pedaco = k => Int16Array.from({ length: 1280 }, (_, i) => ((k * 1280 + i) % 2000) - 1000);
const linhas = dados => Array.from({ length: dados.length / 32 }, (_, r) => {
  const linha = Array.from(dados.subarray(r * 32, r * 32 + 32));
  return linha.every(v => v === linha[0]) ? linha[0] : "misturada";
});
const resultados = [];
for (let k = 0; k < 20; k++) resultados.push(await p.processar(pedaco(k)));
saida.resultados = resultados;
saida.contagem = { mel: chamadas.mel.length, embedding: chamadas.embedding.length, classificador: chamadas.classificador.length };
saida.mel = chamadas.mel.slice(0, 3).map(c => ({ tipo: c.tipo, formato: c.formato, float32: c.float32 }));
saida.mel_valores = {
  primeiro: Array.from(chamadas.mel[0].dados).every((v, i) => v === pedaco(0)[i]),
  contexto: Array.from(chamadas.mel[1].dados.subarray(0, 480)).every((v, i) => v === pedaco(0)[800 + i]),
  novo: Array.from(chamadas.mel[1].dados.subarray(480)).every((v, i) => v === pedaco(1)[i]),
};
saida.embedding = chamadas.embedding.slice(0, 2).map(c => ({ tipo: c.tipo, formato: c.formato }));
saida.janelas = [0, 1, 8, 9].map(k => linhas(chamadas.embedding[k].dados));
saida.classificador = chamadas.classificador.slice(0, 2).map(c => ({ tipo: c.tipo, formato: c.formato,
  linhas: Array.from({ length: 16 }, (_, r) => c.dados[r * 96]), iguais: Array.from({ length: 16 }, (_, r) => new Set(c.dados.subarray(r * 96, r * 96 + 96)).size) }));
// O limiar: 0,5 já é detecção
pontuacoes.push(0.25, 0.4999, 0.5);
saida.limiar = [];
for (let k = 20; k < 23; k++) saida.limiar.push(await p.processar(pedaco(k)));
// Depois da detecção: buffers zerados e 25 pedaços sem classificador
const antes = { mel: chamadas.mel.length, classificador: chamadas.classificador.length };
const pausa = [];
for (let k = 0; k < 25; k++) pausa.push(await p.processar(pedaco(100 + k)));
saida.pausa = { nulos: pausa.every(r => r.pontuacao === null && !r.detectou), classificador: chamadas.classificador.length - antes.classificador,
                mel_sem_contexto: chamadas.mel[antes.mel].formato, janela_zerada: linhas(chamadas.embedding[antes.mel].dados) };
pontuacoes.push(0.375);
saida.depois_da_pausa = await p.processar(pedaco(125));
// A pausa conta desde a detecção: os pedaços da gravação do comando (descontarPausa) entram na conta, e aí
// só falta juntar 16 embeddings de novo (1,28 s).
pontuacoes.push(0.5);
saida.detectou_de_novo = await p.processar(pedaco(126));
for (let k = 0; k < 30; k++) p.descontarPausa();  // 2,4 s de gravação
saida.pausa_depois_da_gravacao = p.pausa;
const retomada = [];
for (let k = 0; k < 16; k++) retomada.push((await p.processar(pedaco(130 + k))).pontuacao);
saida.retomada = retomada;
// zerar com o ONNX rodando: o resultado atrasado é descartado
atrasoDoMel = 20;
const pendente = p.processar(pedaco(200));
p.reiniciar();
saida.zerado_no_meio = { resultado: await pendente, embeddings: p.embeddings.length, mel_de_uns: p.mel.every(q => q.every(v => v === 1)) };
atrasoDoMel = 0;
try { await p.processar(new Int16Array(1000)); saida.pedaco_errado = "aceitou"; } catch (e) { saida.pedaco_errado = e.name; }
""")


@PRECISA_NODE
class TestPipeline(unittest.TestCase):
    """O mesmo cálculo do openwakeword: formatos, transformação, janela, passo, limiar e pausa."""

    @classmethod
    def setUpClass(cls):
        cls.r = rodar_jarvis(PIPELINE_JS)

    def test_cada_modelo_roda_quantas_vezes(self):
        # um melspectrogram e um embedding por pedaço; o classificador só com 16 embeddings de verdade
        self.assertEqual(self.r["contagem"], {"mel": 20, "embedding": 20, "classificador": 5})
        nulos = [r["pontuacao"] for r in self.r["resultados"][:15]]
        self.assertEqual(nulos, [None] * 15)
        self.assertEqual(self.r["resultados"][15], {"pontuacao": 0.125, "detectou": False})

    def test_entrada_do_melspectrogram(self):
        # float32 com os valores do int16 (sem dividir por 32768); 1280 no primeiro pedaço, depois 480 + 1280
        self.assertEqual(self.r["mel"], [{"tipo": "float32", "formato": [1, 1280], "float32": True},
                                         {"tipo": "float32", "formato": [1, 1760], "float32": True},
                                         {"tipo": "float32", "formato": [1, 1760], "float32": True}])
        self.assertEqual(self.r["mel_valores"], {"primeiro": True, "contexto": True, "novo": True})

    def test_transformacao_janela_e_passo_do_embedding(self):
        self.assertEqual(self.r["embedding"], [{"tipo": "float32", "formato": [1, 76, 32, 1]}] * 2)
        uns, primeiro, segundo = [1] * 71, [5, 6, 7, 8, 9], [105 + q for q in range(8)]
        janelas = self.r["janelas"]
        self.assertEqual(janelas[0], uns + primeiro)  # começa com 76 linhas de 1; o 1º pedaço dá 5 quadros
        self.assertEqual(janelas[1], [1] * 63 + primeiro + segundo)  # e cada pedaço seguinte, 8
        self.assertEqual(janelas[2], [1] * 7 + primeiro + [100 * k + q + 5 for k in range(1, 9) for q in range(8)])
        self.assertEqual(janelas[3], primeiro[1:] + [100 * k + q + 5 for k in range(1, 10) for q in range(8)])

    def test_classificador_recebe_os_16_ultimos_embeddings(self):
        c = self.r["classificador"]
        self.assertEqual([x["formato"] for x in c], [[1, 16, 96]] * 2)
        self.assertEqual(c[0]["linhas"], list(range(1, 17)))  # em ordem, do mais antigo ao mais novo
        self.assertEqual(c[1]["linhas"], list(range(2, 18)))  # a janela anda um embedding por pedaço
        self.assertEqual(c[0]["iguais"], [1] * 16)

    def test_limiar_de_05(self):
        self.assertEqual(self.r["limiar"], [{"pontuacao": 0.25, "detectou": False},
                                            {"pontuacao": 0.4999000132083893, "detectou": False},
                                            {"pontuacao": 0.5, "detectou": True}])

    def test_pausa_de_2_s_e_buffers_zerados_depois_da_deteccao(self):
        self.assertEqual(self.r["pausa"], {"nulos": True, "classificador": 0, "mel_sem_contexto": [1, 1280],
                                           "janela_zerada": [1] * 71 + [100 * 23 + q + 5 for q in range(5)]})  # 23ª chamada, sem os quadros antigos
        self.assertEqual(self.r["depois_da_pausa"], {"pontuacao": 0.375, "detectou": False})

    def test_a_pausa_conta_desde_a_deteccao(self):
        self.assertEqual(self.r["detectou_de_novo"], {"pontuacao": 0.5, "detectou": True})
        self.assertEqual(self.r["pausa_depois_da_gravacao"], 0)  # não fica negativa
        # sem a pausa, o classificador volta assim que há 16 embeddings (no 16º pedaço), e não 25 pedaços depois
        self.assertEqual(self.r["retomada"], [None] * 15 + [0.125])

    def test_zerar_no_meio_descarta_o_resultado_atrasado(self):
        self.assertEqual(self.r["zerado_no_meio"], {"resultado": {"pontuacao": None, "detectou": False},
                                                    "embeddings": 0, "mel_de_uns": True})
        self.assertEqual(self.r["pedaco_errado"], "RangeError")


PROCESSADOR_JS = js("""
const enviadas = [];
let registrado = null, Classe = null;
globalThis.AudioWorkletProcessor = class {
  constructor() { this.port = { onmessage: null, postMessage: (m, transferir) => enviadas.push({
    tamanho: m.length, valores: Array.from(m.subarray(0, 3)).concat(Array.from(m.subarray(-2))), transferido: !!transferir && transferir[0] === m.buffer }) }; }
};
globalThis.registerProcessor = (nome, c) => { registrado = nome; Classe = c; };
new Function(Jarvis.CODIGO_DO_PROCESSADOR)();
const p = new Classe({ processorOptions: { tamanho: 256 } });
const bloco = deslocamento => [Float32Array.from({ length: 128 }, (_, i) => i + deslocamento), Float32Array.from({ length: 128 }, (_, i) => i + deslocamento + 1)];
saida.registrado = registrado;
saida.retornos = [p.process([bloco(0)]), p.process([bloco(128)]), p.process([[]]), p.process([])];
saida.enviadas = enviadas.splice(0);
const mono = new Classe({});
for (let k = 0; k < 16; k++) mono.process([[new Float32Array(128).fill(0.25)]]);
saida.padrao = enviadas.splice(0).map(m => m.tamanho);
p.port.onmessage({ data: "parar" });
saida.parado = { retorno: p.process([bloco(0)]), enviadas: enviadas.length };
""")


@PRECISA_NODE
class TestProcessadorDeCaptura(unittest.TestCase):
    def test_junta_a_media_dos_canais_em_blocos_e_para(self):
        r = rodar_jarvis(PROCESSADOR_JS)
        self.assertEqual(r["registrado"], "captura-jarvis")
        self.assertEqual(r["retornos"], [True, True, True, True])
        # dois blocos de 128 viram uma mensagem de 256, com a média de esquerda e direita, transferida sem cópia
        self.assertEqual(r["enviadas"], [{"tamanho": 256, "valores": [0.5, 1.5, 2.5, 254.5, 255.5], "transferido": True}])
        self.assertEqual(r["padrao"], [2048])  # 16 blocos de 128
        self.assertEqual(r["parado"], {"retorno": False, "enviadas": 0})


FILA_JS = js("""
// A escuta com um pipeline lento (o ONNX de um aparelho fraco): o áudio que chega enquanto ele calcula fica
// na fila e, se a palavra for detectada, já é o começo do comando.
const avisos = [];
const e = new Jarvis.Escuta({
  aoDetectar: () => avisos.push("detectou"),
  aoComando: (wav, motivo) => avisos.push(["comando", Buffer.from(wav).toString("base64"), motivo]),
  aoSemFala: () => avisos.push("sem_fala"),
  aoErro: m => avisos.push(["erro", m]),
});
const pendentes = [];
Object.assign(e, {
  estado: "esperando", fila: [], gravacao: [], resto: new Int16Array(0), processando: false, geracao: 1,
  reamostrador: new Jarvis.Reamostrador(16000), voz: new Jarvis.DetectorDeVoz(),
  pipeline: { processar: () => new Promise(ok => pendentes.push(ok)), reiniciar() {}, descontados: 0,
              descontarPausa() { this.descontados++; } },
});
const pedaco = valor => new Float32Array(1280).fill(valor / 32768);
e.receber(pedaco(100));   // A: vai para o pipeline
e.receber(pedaco(5000));  // B e C chegam enquanto ele calcula
e.receber(pedaco(-5000));
saida.antes = { fila: e.fila.length, pipeline: pendentes.length, estado: e.estado };
pendentes[0]({ pontuacao: 0.9, detectou: true });  // A tinha o "hey Jarvis"
await new Promise(ok => setTimeout(ok, 0));
saida.detectou = { avisos: avisos.slice(), fila: e.fila.length, gravados: e.gravacao.length, estado: e.estado, pipeline: pendentes.length };
for (let i = 0; i < 12; i++) e.receber(pedaco(0));  // ~1 s de silêncio
saida.avisos = avisos.map(a => Array.isArray(a) ? [a[0], a[2]] : a);
saida.wav = (avisos.find(a => a[0] === "comando") || [])[1] || null;
saida.depois = { estado: e.estado, fila: e.fila.length, pipeline: pendentes.length, descontados: e.pipeline.descontados };
""")


@PRECISA_NODE_18
class TestEscutaComPipelineLento(unittest.TestCase):
    def test_audio_que_chegou_durante_o_calculo_vira_o_comeco_do_comando(self):
        r = rodar_jarvis(FILA_JS)
        self.assertEqual(r["antes"], {"fila": 2, "pipeline": 1, "estado": "esperando"})
        self.assertEqual(r["detectou"], {"avisos": ["detectou"], "fila": 0, "gravados": 2, "estado": "gravando", "pipeline": 1})
        self.assertEqual(r["avisos"], ["detectou", ["comando", "silencio"]])
        info, amostras = ler_wav(base64.b64decode(r["wav"]))
        self.assertEqual(list(amostras[:1280]), [5000] * 1280)  # B
        self.assertEqual(list(amostras[1280:2560]), [-5000] * 1280)  # C
        self.assertEqual(info["amostras"], 2 * 1280 + 10 * 1280)  # e 0,8 s de silêncio
        self.assertEqual(r["depois"]["estado"], "esperando")
        self.assertEqual(r["depois"]["descontados"], 12)  # cada pedaço gravado conta para a pausa do detector


# ---------------------------------------------------------------------------------------------------
# A página: a escuta ligada no navegador falso
# ---------------------------------------------------------------------------------------------------

# Completa o navegador falso de test_pagina.py com o que a escuta usa: localStorage, microfone, AudioWorklet,
# o onnxruntime-web (um script da CDN que o teste deixa carregar ou falhar) e os modelos no servidor.
ANTES_JS = r"""
const armazenamento = new Map();
let armazenamentoBloqueado = false;
Object.defineProperty(globalThis, "localStorage", { configurable: true, get() {
  if (armazenamentoBloqueado) throw new DOMException("O acesso foi negado", "SecurityError");
  return { getItem: k => (armazenamento.has(k) ? armazenamento.get(k) : null), setItem: (k, v) => armazenamento.set(k, String(v)) };
} });

// Microfone falso: cada getUserMedia entrega uma faixa nova; `falha` faz o pedido falhar com esse erro.
const microfone = { pedidos: [], faixas: [], falha: null };
const midia = { getUserMedia: async restricoes => {
  microfone.pedidos.push(restricoes);
  if (microfone.falha) throw new DOMException("falhou", microfone.falha);
  const faixa = { parada: false, onended: null, stop() { this.parada = true; registro.push(["microfone solto"]); } };
  microfone.faixas.push(faixa);
  return { getTracks: () => [faixa], getAudioTracks: () => [faixa] };
} };
Object.defineProperty(navigator, "mediaDevices", { configurable: true, writable: true, value: midia });

// Web Audio: o AudioWorklet, a entrada do microfone e o bipe.
const modulos = [];
let semWorklet = false;
ContextoFalso.prototype.sampleRate = 48000;
Object.defineProperty(ContextoFalso.prototype, "audioWorklet", { get() {
  return semWorklet ? undefined : { addModule: async url => { modulos.push(url); } };
} });
ContextoFalso.prototype.createMediaStreamSource = function (fluxo) {
  return { fluxo, destino: null, connect(d) { this.destino = d; }, disconnect() { registro.push(["fonte desligada"]); } };
};
ContextoFalso.prototype.createOscillator = function () {
  return { frequency: { value: 0 }, connect() {}, start() { registro.push(["bipe", this.frequency.value]); }, stop() {} };
};
ContextoFalso.prototype.createGain = function () {
  const volume = { maximo: 0 };
  return { gain: { setValueAtTime() {}, linearRampToValueAtTime(v) { volume.maximo = Math.max(volume.maximo, v); if (v) registro.push(["volume do bipe", v]); } },
           connect() {} };
};
const nos = [];
class NoFalso {
  constructor(contexto, nome, opcoes) {
    this.nome = nome; this.opcoes = opcoes; this.mensagens = []; this.destino = null; this.desligado = false;
    this.port = { onmessage: null, postMessage: m => this.mensagens.push(m) };
    nos.push(this);
  }
  connect(d) { this.destino = d; }
  disconnect() { this.desligado = true; }
}
globalThis.AudioWorkletNode = NoFalso;

// O onnxruntime-web falso: o melspectrogram devolve os quadros certos para o tamanho da entrada, e o
// classificador devolve ort.pontuacao (o teste sobe para 0,9 quando "alguém diz hey Jarvis").
const ortEstado = { carga: "ok", falhaAoCriar: null, falhaAoRodar: null, pontuacao: 0.01, rodadas: 0, criados: [] };
const ortFalso = {
  env: { wasm: {} },
  Tensor: class { constructor(tipo, dados, formato) { this.type = tipo; this.data = dados; this.dims = formato; } },
  InferenceSession: { create: async (bytes, opcoes) => {
    const nome = new TextDecoder().decode(bytes);
    ortEstado.criados.push([nome, opcoes]);
    if (ortEstado.falhaAoCriar) throw new Error(ortEstado.falhaAoCriar);
    if (nome === "melspectrogram.onnx") return { inputNames: ["input"], outputNames: ["output"],
      run: async f => ({ output: { data: new Float32Array((Math.floor((f.input.dims[1] - 512) / 160) + 1) * 32) } }) };
    if (nome === "embedding_model.onnx") return { inputNames: ["input_1"], outputNames: ["conv2d_19"],
      run: async () => ({ conv2d_19: { data: new Float32Array(96) } }) };
    return { inputNames: ["x.1"], outputNames: ["53"], run: async () => {
      ortEstado.rodadas++;
      if (ortEstado.falhaAoRodar) throw new Error(ortEstado.falhaAoRodar);
      return { "53": { data: new Float32Array([ortEstado.pontuacao]) } };
    } };
  } },
};
const scripts = [];
document.head = { appendChild(s) {
  scripts.push({ src: s.src, integrity: s.integrity, crossOrigin: s.crossOrigin });
  setTimeout(() => { if (ortEstado.carga === "ok") { globalThis.ort = ortFalso; s.onload(); } else s.onerror(new Event("error")); }, 1);
} };

// Os modelos no servidor: "ok", "faltando" (404 do Flask), "erro" (500) ou "fora" (servidor desligado).
let modelosNoServidor = "ok";
const corpos = [];
const fetchDaPagina = globalThis.fetch;
globalThis.fetch = (url, opcoes = {}) => {
  if (String(url).startsWith("/static/ativacao/modelos/")) {
    if (modelosNoServidor === "fora") return Promise.reject(new TypeError("Failed to fetch"));
    if (modelosNoServidor === "faltando") return Promise.resolve(emJson(404, { erro: "Endereço não encontrado no servidor." }));
    if (modelosNoServidor === "erro") return Promise.resolve(emJson(500, { erro: "Erro interno." }));
    if (modelosNoServidor === "cortado") {  // a conexão cai no meio do arquivo
      return Promise.resolve(new Response(new ReadableStream({ pull(c) { c.error(new TypeError("network error")); } })));
    }
    return Promise.resolve(new Response(new TextEncoder().encode(String(url).split("/").pop())));
  }
  corpos.push(opcoes.body);
  return fetchDaPagina(url, opcoes);
};

// O botão de falar grava com um MediaRecorder falso (os pedaços são 3000 bytes de zeros).
globalThis.MediaRecorder = class {
  static isTypeSupported(t) { return t === "audio/webm;codecs=opus"; }
  constructor(fluxo, opcoes) { this.fluxo = fluxo; this.mimeType = opcoes && opcoes.mimeType; this.state = "inactive"; }
  start() { this.state = "recording"; }
  stop() { this.state = "inactive"; this.ondataavailable({ data: new Blob([new Uint8Array(3000)]) }); this.onstop(); }
};

// O microfone "ouve": blocos de 2048 amostras a 48 kHz, com um tom de 440 Hz (a "fala") ou só um chiado fraco.
let amostraDoMicrofone = 0;
async function ouvirMicrofone(segundos, amplitude = 0) {
  const total = Math.round(segundos * 48000);
  for (let i = 0; i < total; i += 2048) {
    const n = Math.min(2048, total - i);
    const bloco = new Float32Array(n);
    for (let k = 0; k < n; k++, amostraDoMicrofone++) {
      bloco[k] = amplitude * Math.sin(2 * Math.PI * 440 * amostraDoMicrofone / 48000) + 0.0003 * Math.sin(amostraDoMicrofone * 12.9898);
    }
    const no = nos[nos.length - 1];
    if (no && no.port.onmessage) no.port.onmessage({ data: bloco });
    await passar(0);
  }
}
// "hey Jarvis": o classificador passa do limiar no próximo pedaço.
async function heyJarvis() {
  ortEstado.pontuacao = 0.9;
  await ouvirMicrofone(0.1);
  ortEstado.pontuacao = 0.01;
}
async function ligarEscuta_() { elementos.ouvir.checked = true; elementos.ouvir.disparar("change"); await passar(20); }
async function desligarEscuta_() { elementos.ouvir.checked = false; elementos.ouvir.disparar("change"); await passar(); }
const ouvindo = () => elementos.falar.classList.contains("ouvindo");
const situacao = () => ({ ...estado(), ouvindo: ouvindo(), marcado: !!elementos.ouvir.checked,
                          guardado: armazenamento.get("ouvirJarvis") ?? null, escuta: escuta ? escuta.estado : null });
"""


def rodar_com_escuta(codigo: str, antes: str = ""):
    return rodar_pagina(js(codigo), antes=ANTES_JS + antes)


FLUXO_JS = """
const s = emPartes();
respostas.push(s.resposta);
saida.ao_carregar = { ...situacao(), microfone: microfone.pedidos.length, scripts: scripts.length };
await ligarEscuta_();
saida.ligou = { ...situacao(), efeitos: efeitos(), restricoes: microfone.pedidos, scripts, wasm: ortFalso.env.wasm,
  criados: ortEstado.criados, no: { nome: nos[0].nome, opcoes: nos[0].opcoes, saida_na_destination: nos[0].destino === contexto.destination } };
const { resolveObjectURL } = require("node:buffer");
saida.modulos = await Promise.all(modulos.map(async u => {
  const b = resolveObjectURL(u);
  return { blob: u.startsWith("blob:"), tipo: b.type, codigo: (await b.text()) === Jarvis.CODIGO_DO_PROCESSADOR };
}));
await ouvirMicrofone(2);  // chiado: nada dispara
saida.esperando = { ...situacao(), rodadas: ortEstado.rodadas, chamadas: chamadas.length };
await heyJarvis();
saida.detectou = { ...situacao(), efeitos: efeitos() };
await ouvirMicrofone(0.3);
await ouvirMicrofone(1.0, 0.2);  // "abre a calculadora"
saida.falando = situacao();
await ouvirMicrofone(1.0);  // silêncio: depois de ~0,8 s, manda
const voz = chamadas.find(c => c.url === "/voz");
const corpo = corpos[corpos.length - 1];
saida.enviou = { ...situacao(), metodo: voz && voz.metodo, cabecalhos: voz && voz.cabecalhos, conversa: CONVERSA,
                 tipo_do_corpo: corpo && corpo.type, wav: corpo && Buffer.from(await corpo.arrayBuffer()).toString("base64") };
await s.mandar(linha({ tipo: "transcricao", texto: "abre a calculadora" }),
               linha({ tipo: "resposta", texto: "Pronto, abri calculadora.", acoes: [] }), linha({ tipo: "fim" }));
await s.fechar();
saida.respondeu = { ...situacao(), efeitos: efeitos() };
// De volta à espera: a pausa de 2 s desde a detecção passou durante a gravação (2,3 s), mas os buffers
// zerados precisam de 16 pedaços novos (1,28 s) antes de o classificador rodar: até lá, nada dispara.
ortEstado.pontuacao = 0.9;
await ouvirMicrofone(1.1);
saida.na_pausa = situacao();
await ouvirMicrofone(0.4);
saida.depois_da_pausa = { ...situacao(), efeitos: efeitos() };
ortEstado.pontuacao = 0.01;
await desligarEscuta_();
saida.desligou = { ...situacao(), efeitos: efeitos(), no: { desligado: nos[0].desligado, mensagens: nos[0].mensagens,
  ouvinte: nos[0].port.onmessage }, faixas: microfone.faixas.map(f => f.parada) };
const rodadas = ortEstado.rodadas;
await ouvirMicrofone(0.5);
saida.depois_de_desligar = { rodadas: ortEstado.rodadas - rodadas, chamadas: chamadas.length };
"""

INTERROMPER_JS = """
const [a, b] = [emPartes(), emPartes()];
respostas.push(a.resposta, b.resposta);
await ligarEscuta_();
await ouvirMicrofone(1.5);
await digitar("conta uma história");
await a.mandar(linha({ tipo: "transcricao", texto: "conta uma história" }),
               linha({ tipo: "resposta", texto: "Era uma vez. Fim.", acoes: [] }), linha({ tipo: "audio", audio: audioDe(2) }));
efeitos();
await heyJarvis();  // a assistente ainda fala
saida.detectou = { ...situacao(), efeitos: efeitos() };
await a.mandar(linha({ tipo: "audio", audio: audioDe(1) }), linha({ tipo: "fim" }));
await a.fechar();
saida.antigo_terminou = { ...situacao(), efeitos: efeitos() };  // calado: não toca e não apaga o "Ouvindo…"
await ouvirMicrofone(0.2);
await ouvirMicrofone(0.6, 0.2);
await ouvirMicrofone(1.0);
saida.enviou = { ...situacao(), chamadas: chamadas.map(c => [c.url, c.cabecalhos["content-type"]]) };
await b.mandar(linha({ tipo: "transcricao", texto: "para de contar" }),
               linha({ tipo: "resposta", texto: "Tá bom.", acoes: [] }), linha({ tipo: "fim" }));
await b.fechar();
saida.fim = { ...situacao(), efeitos: efeitos() };
"""

SEM_FALA_JS = """
await ligarEscuta_();
await ouvirMicrofone(1.5);
await heyJarvis();
saida.detectou = situacao();
await ouvirMicrofone(3.5);
saida.quase = situacao();
await ouvirMicrofone(0.7);
saida.desistiu = { ...situacao(), chamadas: chamadas.length };
// a pessoa lê o "Diga de novo" e diz logo: a pausa de 2 s depois da detecção já passou durante a gravação
await ouvirMicrofone(1.4);
await heyJarvis();
saida.de_novo = situacao();
"""

BOTAO_JS = """
respostas.push(emJson(200, { transcricao: "que horas são?", resposta: "São 15 horas.", acoes: [] }));
await ligarEscuta_();
await ouvirMicrofone(1.5);
elementos.falar.disparar("pointerdown");  // o botão de falar tem a vez
await passar();
const rodadas = ortEstado.rodadas;
ortEstado.pontuacao = 0.9;
await ouvirMicrofone(1.0);
saida.segurando = { ...situacao(), rodadas: ortEstado.rodadas - rodadas };
await passar(450);
elementos.falar.disparar("pointerup");
await passar();
saida.soltou = { ...situacao(), chamadas: chamadas.map(c => [c.url, c.cabecalhos["content-type"]]) };
// a escuta recomeça do zero: precisa de 16 pedaços (1,28 s) antes de o classificador rodar
await ouvirMicrofone(1.1);
saida.recomecando = situacao();
await ouvirMicrofone(0.4);
saida.voltou = situacao();
"""

ERROS_JS = """
async function caso(nome, preparar, desfazer) {
  preparar();
  const faixas = microfone.faixas.length;
  await ligarEscuta_();
  await passar(20);
  saida[nome] = { ...situacao(), microfone_soltos: microfone.faixas.slice(faixas).map(f => f.parada) };
  desfazer();
  await desligarEscuta_();
}
await caso("sem_gravacao", () => { navigator.mediaDevices = undefined; }, () => { navigator.mediaDevices = midia; });
const pedidosAntes = microfone.pedidos.length;
await caso("sem_worklet", () => { semWorklet = true; }, () => { semWorklet = false; });
saida.sem_worklet.pediu_microfone = microfone.pedidos.length > pedidosAntes;
// Os modelos e o onnxruntime-web ficam guardados depois da primeira vez que carregam: as falhas deles vêm antes.
await caso("modelos", () => { modelosNoServidor = "faltando"; }, () => { modelosNoServidor = "ok"; });
await caso("servidor_fora", () => { modelosNoServidor = "fora"; }, () => { modelosNoServidor = "ok"; });
await caso("erro_http", () => { modelosNoServidor = "erro"; }, () => { modelosNoServidor = "ok"; });
await caso("download_cortado", () => { modelosNoServidor = "cortado"; }, () => { modelosNoServidor = "ok"; });
await caso("onnx", () => { ortEstado.carga = "erro"; }, () => { ortEstado.carga = "ok"; });
saida.onnx.script = scripts[scripts.length - 1];
await caso("sem_wasm", () => {
  ortEstado.falhaAoCriar = "no available backend found. ERR: [wasm] RuntimeError: Aborted(both async and sync fetching of the wasm failed).";
}, () => { ortEstado.falhaAoCriar = null; });
await caso("modelo_estragado", () => {
  ortEstado.falhaAoCriar = "Can't create a session. ERROR_CODE: 7, ERROR_MESSAGE: Failed to load model because protobuf parsing failed.";
}, () => { ortEstado.falhaAoCriar = null; });
await caso("microfone_negado", () => { microfone.falha = "NotAllowedError"; }, () => { microfone.falha = null; });
await caso("sem_microfone", () => { microfone.falha = "NotFoundError"; }, () => { microfone.falha = null; });
await caso("microfone_ocupado", () => { microfone.falha = "NotReadableError"; }, () => { microfone.falha = null; });
// Com a escuta funcionando: o microfone é desligado, e o classificador falha
await ligarEscuta_();
saida.funcionando = situacao();
microfone.faixas[microfone.faixas.length - 1].onended();
saida.microfone_parou = { ...situacao(), solto: microfone.faixas[microfone.faixas.length - 1].parada, no_desligado: nos[nos.length - 1].desligado };
await ligarEscuta_();
await ouvirMicrofone(1.0);
ortEstado.falhaAoRodar = "falha no classificador";
await ouvirMicrofone(0.5);
saida.classificador_falhou = situacao();
// um erro longo em inglês, como os do onnxruntime: fica curto e marcado como detalhe técnico
await ligarEscuta_();
await ouvirMicrofone(1.0);
ortEstado.falhaAoRodar = "failed to call OrtRun(). ERROR_CODE: 6, ERROR_MESSAGE: Non-zero status code returned while running Conv node. " + "x".repeat(300);
await ouvirMicrofone(0.5);
saida.erro_longo = situacao();
ortEstado.falhaAoRodar = null;
await ligarEscuta_();
saida.depois_de_tudo = situacao();
saida.scripts_carregados = scripts.length;
"""

LIGA_E_DESLIGA_JS = """
// Desligar enquanto carrega: nada fica ligado e o microfone é solto assim que chega.
elementos.ouvir.checked = true;
elementos.ouvir.disparar("change");
elementos.ouvir.checked = false;
elementos.ouvir.disparar("change");
await passar(30);
saida.desligou_carregando = { ...situacao(), faixas: microfone.faixas.map(f => f.parada), nos: nos.length };
await ligarEscuta_();
saida.ligou_de_novo = { ...situacao(), nos: nos.length, carregou_o_ort: scripts.length };
"""

RECARREGAR_JS = """
await passar(30);
saida.recarregou = { ...situacao(), microfone: microfone.pedidos.length };
"""

SEM_GESTO_JS = """
await passar(400);
saida.sem_gesto = { ...situacao(), contexto: contexto.state };
ContextoFalso.libera = true;
documento.disparar("pointerdown");  // um toque em qualquer lugar da página
await passar();
saida.tocou = { ...situacao(), contexto: contexto.state };
"""

SEM_GESTO_TOQUE_JS = """
await passar(400);
saida.sem_gesto = situacao();
ContextoFalso.libera = true;
documento.disparar("pointerup", { pointerType: "touch" });  // na tela de toque, o gesto é o pointerup
await passar();
saida.tocou = { ...situacao(), contexto: contexto.state };
"""

SOM_PAUSADO_JS = """
await ligarEscuta_();
saida.ligou = situacao();
ContextoFalso.libera = false;
contexto.state = "suspended";  // o navegador pausou o som no meio
contexto.onstatechange();
saida.pausou = { ...situacao(), vermelho: estado().vermelho };
ContextoFalso.libera = true;
documento.disparar("pointerdown");
await passar();
saida.voltou = { ...situacao(), contexto: contexto.state };
// o navegador pausa e retoma sozinho (a ligação acabou)
contexto.state = "suspended";
contexto.onstatechange();
saida.pausou_de_novo = situacao().status;
contexto.state = "running";
contexto.onstatechange();
saida.retomou_sozinho = situacao().status;
// durante uma gravação, o aviso não apaga o "Ouvindo…"
await ouvirMicrofone(1.5);
await heyJarvis();
contexto.state = "suspended";
contexto.onstatechange();
saida.gravando = situacao();
"""

BLOQUEADO_JS = """
saida.carregou = situacao();
await ligarEscuta_();
saida.ligou = { ...situacao(), guardado: null };
await desligarEscuta_();
saida.desligou = { ...situacao(), guardado: null };
"""

RESTAURADA_JS = """
await passar(30);
saida.carregou = { ...situacao(), microfone: microfone.pedidos.length };
"""


@PRECISA_NODE_18
class TestPaginaComJarvis(unittest.TestCase):
    """A página inteira no navegador falso, com a escuta do "hey Jarvis" ligada."""

    @classmethod
    def setUpClass(cls):
        cls.m = mensagens()

    def test_liga_detecta_grava_envia_e_volta_a_esperar(self):
        r = rodar_com_escuta(FLUXO_JS)
        self.assertEqual(r["ao_carregar"], {"status": "", "vermelho": False, "pensando": False, "historico": [],
                                            "ouvindo": False, "marcado": False, "guardado": None, "escuta": None,
                                            "microfone": 0, "scripts": 0})
        ligou = r["ligou"]
        self.assertEqual((ligou["status"], ligou["marcado"], ligou["guardado"], ligou["escuta"]), (DIGA, True, "1", "esperando"))
        self.assertEqual(ligou["efeitos"], [["contexto", "running"]])  # o contexto nasce no clique (um gesto)
        self.assertEqual(ligou["restricoes"], [{"audio": {"echoCancellation": True, "noiseSuppression": True,
                                                          "autoGainControl": True, "channelCount": 1}}])
        self.assertEqual(ligou["scripts"], [{
            "src": "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.30.0/dist/ort.wasm.min.js",
            "integrity": "sha384-Bw6URI+Gvoadw0do7/QrSHUC9D1vCYYK5OSqChFyhyV5mzPL4oe2rOAFljAnS8Sq", "crossOrigin": "anonymous"}])
        self.assertEqual(ligou["wasm"], {"wasmPaths": "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.30.0/dist/", "numThreads": 1})
        self.assertEqual(ligou["criados"], [[nome, {"executionProviders": ["wasm"]}] for nome in
                                            ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")])
        self.assertEqual(ligou["no"]["nome"], "captura-jarvis")
        self.assertTrue(ligou["no"]["saida_na_destination"])
        self.assertEqual(r["modulos"], [{"blob": True, "tipo": "text/javascript", "codigo": True}])

        self.assertEqual(r["esperando"]["status"], DIGA)
        self.assertGreater(r["esperando"]["rodadas"], 5)  # o classificador roda enquanto espera
        self.assertEqual(r["esperando"]["chamadas"], 0)  # nada sai do aparelho antes da palavra

        detectou = r["detectou"]
        self.assertEqual((detectou["status"], detectou["ouvindo"], detectou["escuta"]), ("Ouvindo…", True, "gravando"))
        self.assertEqual(detectou["efeitos"], [["cancelar"], ["volume do bipe", 0.08], ["bipe", 880]])
        self.assertEqual(r["falando"]["escuta"], "gravando")

        enviou = r["enviou"]
        self.assertEqual(enviou["metodo"], "POST")
        self.assertEqual(enviou["cabecalhos"], {"accept": "application/x-ndjson", "content-type": "audio/wav",
                                                "x-conversa": enviou["conversa"]})
        self.assertEqual(enviou["tipo_do_corpo"], "audio/wav")
        self.assertEqual((enviou["status"], enviou["pensando"], enviou["ouvindo"], enviou["escuta"]),
                         ("Pensando…", True, False, "esperando"))  # já espera a palavra de novo
        info, amostras = ler_wav(base64.b64decode(enviou["wav"]))
        self.assertEqual((info["taxa"], info["canais"], info["bytes_por_amostra"]), (16000, 1, 2))
        segundos = info["amostras"] / 16000
        self.assertGreater(segundos, 2.0)  # ~0,05 s depois da detecção + 0,3 s + 1 s de fala + 0,8 s de silêncio
        self.assertLess(segundos, 2.3)
        self.assertEqual(info["amostras"] % 1280, 0)
        fala = amostras[int(0.6 * 16000):int(1.2 * 16000)]
        self.assertAlmostEqual(rms(fala), 0.2 / math.sqrt(2) * 32768, delta=150)  # o tom de 440 Hz chega inteiro
        self.assertLess(rms(amostras[-int(0.7 * 16000):]), 20)  # o fim é o silêncio que encerrou a gravação

        self.assertEqual(r["respondeu"]["status"], DIGA)
        self.assertEqual(r["respondeu"]["historico"], [[["voce", "abre a calculadora"], ["ela", "Pronto, abri calculadora."]]])
        self.assertEqual(r["respondeu"]["efeitos"], [["cancelar"], ["cancelar"], ["navegador", "Pronto, abri calculadora."]])
        self.assertEqual((r["na_pausa"]["escuta"], r["na_pausa"]["status"]), ("esperando", DIGA))
        self.assertEqual((r["depois_da_pausa"]["escuta"], r["depois_da_pausa"]["status"]), ("gravando", "Ouvindo…"))
        self.assertEqual(r["depois_da_pausa"]["efeitos"], [["cancelar"], ["volume do bipe", 0.08], ["bipe", 880]])

        desligou = r["desligou"]
        self.assertEqual((desligou["status"], desligou["marcado"], desligou["guardado"], desligou["escuta"], desligou["ouvindo"]),
                         (PRONTO, False, "0", None, False))
        self.assertEqual(desligou["efeitos"], [["fonte desligada"], ["microfone solto"]])
        self.assertEqual(desligou["no"], {"desligado": True, "mensagens": ["parar"], "ouvinte": None})
        self.assertEqual(desligou["faixas"], [True])
        self.assertEqual(r["depois_de_desligar"], {"rodadas": 0, "chamadas": 1})

    def test_hey_jarvis_interrompe_a_assistente(self):
        r = rodar_com_escuta(INTERROMPER_JS)
        # a frase agendada para, a voz do navegador também, e vem o bipe
        self.assertEqual(r["detectou"]["efeitos"], [["parar"], ["cancelar"], ["volume do bipe", 0.08], ["bipe", 880]])
        self.assertEqual(r["detectou"]["status"], "Ouvindo…")
        # o pedido calado termina sem tocar, sem falar e sem apagar o "Ouvindo…"
        antigo = r["antigo_terminou"]
        self.assertEqual((antigo["efeitos"], antigo["status"], antigo["ouvindo"]), ([], "Ouvindo…", True))
        self.assertEqual(antigo["historico"][0], [["voce", "conta uma história"], ["ela", "Era uma vez. Fim."]])
        self.assertEqual(r["enviou"]["chamadas"], [["/texto", "application/json"], ["/voz", "audio/wav"]])
        self.assertEqual(r["fim"]["status"], DIGA)
        self.assertEqual(r["fim"]["efeitos"], [["cancelar"], ["cancelar"], ["navegador", "Tá bom."]])

    def test_sem_fala_desiste_e_volta_a_esperar(self):
        r = rodar_com_escuta(SEM_FALA_JS)
        self.assertEqual(r["detectou"]["status"], "Ouvindo…")
        self.assertEqual(r["quase"]["status"], "Ouvindo…")  # ainda dentro dos ~4 s
        desistiu = r["desistiu"]
        self.assertEqual((desistiu["status"], desistiu["vermelho"], desistiu["ouvindo"], desistiu["escuta"], desistiu["chamadas"]),
                         (SEM_FALA, False, False, "esperando", 0))
        self.assertEqual((r["de_novo"]["status"], r["de_novo"]["escuta"]), ("Ouvindo…", "gravando"))

    def test_botao_de_falar_continua_funcionando_e_pausa_a_escuta(self):
        r = rodar_com_escuta(BOTAO_JS)
        self.assertEqual((r["segurando"]["status"], r["segurando"]["escuta"], r["segurando"]["rodadas"]), ("Ouvindo…", "esperando", 0))
        self.assertEqual(r["soltou"]["chamadas"], [["/voz", "audio/webm;codecs=opus"]])
        self.assertEqual(r["soltou"]["status"], DIGA)  # a resposta do botão também volta para o "Diga"
        self.assertEqual(r["recomecando"]["escuta"], "esperando")
        self.assertEqual((r["voltou"]["escuta"], r["voltou"]["status"]), ("gravando", "Ouvindo…"))

    def test_erros_em_portugues_e_microfone_solto(self):
        r = rodar_com_escuta(ERROS_JS)
        m = self.m
        esperados = {
            "sem_gravacao": m["sem_gravacao"], "sem_worklet": m["sem_worklet"], "microfone_negado": m["microfone_negado"],
            "sem_microfone": m["sem_microfone"], "microfone_ocupado": m["microfone_ocupado"], "modelos": m["modelos"],
            "servidor_fora": m["servidor_fora"], "onnx": m["onnx"], "sem_wasm": m["onnx"],
            "modelo_estragado": m["modelos_com_defeito"],
            "erro_http": "O servidor respondeu com erro 500 ao mandar os modelos do “hey Jarvis”. Tente de novo.",
            "download_cortado": m["servidor_fora"],
        }
        for caso, mensagem in esperados.items():
            with self.subTest(caso=caso):
                self.assertEqual(r[caso]["status"], mensagem)
                self.assertTrue(r[caso]["vermelho"])
                self.assertEqual((r[caso]["marcado"], r[caso]["guardado"], r[caso]["escuta"]), (False, "0", None))
                self.assertTrue(all(r[caso]["microfone_soltos"]), "o microfone ficou preso")
        self.assertEqual(m["modelos"], "Os modelos do “hey Jarvis” não estão no servidor: na pasta celular_servidor, "
                                       "rode python baixar_ativacao.py")
        self.assertFalse(r["sem_worklet"]["pediu_microfone"])
        self.assertEqual(r["sem_gravacao"]["microfone_soltos"], [])
        self.assertEqual(len(r["modelos"]["microfone_soltos"]), 1)  # pediu o microfone e soltou
        self.assertEqual(r["onnx"]["script"]["src"], "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.30.0/dist/ort.wasm.min.js")
        self.assertEqual(r["funcionando"]["status"], DIGA)
        self.assertEqual((r["microfone_parou"]["status"], r["microfone_parou"]["vermelho"], r["microfone_parou"]["marcado"]),
                         (m["microfone_parou"], True, False))
        self.assertTrue(r["microfone_parou"]["no_desligado"])
        self.assertEqual(r["classificador_falhou"]["status"],
                         "A escuta do “hey Jarvis” parou por um erro. Ligue de novo (detalhe técnico: falha no classificador).")
        self.assertEqual((r["classificador_falhou"]["marcado"], r["classificador_falhou"]["escuta"]), (False, None))
        longo = r["erro_longo"]["status"]
        self.assertTrue(longo.startswith("A escuta do “hey Jarvis” parou por um erro. Ligue de novo (detalhe técnico: "
                                         "failed to call OrtRun(). ERROR_CODE: 6"), longo)
        self.assertTrue(longo.endswith("...)."), longo)
        self.assertLess(len(longo), 200)
        self.assertTrue(r["erro_longo"]["vermelho"])
        self.assertEqual(r["depois_de_tudo"]["status"], DIGA)  # depois dos erros, liga normalmente
        self.assertEqual(r["scripts_carregados"], 2)  # o ort da CDN carrega uma vez só (e uma tentativa que falhou)

    def test_desligar_enquanto_carrega(self):
        r = rodar_com_escuta(LIGA_E_DESLIGA_JS)
        d = r["desligou_carregando"]
        self.assertEqual((d["status"], d["marcado"], d["escuta"], d["guardado"], d["faixas"], d["nos"]),
                         (PRONTO, False, None, "0", [True], 0))
        self.assertEqual((r["ligou_de_novo"]["status"], r["ligou_de_novo"]["nos"]), (DIGA, 1))

    def test_escolha_guardada_liga_a_escuta_ao_recarregar(self):
        r = rodar_com_escuta(RECARREGAR_JS, antes='armazenamento.set("ouvirJarvis", "1");')
        self.assertEqual((r["recarregou"]["status"], r["recarregou"]["marcado"], r["recarregou"]["escuta"],
                          r["recarregou"]["microfone"]), (DIGA, True, "esperando", 1))

    def test_ao_recarregar_sem_gesto_explica_e_o_primeiro_toque_libera(self):
        r = rodar_com_escuta(SEM_GESTO_JS, antes='armazenamento.set("ouvirJarvis", "1");'
                                                 'ContextoFalso.estado = "suspended"; ContextoFalso.libera = false;')
        self.assertEqual((r["sem_gesto"]["status"], r["sem_gesto"]["vermelho"], r["sem_gesto"]["contexto"],
                          r["sem_gesto"]["escuta"]), (GESTO, False, "suspended", "esperando"))
        self.assertEqual((r["tocou"]["status"], r["tocou"]["contexto"]), (DIGA, "running"))

    def test_na_tela_de_toque_o_pointerup_libera_o_som(self):
        r = rodar_com_escuta(SEM_GESTO_TOQUE_JS, antes='armazenamento.set("ouvirJarvis", "1");'
                                                       'ContextoFalso.estado = "suspended"; ContextoFalso.libera = false;')
        self.assertEqual(r["sem_gesto"]["status"], GESTO)
        self.assertEqual((r["tocou"]["status"], r["tocou"]["contexto"]), (DIGA, "running"))

    def test_som_pausado_no_meio_avisa_e_o_toque_retoma(self):
        r = rodar_com_escuta(SOM_PAUSADO_JS)
        self.assertEqual(r["ligou"]["status"], DIGA)
        self.assertEqual((r["pausou"]["status"], r["pausou"]["vermelho"], r["pausou"]["escuta"]),
                         ("O navegador pausou o som, e a escuta do “hey Jarvis” parou: toque na página para voltar.",
                          False, "esperando"))
        self.assertEqual((r["voltou"]["status"], r["voltou"]["contexto"]), (DIGA, "running"))
        self.assertEqual((r["pausou_de_novo"], r["retomou_sozinho"]), (r["pausou"]["status"], DIGA))
        self.assertEqual((r["gravando"]["status"], r["gravando"]["escuta"]), ("Ouvindo…", "gravando"))

    def test_sem_localstorage_a_escuta_funciona_igual(self):
        r = rodar_com_escuta(BLOQUEADO_JS, antes="armazenamentoBloqueado = true;")
        self.assertEqual(r["carregou"]["status"], "")  # o navegador falso não lê o HTML: nada mudou
        self.assertEqual((r["ligou"]["status"], r["ligou"]["marcado"]), (DIGA, True))
        self.assertEqual((r["desligou"]["status"], r["desligou"]["marcado"]), (PRONTO, False))

    def test_caixa_restaurada_pelo_navegador_nao_liga_sozinha(self):
        r = rodar_com_escuta(RESTAURADA_JS, antes="elementos.ouvir.checked = true;")
        self.assertEqual((r["carregou"]["marcado"], r["carregou"]["microfone"], r["carregou"]["status"]), (False, 0, ""))
        r = rodar_com_escuta(RESTAURADA_JS, antes='armazenamento.set("ouvirJarvis", "0");')
        self.assertEqual((r["carregou"]["marcado"], r["carregou"]["microfone"]), (False, 0))


class TestPaginaTemOControle(unittest.TestCase):
    def test_controle_abaixo_do_botao_e_script_antes_da_pagina(self):
        html = INDEX.read_text(encoding="utf-8")
        botao, controle, formulario = (html.index('id="falar"'), html.index('id="ouvir"'), html.index('id="formTexto"'))
        self.assertLess(botao, controle)
        self.assertLess(controle, formulario)
        self.assertIn('<input id="ouvir" type="checkbox" role="switch" autocomplete="off"> Ouvir “hey Jarvis”</label>', html)
        self.assertLess(html.index('<script src="/static/ativacao/jarvis.js"></script>'), html.index("<script>\n"))


# ---------------------------------------------------------------------------------------------------
# baixar_ativacao.py
# ---------------------------------------------------------------------------------------------------

class GitHubFalso:
    """Serve /v0.5.1/<arquivo> com o conteúdo de `arquivos` (404 para o resto) e guarda os caminhos pedidos."""

    def __init__(self):
        self.arquivos: dict[str, bytes] = {}
        self.pedidos: list[str] = []
        falso = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                falso.pedidos.append(self.path)
                nome = self.path.rsplit("/", 1)[-1]
                corpo = falso.arquivos.get(nome) if self.path.startswith("/v0.5.1/") else None
                self.send_response(200 if corpo is not None else 404)
                corpo = corpo if corpo is not None else b"Not Found"
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, *args):
                pass

        self._servidor = ServidorLocal(Handler)
        self.origem = self._servidor.url + "/v0.5.1/"

    def parar(self):
        self._servidor.parar()


def porta_fechada() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestBaixarAtivacao(unittest.TestCase):
    """O download dos modelos, contra um servidor falso local: nada vai para a internet."""

    CONTEUDO = {"melspectrogram.onnx": b"mel" * 1000, "embedding_model.onnx": b"emb" * 1200,
                "hey_jarvis_v0.1.onnx": b"jarvis" * 500}

    def setUp(self):
        self.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        self.sem_proxy.start()
        self.addCleanup(self.sem_proxy.stop)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.pasta = Path(self.tmp.name) / "celular_servidor"
        self.pasta.mkdir()
        shutil.copy2(PASTA_SERVIDOR / "baixar_ativacao.py", self.pasta)
        self.modulo = importar_copia(self.pasta, "baixar_ativacao")
        self.modelos = self.pasta / "static" / "ativacao" / "modelos"
        self.github = GitHubFalso()
        self.addCleanup(self.github.parar)
        self.github.arquivos = dict(self.CONTEUDO)
        falsos = {nome: (len(c), hashlib.sha256(c).hexdigest()) for nome, c in self.CONTEUDO.items()}
        patcher = mock.patch.object(self.modulo, "MODELOS", falsos)
        patcher.start()
        self.addCleanup(patcher.stop)

    def baixar(self, *argumentos):
        saida, erros = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erros):
            codigo = self.modulo.main(list(argumentos or ["--origem", self.github.origem]))
        return codigo, saida.getvalue(), erros.getvalue()

    def arquivos(self) -> dict:
        return {p.name: p.read_bytes() for p in self.modelos.iterdir()} if self.modelos.exists() else {}

    def test_baixa_os_tres_para_a_pasta_do_script_e_confere(self):
        antes = Path.cwd()
        outra = Path(self.tmp.name) / "outra pasta"
        outra.mkdir()
        os.chdir(outra)  # roda de qualquer pasta: o destino é relativo ao próprio script
        try:
            codigo, saida, erros = self.baixar()
        finally:
            os.chdir(antes)
        self.assertEqual((codigo, erros), (0, ""))
        self.assertEqual(self.arquivos(), self.CONTEUDO)
        self.assertEqual(list(outra.iterdir()), [])
        self.assertEqual(sorted(self.github.pedidos), sorted(f"/v0.5.1/{n}" for n in self.CONTEUDO))
        self.assertIn("melspectrogram.onnx: baixando 0,0 MB...", saida)
        self.assertIn(f'Pronto: os modelos do "hey Jarvis" estão em {self.modelos}.', saida)
        self.assertIn("CC BY-NC-SA 4.0", saida)

    def test_nao_baixa_de_novo_o_que_ja_esta_certo_e_troca_o_que_esta_errado(self):
        self.baixar()
        self.github.pedidos.clear()
        (self.modelos / "embedding_model.onnx").write_bytes(b"estragado")
        codigo, saida, _ = self.baixar()
        self.assertEqual(codigo, 0)
        self.assertEqual(self.github.pedidos, ["/v0.5.1/embedding_model.onnx"])
        self.assertIn("melspectrogram.onnx: já está baixado.", saida)
        self.assertIn("embedding_model.onnx: baixando", saida)
        self.assertEqual(self.arquivos(), self.CONTEUDO)

    def test_recusa_arquivo_com_hash_errado_e_nao_deixa_nada_pela_metade(self):
        self.github.arquivos["embedding_model.onnx"] = b"outro arquivo"
        codigo, saida, erros = self.baixar()
        self.assertEqual(codigo, 1)
        self.assertIn(f"O embedding_model.onnx baixado de {self.github.origem}embedding_model.onnx não é o arquivo esperado", erros)
        self.assertIn(hashlib.sha256(b"outro arquivo").hexdigest(), erros)
        self.assertIn("Ele foi apagado.", erros)
        self.assertEqual(self.arquivos(), {"melspectrogram.onnx": self.CONTEUDO["melspectrogram.onnx"]})  # sem .parcial
        self.assertNotIn("Pronto", saida)

    def test_erro_http(self):
        del self.github.arquivos["hey_jarvis_v0.1.onnx"]
        codigo, _, erros = self.baixar()
        self.assertEqual(codigo, 1)
        self.assertIn(f"O servidor respondeu com erro 404 ao baixar {self.github.origem}hey_jarvis_v0.1.onnx", erros)
        self.assertNotIn("hey_jarvis_v0.1.onnx", self.arquivos())

    def test_sem_internet(self):
        origem = f"http://127.0.0.1:{porta_fechada()}/v0.5.1/"
        codigo, _, erros = self.baixar("--origem", origem)
        self.assertEqual(codigo, 1)
        self.assertTrue(erros.startswith(f"Sem conexão para baixar {origem}melspectrogram.onnx: confira a internet e rode de novo"), erros)
        self.assertEqual(self.arquivos(), {})

    def test_endereco_sem_http(self):
        codigo, _, erros = self.baixar("--origem", "espelho.local/modelos")
        self.assertEqual(codigo, 1)
        self.assertIn("O endereço espelho.local/modelos/melspectrogram.onnx não é válido: use um que comece com https://", erros)
        self.assertNotIn("Traceback", erros)
        self.assertEqual(self.arquivos(), {})

    def test_argumento_desconhecido(self):
        codigo, _, erros = self.baixar("--ajuda")
        self.assertEqual(codigo, 2)
        self.assertIn("Uso: python baixar_ativacao.py [--origem URL]", erros)

    def test_como_o_usuario_roda_de_outra_pasta(self):
        # sem o MODELOS falso (outro processo): o servidor falso manda arquivos que não conferem com o SHA-256 real
        outra = Path(self.tmp.name) / "outra"
        outra.mkdir()
        r = subprocess.run([sys.executable, str(self.pasta / "baixar_ativacao.py"), "--origem", self.github.origem],
                           cwd=outra, capture_output=True, text=True, encoding="utf-8", timeout=60,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("melspectrogram.onnx: baixando 1,1 MB...", r.stdout)
        self.assertIn("não é o arquivo esperado", r.stderr)
        self.assertIn("ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f", r.stderr)
        self.assertTrue(self.modelos.is_dir())  # a pasta nasce ao lado do script, não em `outra`
        self.assertEqual(list(self.modelos.iterdir()), [])
        self.assertEqual(list(outra.iterdir()), [])

    def test_modelos_oficiais_e_os_mesmos_da_pagina(self):
        oficial = importar_copia(self.pasta, "baixar_ativacao")
        self.assertEqual(oficial.ORIGEM, "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/")
        self.assertEqual({n: t for n, (t, _) in oficial.MODELOS.items()},
                         {"melspectrogram.onnx": 1087958, "embedding_model.onnx": 1326578, "hey_jarvis_v0.1.onnx": 1271370})
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for _, h in oficial.MODELOS.values()))
        self.assertEqual(oficial.PASTA, self.modelos)
        pagina = re.search(r"const MODELOS = (\{.*?\});", JARVIS.read_text(encoding="utf-8")).group(1)
        self.assertEqual(sorted(re.findall(r'"([\w.]+\.onnx)"', pagina)), sorted(oficial.MODELOS))

    def test_gitignore_deixa_os_modelos_fora(self):
        linhas = (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("celular_servidor/static/ativacao/modelos/*.onnx", linhas)


if __name__ == "__main__":
    unittest.main()
