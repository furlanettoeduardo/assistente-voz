"""
Voz das respostas gerada no servidor, com o Piper (piper-tts), em vez da voz do navegador.

É opcional: sem o piper-tts instalado (no Termux não há como) ou sem o arquivo da voz, o servidor
responde só com o texto e a página fala com a voz do navegador, como antes.

O Piper já lê inteiros, "%", ordinais e siglas; normalizar() corrige só o que ele erra: horas
("10h33" sairia "dez agá trinta e três"), "°C", "R$", decimal com ponto, emojis e markdown.
"""
import importlib.util
import io
import os
import re
import sys
import threading
import unicodedata
import wave
from collections.abc import Iterator
from pathlib import Path

PASTA_VOZES = Path(__file__).parent / "vozes"
TESTE = "Pronto."  # síntese feita ao subir: um bloqueio do Windows aparece logo, e não no primeiro pedido
INSTALAR = "na pasta celular_servidor, com o ambiente virtual ativado, rode: pip install -r requirements-voz.txt"
NO_ANDROID = "no Android (Termux) não há como instalar o Piper"

_FEMININO = {1: "uma", 2: "duas", 21: "vinte e uma", 22: "vinte e duas"}  # o Piper diria "dois horas"
_EMOJIS = {"️", "‍", "⃣"}  # variação, junção e tecla: sozinhos, viram lixo na fala


def _horas(hora: int, minutos: int) -> str:
    texto = f"{_FEMININO.get(hora, hora)} {'hora' if hora == 1 else 'horas'}"
    return f"{texto} e {minutos}" if minutos else texto


def _reais(achado: re.Match) -> str:
    reais = int(achado.group(1).replace(".", ""))
    centavos = int((achado.group(2) or "0").ljust(2, "0"))  # "12,5" é 12 reais e 50 centavos
    nome_dos_centavos = f"{centavos} {'centavo' if centavos == 1 else 'centavos'}"
    if not reais and centavos:  # "R$ 0,99": só os centavos
        return nome_dos_centavos
    texto = f"{reais} {'real' if reais == 1 else 'reais'}"
    return f"{texto} e {nome_dos_centavos}" if centavos else texto


def _reais_com_escala(achado: re.Match) -> str:
    """"R$ 1,5 milhão" -> "1,5 milhão de reais"; "R$ 50 mil" -> "50 mil reais" (a vírgula aqui é decimal)."""
    numero, escala = achado.group(1), achado.group(2)
    return f"{numero} {escala} reais" if escala.lower() == "mil" else f"{numero} {escala} de reais"


def normalizar(texto: str) -> str:
    """Deixa o texto pronto para o Piper ler em voz alta."""
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", texto)  # link de markdown: fica só o texto
    # 10h33, 10h, 10:33, 2h30min, 10:33h (só hora válida) -> "10 horas e 33"; "1h" -> "uma hora"
    t = re.sub(r"\b([01]?\d|2[0-3])(?:h|:)([0-5]\d)?(?:min|h)?\b(?!:?\d)",
               lambda a: _horas(int(a.group(1)), int(a.group(2) or 0)), t)
    t = re.sub(r"\b24h\b", "24 horas", t)
    t = re.sub(r"\b(1|2|21|22)(?= horas?\b)", lambda a: _FEMININO[int(a.group(1))], t)  # "2 horas" digitado
    # 22°C, 22 °C, 5° -> "22 graus" ("1º lugar", o ordinal, fica como está)
    t = re.sub(r"(-?\d+(?:,\d+)?)\s*(?:°\s*C?|ºC)(?![A-Za-z])",
               lambda a: a.group(1) + (" grau" if a.group(1) in ("1", "-1") else " graus"), t)
    t = re.sub(r"R\$\s*(\d+(?:,\d+)?)\s+(mil|milh(?:ão|ões)|bilh(?:ão|ões)|trilh(?:ão|ões))\b", _reais_com_escala, t,
               flags=re.IGNORECASE)
    t = re.sub(r"R\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\b", _reais, t)  # antes do decimal com ponto
    t = re.sub(r"(?<![\d.])(\d+)\.(\d{1,2})\b(?![.\d])", r"\1,\2", t)  # 3.5 -> 3,5; o milhar 1.000 fica
    t = re.sub(r"(?<=\d)\s*km/h\b|\bkm/h\b", " quilômetros por hora", t, flags=re.IGNORECASE)
    # Emojis (símbolos "So", modificadores de tom de pele "Sk" e os invisíveis que os juntam) e markdown.
    t = "".join(c for c in t if unicodedata.category(c) not in ("So", "Sk") and c not in _EMOJIS and c not in "*#_`")
    return re.sub(r"\s+", " ", t).strip()


