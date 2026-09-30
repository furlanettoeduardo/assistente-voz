/*
 * Palavra de ativação "hey Jarvis" no navegador: simula o que o satélite ESP32 vai fazer.
 *
 * Com a escuta ligada, a página ouve o microfone o tempo todo e reconhece "hey Jarvis" aqui mesmo, com o
 * modelo pronto do openWakeWord rodando no onnxruntime-web (nada sai do aparelho antes disso). Depois grava
 * o comando até a pessoa parar de falar e entrega um WAV 16 kHz mono de 16 bits, o formato do ESP32.
 *
 * O caminho do áudio: microfone → AudioWorklet (só copia as amostras) → reamostragem para 16 kHz com
 * passa-baixa → pedaços de 1280 amostras (80 ms) → melspectrogram → embedding → classificador. O ONNX
 * roda fora do AudioWorklet, na página, um pedaço de cada vez.
 *
 * O pipeline repete o do openWakeWord em Python (openwakeword/utils.py, AudioFeatures, e model.py) e dá as
 * mesmas pontuações para o mesmo áudio. Os modelos são do openWakeWord (código Apache-2.0, modelos
 * pré-treinados CC BY-NC-SA 4.0: uso não comercial, com atribuição).
 *
 * Também roda no Node (module.exports), para os testes.
 */