class ErroNaVoz(Exception):
    """
    A voz não pôde ser preparada: a mensagem, em português, diz o que fazer. `codigo` é "sem_piper"
    (o piper-tts não está instalado, o que é normal no Termux), "sem_arquivo", "bloqueado" ou "falhou".
    """

    def __init__(self, mensagem: str, codigo: str = "falhou"):
        super().__init__(mensagem)
        self.codigo = codigo


def no_android() -> bool:
    return hasattr(sys, "getandroidapilevel") or "TERMUX_VERSION" in os.environ


def caminho_sem_acento(pasta: Path) -> str:
    """
    O espeak do Piper não abre pastas com acento no caminho ("Área de Trabalho", "João") e, em vez de dar
    erro, encerra o processo inteiro. No Windows, o caminho curto (8.3) da mesma pasta não tem acento.
    """
    caminho = str(pasta)
    if caminho.isascii():
        return caminho
    if sys.platform == "win32":
        import ctypes
        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(caminho, buffer, len(buffer)) and buffer.value.isascii():
            return buffer.value
    raise ErroNaVoz(f"o Piper não funciona com acento no caminho da pasta ({caminho}): mova o projeto (ou o "
                    "ambiente virtual) para uma pasta sem acento")


def carregar_piper(arquivo: Path):
    """Carrega a voz com o piper-tts (import aqui dentro: a dependência é opcional)."""
    from piper import PiperVoice
    from piper.phonemize_espeak import ESPEAK_DATA_DIR
    return PiperVoice.load(str(arquivo), espeak_data_dir=caminho_sem_acento(Path(ESPEAK_DATA_DIR)))