(function (raiz) {
  "use strict";

  const TAXA = 16000;
  const PEDACO = 1280;        // 80 ms: o openWakeWord só calcula quando junta 1280 amostras novas
  const CONTEXTO = 480;       // o melspectrogram recebe junto as 480 amostras anteriores (160 * 3)
  const QUADROS = 76;         // janela do embedding: 76 quadros mel (a cada pedaço entram 8)
  const BANDAS = 32;          // bandas mel por quadro
  const EMBEDDINGS = 16;      // o classificador do hey_jarvis recebe os 16 embeddings mais recentes
  const DIMENSAO = 96;        // tamanho de cada embedding
  const LIMIAR = 0.5;         // o limiar padrão do openWakeWord
  const PAUSA = 25;           // pedaços (2 s) sem detectar depois de uma detecção

  const BASE = "/static/ativacao/";
  const MODELOS = { mel: "melspectrogram.onnx", embedding: "embedding_model.onnx", classificador: "hey_jarvis_v0.1.onnx" };
  const VERSAO_ORT = "1.30.0";
  const ORT_DIST = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${VERSAO_ORT}/dist/`;
  const ORT_URL = ORT_DIST + "ort.wasm.min.js";  // só o backend WebAssembly: o menor pacote (o .wasm tem 3 MB comprimido)
  const ORT_INTEGRIDADE = "sha384-Bw6URI+Gvoadw0do7/QrSHUC9D1vCYYK5OSqChFyhyV5mzPL4oe2rOAFljAnS8Sq";

  const NOME = "“hey Jarvis”";
  const MENSAGENS = {
    sem_gravacao: "Este navegador não grava áudio aqui. Abra pelo endereço localhost.",
    sem_worklet: `Este navegador não tem o AudioWorklet, que a escuta do ${NOME} usa. Use o Chrome, o Edge ou o Firefox atualizados.`,
    microfone_negado: "Sem acesso ao microfone. Permita o uso nas configurações do navegador.",
    sem_microfone: "Nenhum microfone encontrado. Ligue um microfone e tente de novo.",
    microfone_ocupado: "O microfone não respondeu (pode estar em uso por outro programa). Feche o outro programa e tente de novo.",
    microfone_parou: `O microfone parou de mandar áudio (foi desligado ou desconectado). Ligue a escuta do ${NOME} de novo.`,
    modelos: `Os modelos do ${NOME} não estão no servidor: na pasta celular_servidor, rode python baixar_ativacao.py`,
    modelos_com_defeito: `Os modelos do ${NOME} no servidor estão com defeito: na pasta celular_servidor, rode python baixar_ativacao.py de novo.`,
    servidor_fora: "Sem conexão com o servidor do celular. Confira se ele ainda está rodando no Termux.",
    onnx: `Não consegui carregar o onnxruntime-web, que reconhece o ${NOME}: ele vem da internet (cdn.jsdelivr.net). Confira a conexão e ligue a escuta de novo.`,
  };
  const erroHttp = status => `O servidor respondeu com erro ${status} ao mandar os modelos do ${NOME}. Tente de novo.`;
  // O detalhe do navegador (ou do onnxruntime) costuma vir em inglês e pode ser enorme: vai marcado como
  // detalhe técnico e curto na tela; inteiro, no console.
  function erroInesperado(e) {
    if (raiz.console) console.error("[hey Jarvis]", e);
    let detalhe = String((e && (e.message || e.name)) || e).replace(/\s+/g, " ").trim();
    if (detalhe.length > 120) detalhe = detalhe.slice(0, 117) + "...";
    return `A escuta do ${NOME} parou por um erro. Ligue de novo (detalhe técnico: ${detalhe}).`;
  }

  class ErroDaEscuta extends Error {
    constructor(codigo, mensagem = MENSAGENS[codigo]) {
      super(mensagem);
      this.codigo = codigo;
    }
  }

  // ---------- Áudio: reamostragem, PCM e WAV ----------

  const mdc = (a, b) => (b ? mdc(b, a % b) : a);

  /*
   * Reamostragem de qualquer taxa (44,1 ou 48 kHz, as do AudioContext) para 16 kHz, em fluxo. Um filtro
   * passa-baixa (sinc com janela de Blackman) corta o que passa de 8 kHz antes de descartar amostras, para
   * não virar ruído dobrado no espectro. Com a razão L/M (160/441 para 44,1 kHz, 1/3 para 48 kHz), cada
   * amostra de saída cai numa de L fases, e os coeficientes de cada fase ficam calculados de antemão.
   */
  class Reamostrador {
    constructor(entrada, saida = TAXA, { zeros = 20, corte = 0.92 } = {}) {
      if (!(entrada > 0) || !(saida > 0) || entrada % 1 || saida % 1) {
        throw new RangeError(`taxa de amostragem inválida: ${entrada} → ${saida}`);
      }
      this.entrada = entrada;
      this.saida = saida;
      this.direto = entrada === saida;
      if (this.direto) return;
      const g = mdc(entrada, saida);
      this.L = saida / g;
      this.M = entrada / g;
      const razao = Math.min(1, saida / entrada);
      const fc = 0.5 * razao * corte;  // corte em ciclos por amostra de entrada (um pouco abaixo de 8 kHz)
      this.meia = Math.ceil(zeros / razao);  // meia largura do filtro, em amostras de entrada
      const taps = 2 * this.meia;
      this.fases = [];
      for (let p = 0; p < this.L; p++) {
        const h = new Float64Array(taps);
        let soma = 0;
        for (let k = 0; k < taps; k++) {
          const d = p / this.L - (k - this.meia + 1);  // distância entre a saída e a amostra de entrada
          const x = 2 * fc * d;
          const sinc = x === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
          const u = d / this.meia;
          const janela = Math.abs(u) >= 1 ? 0 : 0.42 + 0.5 * Math.cos(Math.PI * u) + 0.08 * Math.cos(2 * Math.PI * u);
          h[k] = sinc * janela;
          soma += h[k];
        }
        this.fases.push(Float32Array.from(h, v => v / soma));  // ganho 1 em cada fase
      }
      this.reiniciar();
    }

    reiniciar() {
      if (this.direto) return;
      // O começo é precedido de zeros: a saída 0 corresponde à entrada 0.
      this.buffer = new Float32Array(this.meia - 1);
      this.inicio = -(this.meia - 1);  // índice absoluto de buffer[0]
      this.base = 0;                   // amostra de entrada logo antes (ou em cima) da próxima saída
      this.fase = 0;                   // posição da próxima saída entre base e base + 1, em L avos
      this.lidas = 0;
      this.geradas = 0;
    }

    /** Recebe amostras (Float32Array) na taxa de entrada e devolve as que já dá para calcular na saída. */
    processar(amostras) {
      if (this.direto) return Float32Array.from(amostras);
      const junto = new Float32Array(this.buffer.length + amostras.length);
      junto.set(this.buffer);
      junto.set(amostras, this.buffer.length);
      this.lidas += amostras.length;
      const fim = this.inicio + junto.length;  // uma depois da última amostra disponível
      const saida = new Float32Array(Math.max(0, Math.ceil((fim - this.meia - this.base) * this.L / this.M) + 1));
      let n = 0;
      while (this.base + this.meia < fim) {
        const h = this.fases[this.fase];
        const primeira = this.base - this.meia + 1 - this.inicio;
        let soma = 0;
        for (let k = 0; k < h.length; k++) soma += junto[primeira + k] * h[k];
        saida[n++] = soma;
        this.fase += this.M;
        this.base += Math.floor(this.fase / this.L);
        this.fase %= this.L;
      }
      this.geradas += n;
      const guardar = this.base - this.meia + 1;  // a primeira amostra que as próximas saídas ainda usam
      this.buffer = junto.slice(Math.max(0, guardar - this.inicio));
      this.inicio = Math.max(this.inicio, guardar);
      return saida.subarray(0, n);
    }

    /** Fim do áudio: completa com zeros, devolve o que faltava (ceil(lidas * L / M) no total) e recomeça. */
    terminar() {
      if (this.direto) return new Float32Array(0);
      const total = Math.ceil(this.lidas * this.L / this.M);
      const antes = this.geradas;
      const resto = this.processar(new Float32Array(this.meia + 1));
      this.reiniciar();
      return resto.slice(0, Math.max(0, total - antes));
    }
  }

  /** Float32 (-1 a 1, como a Web Audio entrega) para PCM de 16 bits. */
  function paraInt16(amostras) {
    const saida = new Int16Array(amostras.length);
    for (let i = 0; i < amostras.length; i++) {
      saida[i] = Math.max(-32768, Math.min(32767, Math.round(amostras[i] * 32768)));
    }
    return saida;
  }

  function juntar(partes, Tipo = Int16Array) {
    const saida = new Tipo(partes.reduce((n, p) => n + p.length, 0));
    let i = 0;
    for (const p of partes) { saida.set(p, i); i += p.length; }
    return saida;
  }

  /** WAV PCM mono de 16 bits (cabeçalho de 44 bytes), como o ESP32 manda. */
  function criarWav(amostras, taxa = TAXA) {
    const bytes = new ArrayBuffer(44 + amostras.length * 2);
    const v = new DataView(bytes);
    const texto = (pos, s) => { for (let i = 0; i < s.length; i++) v.setUint8(pos + i, s.charCodeAt(i)); };
    texto(0, "RIFF");
    v.setUint32(4, 36 + amostras.length * 2, true);
    texto(8, "WAVE");
    texto(12, "fmt ");
    v.setUint32(16, 16, true);         // tamanho do bloco fmt
    v.setUint16(20, 1, true);          // PCM
    v.setUint16(22, 1, true);          // mono
    v.setUint32(24, taxa, true);
    v.setUint32(28, taxa * 2, true);   // bytes por segundo
    v.setUint16(32, 2, true);          // bytes por amostra
    v.setUint16(34, 16, true);         // bits por amostra
    texto(36, "data");
    v.setUint32(40, amostras.length * 2, true);
    for (let i = 0; i < amostras.length; i++) v.setInt16(44 + i * 2, amostras[i], true);
    return new Uint8Array(bytes);
  }

  // ---------- Fim da fala: detector de voz por energia ----------

  /*
   * Decide quando o comando acabou. Enquanto espera a palavra, guarda a energia (RMS) dos últimos 5 s em
   * quadros de 20 ms; o nível de ruído é o 20º percentil disso (a fala não o puxa para cima). Na gravação,
   * um quadro é fala acima de 3x o ruído e silêncio abaixo de 2x (com mínimos para uma sala muito quieta).
   * Só conta como fala quando 8 dos últimos 15 quadros (160 de 300 ms) passam do limiar: o bipe da
   * detecção, se voltar pelo microfone, e estalos curtos não bastam.
   */
  class DetectorDeVoz {
    constructor({
      taxa = TAXA, quadro = 0.02, silencio = 0.8, maximo = 8, semFala = 4, memoria = 5,
      fatorFala = 3, fatorSilencio = 2, minimoFala = 200, minimoSilencio = 120, inicio = 8, janela = 15,
    } = {}) {
      this.q = Math.round(taxa * quadro);
      this.quadrosSilencio = Math.round(silencio / quadro);
      this.quadrosMaximo = Math.round(maximo / quadro);
      this.quadrosSemFala = Math.round(semFala / quadro);
      Object.assign(this, { fatorFala, fatorSilencio, minimoFala, minimoSilencio, inicio, janela });
      this.energias = new Float32Array(Math.round(memoria / quadro));
      this.guardadas = 0;
      this.posicao = 0;
      this.sobraRuido = new Int16Array(0);
      this.comecar();
    }

    static rms(amostras, de, ate) {
      let soma = 0;
      for (let i = de; i < ate; i++) soma += amostras[i] * amostras[i];
      return Math.sqrt(soma / (ate - de));
    }

    /** Separa em quadros inteiros, guardando a sobra para a próxima vez. */
    _quadros(sobra, amostras, fazer) {
      const junto = sobra.length ? juntar([sobra, amostras]) : amostras;
      let i = 0;
      for (; i + this.q <= junto.length; i += this.q) {
        const r = fazer(DetectorDeVoz.rms(junto, i, i + this.q));
        if (r) return { sobra: new Int16Array(0), fim: r };
      }
      return { sobra: junto.slice(i), fim: null };
    }

    /** Áudio de enquanto espera a palavra: só alimenta a estimativa do ruído. */
    ouvirRuido(amostras) {
      this.sobraRuido = this._quadros(this.sobraRuido, amostras, e => {
        this.energias[this.posicao] = e;
        this.posicao = (this.posicao + 1) % this.energias.length;
        this.guardadas = Math.min(this.guardadas + 1, this.energias.length);
      }).sobra;
    }

    ruido() {
      if (!this.guardadas) return 0;
      const ordenadas = Array.from(this.energias.subarray(0, this.guardadas)).sort((a, b) => a - b);
      return ordenadas[Math.floor(0.2 * (ordenadas.length - 1))];
    }

    /** Começa a gravação: fixa os limiares com o ruído de agora. */
    comecar() {
      const ruido = this.ruido();
      this.limiarFala = Math.max(ruido * this.fatorFala, this.minimoFala);
      this.limiarSilencio = Math.max(ruido * this.fatorSilencio, this.minimoSilencio);
      this.quadros = 0;
      this.falou = false;
      this.silencio = 0;
      this.altos = [];
      this.sobra = new Int16Array(0);
    }

    /** Áudio da gravação. Devolve "silencio", "maximo" ou "sem_fala" quando acabou; null enquanto continua. */
    ouvir(amostras) {
      const r = this._quadros(this.sobra, amostras, e => this._quadro(e));
      this.sobra = r.sobra;
      return r.fim;
    }

    _quadro(energia) {
      this.quadros++;
      const alto = energia >= this.limiarFala;
      if (!this.falou) {
        this.altos.push(alto);
        if (this.altos.length > this.janela) this.altos.shift();
        if (this.altos.filter(Boolean).length >= this.inicio) this.falou = true;
      }
      this.silencio = energia < this.limiarSilencio ? this.silencio + 1 : 0;
      if (this.falou && this.silencio >= this.quadrosSilencio) return "silencio";
      if (this.quadros >= this.quadrosMaximo) return this.falou ? "maximo" : "sem_fala";
      if (!this.falou && this.quadros >= this.quadrosSemFala) return "sem_fala";
      return null;
    }
  }

  // ---------- O pipeline do openWakeWord ----------

  // As sessões ONNX rodam uma de cada vez, mesmo com duas escutas (ligar e desligar rápido).
  let filaDoOnnx = Promise.resolve();
  function rodar(sessao, entradas) {
    const resultado = filaDoOnnx.then(() => sessao.run(entradas));
    filaDoOnnx = resultado.catch(() => {});
    return resultado;
  }

  /*
   * O mesmo cálculo do openwakeword (AudioFeatures._streaming_features e Model.predict), pedaço a pedaço:
   *  1. melspectrogram das 1280 amostras novas mais as 480 anteriores, como float32 com os valores do int16
   *     (sem dividir por 32768): [1, 1760] → [1, 1, 8, 32] (no primeiro pedaço, sem as anteriores: 5 quadros);
   *     cada valor vira x / 10 + 2;
   *  2. os quadros entram num buffer que começa com 76 linhas de 1; os 76 últimos, [1, 76, 32, 1], dão um
   *     embedding de 96 números por pedaço (a janela anda 8 quadros por vez);
   *  3. os 16 últimos embeddings, [1, 16, 96], dão a pontuação (de 0 a 1); a partir de 0,5 é "hey Jarvis".
   * Diferença do Python: ele começa com embeddings de ruído aleatório e zera as 5 primeiras pontuações;
   * aqui o classificador só roda com 16 embeddings de verdade (1,28 s depois de começar ou de zerar).
   * Depois de uma detecção, os buffers zeram e o classificador fica parado por 25 pedaços (2 s), contados
   * desde a detecção: os pedaços da gravação do comando, que não passam por aqui, entram na conta
   * (descontarPausa), senão o "Diga hey Jarvis de novo" depois de uma gravação sem fala seria ignorado.
   */
  class PipelineJarvis {
    constructor({ sessoes, Tensor, limiar = LIMIAR, pausa = PAUSA }) {
      this.sessoes = sessoes;
      this.Tensor = Tensor;
      this.limiar = limiar;
      this.pausaDepois = pausa;
      this.pausa = 0;
      this.versao = 0;
      this.reiniciar();
    }

    reiniciar() {
      this.versao++;
      this.anteriores = new Float32Array(0);
      this.mel = [];
      for (let i = 0; i < QUADROS; i++) this.mel.push(new Float32Array(BANDAS).fill(1));
      this.embeddings = [];
    }

    /** Passou um pedaço (80 ms) sem o detector, como na gravação do comando: conta para a pausa. */
    descontarPausa() {
      if (this.pausa > 0) this.pausa--;
    }

    async _rodar(nome, dados, formato) {
      const sessao = this.sessoes[nome];
      const saidas = await rodar(sessao, { [sessao.inputNames[0]]: new this.Tensor("float32", dados, formato) });
      return saidas[sessao.outputNames[0]].data;
    }

    /** Um pedaço de 1280 amostras (Int16Array). Devolve { pontuacao (null sem classificador), detectou }. */
    async processar(pedaco) {
      if (pedaco.length !== PEDACO) throw new RangeError(`o pedaço precisa ter ${PEDACO} amostras`);
      const versao = this.versao;
      const nada = { pontuacao: null, detectou: false };

      const audio = new Float32Array(this.anteriores.length + PEDACO);
      audio.set(this.anteriores);
      audio.set(pedaco, this.anteriores.length);  // o int16 vira float32 sem mudar o valor
      const mel = await this._rodar("mel", audio, [1, audio.length]);
      if (versao !== this.versao) return nada;  // zerado enquanto o ONNX rodava
      this.anteriores = audio.slice(-CONTEXTO);
      for (let t = 0; t + BANDAS <= mel.length; t += BANDAS) {
        const quadro = new Float32Array(BANDAS);
        // Em float32 como no numpy: fround(fround(x / 10) + 2).
        for (let b = 0; b < BANDAS; b++) quadro[b] = Math.fround(Math.fround(mel[t + b] / 10) + 2);
        this.mel.push(quadro);
      }
      this.mel.splice(0, this.mel.length - QUADROS);

      const janela = new Float32Array(QUADROS * BANDAS);
      this.mel.forEach((quadro, i) => janela.set(quadro, i * BANDAS));
      const embedding = await this._rodar("embedding", janela, [1, QUADROS, BANDAS, 1]);
      if (versao !== this.versao) return nada;
      this.embeddings.push(Float32Array.from(embedding.subarray(0, DIMENSAO)));
      this.embeddings.splice(0, this.embeddings.length - EMBEDDINGS);

      if (this.pausa > 0) {
        this.pausa--;
        return nada;
      }
      if (this.embeddings.length < EMBEDDINGS) return nada;
      const entrada = juntar(this.embeddings, Float32Array);
      const saida = await this._rodar("classificador", entrada, [1, EMBEDDINGS, DIMENSAO]);
      if (versao !== this.versao) return nada;
      const pontuacao = saida[0];
      const detectou = pontuacao >= this.limiar;
      if (detectou) {
        this.reiniciar();
        this.pausa = this.pausaDepois;
      }
      return { pontuacao, detectou };
    }
  }

  // ---------- Carregar o onnxruntime-web e os modelos ----------

  function carregarScript(url, integridade, tempo = 30000) {
    return new Promise((ok, falha) => {
      const script = document.createElement("script");
      const espera = setTimeout(() => falha(new Error("tempo esgotado")), tempo);
      script.src = url;
      if (integridade) { script.integrity = integridade; script.crossOrigin = "anonymous"; }
      script.onload = () => { clearTimeout(espera); ok(); };
      script.onerror = () => { clearTimeout(espera); falha(new Error("o script não carregou")); };
      document.head.appendChild(script);
    });
  }

  let ortCarregando = null;
  /** O onnxruntime-web, da CDN (uma vez por página). Sem internet, lança o erro "onnx". */
  function carregarOrt(opcoes = {}) {
    if (raiz.ort) return Promise.resolve(raiz.ort);
    if (!ortCarregando) {
      ortCarregando = carregarScript(opcoes.ortUrl || ORT_URL, opcoes.ortIntegridade ?? ORT_INTEGRIDADE)
        .then(() => {
          if (!raiz.ort) throw new Error("o script não definiu o ort");
          raiz.ort.env.wasm.wasmPaths = opcoes.ortDist || ORT_DIST;  // os .wasm e .mjs vêm da mesma pasta da CDN
          raiz.ort.env.wasm.numThreads = 1;  // sem o isolamento de origem (COOP/COEP), não há threads
          return raiz.ort;
        })
        .catch(e => { ortCarregando = null; throw new ErroDaEscuta("onnx"); });
    }
    return ortCarregando;
  }

  async function baixarModelo(base, arquivo) {
    let r;
    try {
      r = await fetch(base + "modelos/" + arquivo);
    } catch {
      throw new ErroDaEscuta("servidor_fora");
    }
    if (r.status === 404) throw new ErroDaEscuta("modelos");
    if (!r.ok) throw new ErroDaEscuta("http", erroHttp(r.status));
    try {
      return new Uint8Array(await r.arrayBuffer());
    } catch {
      throw new ErroDaEscuta("servidor_fora");  // a conexão caiu no meio do arquivo
    }
  }

  let sessoesCarregando = null;
  /** As três sessões ONNX (uma vez por página): primeiro os modelos do servidor, depois o runtime da CDN. */
  function carregarSessoes(opcoes = {}) {
    if (!sessoesCarregando) {
      sessoesCarregando = (async () => {
        const base = opcoes.base || BASE;
        const nomes = Object.keys(MODELOS);
        const arquivos = await Promise.all(nomes.map(n => baixarModelo(base, MODELOS[n])));
        const ort = await carregarOrt(opcoes);
        const sessoes = {};
        try {
          for (let i = 0; i < nomes.length; i++) {
            sessoes[nomes[i]] = await ort.InferenceSession.create(arquivos[i], { executionProviders: ["wasm"] });
          }
        } catch (e) {
          // Sem internet, o .wasm não chega e o erro fala em "backend"; o resto é arquivo estragado.
          throw new ErroDaEscuta(/backend|wasm|fetch|import/i.test(String(e && e.message)) ? "onnx" : "modelos_com_defeito");
        }
        return { sessoes, Tensor: ort.Tensor };
      })();
      sessoesCarregando.catch(() => { sessoesCarregando = null; });
    }
    return sessoesCarregando;
  }

  // ---------- Captura: o AudioWorklet ----------

  // Roda na thread de áudio: só junta as amostras (a média dos canais) em blocos e manda para a página.
  function processadorDeCaptura() {
    class CapturaJarvis extends AudioWorkletProcessor {
      constructor(opcoes) {
        super();
        this.tamanho = (opcoes && opcoes.processorOptions && opcoes.processorOptions.tamanho) || 2048;
        this.bloco = new Float32Array(this.tamanho);
        this.usado = 0;
        this.ativo = true;
        this.port.onmessage = e => { if (e.data === "parar") this.ativo = false; };
      }

      process(entradas) {
        const canais = entradas[0];
        if (this.ativo && canais && canais.length) {
          const n = canais[0].length;
          for (let i = 0; i < n; i++) {
            let soma = 0;
            for (let c = 0; c < canais.length; c++) soma += canais[c][i];
            this.bloco[this.usado++] = soma / canais.length;
            if (this.usado === this.tamanho) {
              this.port.postMessage(this.bloco, [this.bloco.buffer]);
              this.bloco = new Float32Array(this.tamanho);
              this.usado = 0;
            }
          }
        }
        return this.ativo;
      }
    }
    registerProcessor("captura-jarvis", CapturaJarvis);
  }
  // Vai para o AudioWorklet por um Blob: não depende do tipo que o servidor dá aos .js (no Windows, o
  // registro às vezes troca text/javascript por text/plain, e o addModule recusa).
  const CODIGO_DO_PROCESSADOR = `(${processadorDeCaptura.toString()})();\n`;
  const contextosPreparados = new WeakMap();

  function prepararProcessador(contexto) {
    if (!contextosPreparados.has(contexto)) {
      const url = URL.createObjectURL(new Blob([CODIGO_DO_PROCESSADOR], { type: "text/javascript" }));
      const pronto = contexto.audioWorklet.addModule(url);
      pronto.catch(() => contextosPreparados.delete(contexto));
      contextosPreparados.set(contexto, pronto);
    }
    return contextosPreparados.get(contexto);
  }

  async function pedirMicrofone(midia) {
    try {
      return await midia.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      const nome = e && e.name;
      if (nome === "NotAllowedError" || nome === "SecurityError") throw new ErroDaEscuta("microfone_negado");
      if (nome === "NotFoundError" || nome === "OverconstrainedError") throw new ErroDaEscuta("sem_microfone");
      if (nome === "NotReadableError" || nome === "AbortError") throw new ErroDaEscuta("microfone_ocupado");
      throw new ErroDaEscuta("falhou", erroInesperado(e));
    }
  }

  // ---------- A escuta: junta tudo ----------

  /*
   * Opções: contexto() devolve o AudioContext da página (o mesmo da voz); ocupada() diz se o botão de falar
   * está gravando (aí a escuta para e descarta o que gravava); e os avisos aoDetectar(), aoComando(wav,
   * motivo), aoSemFala(), aoErro(mensagem) e, para diagnóstico, aoPontuar(pontuacao).
   * Estados: desligada → carregando → esperando ⇄ gravando.
   */
  class Escuta {
    constructor(opcoes) {
      this.o = opcoes;
      this.estado = "desligada";
      this.geracao = 0;
    }

    async ligar() {
      if (this.estado !== "desligada") return;
      const geracao = ++this.geracao;
      this.estado = "carregando";
      try {
        const midia = raiz.navigator && raiz.navigator.mediaDevices;
        if (!midia || !midia.getUserMedia) throw new ErroDaEscuta("sem_gravacao");
        const contexto = this.o.contexto();
        if (!contexto || !contexto.audioWorklet || typeof raiz.AudioWorkletNode !== "function") {
          throw new ErroDaEscuta("sem_worklet");
        }
        const [microfone, modelos] = await Promise.allSettled([pedirMicrofone(midia), carregarSessoes(this.o)]);
        if (microfone.status === "fulfilled") {
          if (geracao !== this.geracao) return pararFluxo(microfone.value);  // desligaram enquanto carregava
          this.fluxo = microfone.value;
        }
        if (geracao !== this.geracao) return;
        if (microfone.status === "rejected") throw microfone.reason;
        if (modelos.status === "rejected") throw modelos.reason;
        await prepararProcessador(contexto);
        if (geracao !== this.geracao) return;

        this.reamostrador = new Reamostrador(contexto.sampleRate, TAXA);
        this.pipeline = new PipelineJarvis(modelos.value);
        this.voz = new DetectorDeVoz();
        this.resto = new Int16Array(0);
        this.fila = [];
        this.gravacao = [];
        this.processando = false;
        this.interrompida = false;

        this.fonte = contexto.createMediaStreamSource(this.fluxo);
        this.no = new AudioWorkletNode(contexto, "captura-jarvis", {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], processorOptions: { tamanho: 2048 },
        });
        this.no.port.onmessage = e => this.receber(e.data);
        this.fonte.connect(this.no);
        this.no.connect(contexto.destination);  // sem som: o processador não escreve na saída
        this.fluxo.getAudioTracks().forEach(faixa => {
          faixa.onended = () => this.falhar(new ErroDaEscuta("microfone_parou"));
        });
        this.estado = "esperando";
      } catch (e) {
        if (geracao !== this.geracao) return;
        this.desligar();
        throw e instanceof ErroDaEscuta ? e : new ErroDaEscuta("falhou", erroInesperado(e));
      }
    }

    /** Solta o microfone e para tudo. */
    desligar() {
      this.geracao++;
      if (this.no) {
        this.no.port.onmessage = null;
        try { this.no.port.postMessage("parar"); } catch {}
        try { this.no.disconnect(); } catch {}
      }
      if (this.fonte) try { this.fonte.disconnect(); } catch {}
      if (this.fluxo) pararFluxo(this.fluxo);
      this.no = this.fonte = this.fluxo = null;
      this.fila = [];
      this.gravacao = [];
      this.estado = "desligada";
    }

    falhar(erro) {
      if (this.estado === "desligada") return;
      this.desligar();
      this.o.aoErro(erro.message);
    }

    /** Um bloco do AudioWorklet (Float32Array na taxa do AudioContext). */
    receber(bloco) {
      if (this.estado !== "esperando" && this.estado !== "gravando") return;
      if (this.o.ocupada && this.o.ocupada()) {
        // O botão de falar está gravando: a escuta descarta o que tinha e espera ele terminar.
        if (!this.interrompida) {
          this.interrompida = true;
          this.estado = "esperando";
          this.fila = [];
          this.gravacao = [];
          this.resto = new Int16Array(0);
          this.pipeline.reiniciar();
        }
        return;
      }
      this.interrompida = false;
      const novas = paraInt16(this.reamostrador.processar(bloco));
      const tudo = this.resto.length ? juntar([this.resto, novas]) : novas;
      let i = 0;
      for (; i + PEDACO <= tudo.length; i += PEDACO) this._pedaco(tudo.slice(i, i + PEDACO));
      this.resto = tudo.slice(i);
    }

    _pedaco(pedaco) {
      if (this.estado === "gravando") return this._gravar(pedaco);
      if (this.estado !== "esperando") return;
      this.voz.ouvirRuido(pedaco);
      this.fila.push(pedaco);
      if (this.fila.length > 100) {  // aparelho lento demais: descarta o atraso em vez de acumular
        this.fila.splice(0, this.fila.length - 1);
        this.pipeline.reiniciar();
      }
      this._processarFila();
    }

    async _processarFila() {
      if (this.processando) return;
      this.processando = true;
      const geracao = this.geracao;
      try {
        while (this.fila.length && this.estado === "esperando") {
          const r = await this.pipeline.processar(this.fila.shift());
          if (geracao !== this.geracao) return;
          if (r.pontuacao !== null && this.o.aoPontuar) this.o.aoPontuar(r.pontuacao);
          if (r.detectou) this._detectar();
        }
      } catch (e) {
        if (geracao === this.geracao) this.falhar(new ErroDaEscuta("falhou", erroInesperado(e)));
      } finally {
        if (geracao === this.geracao) this.processando = false;
      }
    }

    _detectar() {
      this.estado = "gravando";
      this.gravacao = [];
      this.voz.comecar();
      const depois = this.fila;  // o áudio que chegou enquanto o ONNX rodava já é parte do comando
      this.fila = [];
      this.o.aoDetectar();
      for (const pedaco of depois) if (this.estado === "gravando") this._gravar(pedaco);
    }

    _gravar(pedaco) {
      this.pipeline.descontarPausa();
      this.gravacao.push(pedaco);
      const fim = this.voz.ouvir(pedaco);
      if (!fim) return;
      const audio = juntar(this.gravacao);
      this.gravacao = [];
      this.estado = "esperando";  // volta a esperar a palavra já, inclusive enquanto a assistente responde
      if (fim === "sem_fala") this.o.aoSemFala();
      else this.o.aoComando(criarWav(audio), fim);
    }
  }

  function pararFluxo(fluxo) {
    fluxo.getTracks().forEach(t => { t.onended = null; t.stop(); });
  }

  const Jarvis = {
    TAXA, PEDACO, CONTEXTO, QUADROS, BANDAS, EMBEDDINGS, DIMENSAO, LIMIAR, PAUSA, BASE, MODELOS,
    VERSAO_ORT, ORT_URL, ORT_DIST, ORT_INTEGRIDADE, MENSAGENS, CODIGO_DO_PROCESSADOR,
    ErroDaEscuta, Reamostrador, paraInt16, juntar, criarWav, DetectorDeVoz, PipelineJarvis,
    carregarOrt, carregarSessoes, Escuta,
  };
  raiz.Jarvis = Jarvis;
  if (typeof module === "object" && module.exports) module.exports = Jarvis;
})(typeof globalThis !== "undefined" ? globalThis : self);