class Voz:
    def __init__(self, nome: str, pasta: Path = PASTA_VOZES, carregar=carregar_piper):
        self.nome = nome
        if carregar is carregar_piper and importlib.util.find_spec("piper") is None:
            raise ErroNaVoz(NO_ANDROID if no_android() else f"falta o piper-tts: {INSTALAR}", "sem_piper")
        arquivo = Path(pasta) / f"{nome}.onnx"
        if not arquivo.is_file() or not arquivo.with_name(f"{arquivo.name}.json").is_file():
            raise ErroNaVoz(f"não achei a voz {nome} em {pasta}: na pasta celular_servidor, rode "
                            f"python -m piper.download_voices {nome} --data-dir vozes", "sem_arquivo")
        try:
            self._piper = carregar(arquivo)
        except ErroNaVoz:
            raise
        except ImportError as e:
            if getattr(e, "name", None) in ("piper", "onnxruntime"):
                raise ErroNaVoz(f"falta o piper-tts: {INSTALAR}", "sem_piper") from e
            raise _bloqueio(e) from e
        except Exception as e:  # arquivo cortado (INVALID_PROTOBUF) e outras falhas do onnxruntime
            raise ErroNaVoz(f"não consegui carregar a voz {nome} (detalhe técnico: {e}); se o download foi "
                            f"interrompido, baixe de novo com --force-redownload") from e
        self._trava = threading.Lock()  # uma síntese por vez: latência e memória previsíveis
        try:
            audio = self.sintetizar(TESTE)
        except ImportError as e:  # o Windows bloqueia a parte do Piper que lê o texto só na 1ª síntese
            raise _bloqueio(e) from e
        except Exception as e:
            raise ErroNaVoz(f"a voz {nome} falhou no teste (detalhe técnico: {e})") from e
        if not audio:
            raise ErroNaVoz(f"a voz {nome} não gerou áudio no teste")

    @property
    def taxa(self) -> int:
        return self._piper.config.sample_rate

    def sintetizar(self, texto: str) -> bytes | None:
        """WAV (16 bits, mono) com o texto falado; None quando não sobra nada para falar."""
        texto = normalizar(texto or "")
        if not re.search(r"\w", texto):  # "", "..." ou só emoji: o Piper quebraria
            return None
        buffer = io.BytesIO()
        with self._trava, wave.open(buffer, "wb") as wav:
            # O formato vai antes: se a síntese falhar, a exceção dela sobe, e não um wave.Error.
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.taxa)
            self._piper.synthesize_wav(texto, wav, set_wav_format=False)
        dados = buffer.getvalue()
        return dados if len(dados) > 44 else None  # 44 bytes é só o cabeçalho

    def frases(self, texto: str) -> Iterator[bytes]:
        """
        Um WAV completo por frase, na ordem da fala, para a página começar a tocar antes do fim; nada
        quando não sobra o que falar. O Piper já divide o texto em frases (um pedaço por frase).
        """
        texto = normalizar(texto or "")
        if not re.search(r"\w", texto):  # "", "..." ou só emoji: o Piper quebraria
            return
        with self._trava:
            pedacos = iter(self._piper.synthesize(texto))
        while True:
            # A trava vale só para gerar a próxima frase: quem consome o gerador pode demorar entre uma
            # frase e outra (a rede, um cliente que sumiu), e os outros pedidos não ficam esperando por isso.
            with self._trava:
                pedaco = next(pedacos, None)
            if pedaco is None:
                return
            amostras = pedaco.audio_int16_bytes  # no Piper, cada leitura converte e copia o áudio de novo
            if amostras:
                yield _wav(amostras, pedaco.sample_rate, pedaco.sample_width, pedaco.sample_channels)


def _wav(amostras: bytes, taxa: int, largura: int, canais: int) -> bytes:
    """Monta um arquivo WAV em memória com as amostras dadas."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(canais)
        wav.setsampwidth(largura)
        wav.setframerate(taxa)
        wav.writeframes(amostras)
    return buffer.getvalue()


def _bloqueio(erro: ImportError) -> ErroNaVoz:
    texto = str(erro)
    if "espeakbridge" in texto or "Controle de Aplicativo" in texto or "Application Control" in texto:
        return ErroNaVoz("o Windows bloqueou uma parte do Piper (Smart App Control); a versão fixada no "
                         f"requirements-voz.txt costuma passar: {INSTALAR} (detalhe técnico: {texto})", "bloqueado")
    if "onnxruntime_pybind11_state" in texto or "DLL load failed" in texto:
        return ErroNaVoz("falta no Windows o Microsoft Visual C++ Redistributable (x64), de que o Piper precisa: "
                         "instale o vc_redist.x64 do site da Microsoft e suba o servidor de novo "
                         f"(detalhe técnico: {texto})")
    return ErroNaVoz(f"o piper-tts não carregou: {INSTALAR} (detalhe técnico: {texto})")


def preparar(nome: str | None, pasta: Path = PASTA_VOZES,
             carregar=carregar_piper) -> tuple["Voz | None", "ErroNaVoz | None", str]:
    """
    A voz pronta (ou None), o erro que impediu (ou None) e uma linha para o terminal do servidor
    explicando qual voz vale.
    """
    if not nome:
        return None, None, "[servidor] voz: a do navegador (a voz do servidor é opcional; veja o README)."
    try:
        voz = Voz(nome, pasta, carregar)
    except ErroNaVoz as e:
        return None, e, f"[servidor] voz do servidor desligada, vale a do navegador: {e}"
    return voz, None, f"[servidor] voz: {nome}, gerada no servidor."
