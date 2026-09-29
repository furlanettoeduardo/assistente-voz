"""
Testes da voz do servidor (celular_servidor/voz.py) e da integração com o servidor. O Piper é trocado
por um falso que grava um WAV curto; o teste com o Piper de verdade só roda se ele e a voz estiverem
instalados. Nenhum teste toca áudio.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from tests.auxiliares import PASTA_SERVIDOR, LLMFalso, resposta_texto
from tests.test_servidor import PRECISA_DEPENDENCIAS, carregar_servidor

spec = importlib.util.spec_from_file_location("voz_em_teste", PASTA_SERVIDOR / "voz.py")
voz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voz)

NOME = "pt_BR-teste-medium"


class PiperFalso:
    """Imita o PiperVoice: grava 0,1 s de silêncio e guarda os textos recebidos."""

    def __init__(self, erro_na_sintese=None):
        self.config = mock.Mock(sample_rate=22050)
        self.textos = []
        self.erro_na_sintese = erro_na_sintese

    def synthesize_wav(self, texto, wav, set_wav_format=True):
        self.textos.append(texto)
        if self.erro_na_sintese:
            raise self.erro_na_sintese
        wav.writeframes(b"\x00\x00" * 2205)


def criar_arquivos(pasta: Path, nome: str = NOME) -> None:
    (pasta / f"{nome}.onnx").write_bytes(b"modelo")
    (pasta / f"{nome}.onnx.json").write_text("{}", encoding="utf-8")


class TestNormalizar(unittest.TestCase):
    def test_casos_que_o_piper_erra(self):
        casos = {
            "São 10h33.": "São 10 horas e 33.",
            "Às 07:05": "Às 7 horas e 5",
            "1h": "uma hora", "21h": "vinte e uma horas", "às 22h30": "às vinte e duas horas e 30",
            "12:00": "12 horas", "2 horas": "duas horas",
            "22°C": "22 graus", "1 °C": "1 grau", "-3°C": "-3 graus", "5°": "5 graus",
            "R$ 12,50": "12 reais e 50 centavos", "R$ 1": "1 real", "R$ 1.500,00": "1500 reais",
            "R$ 12,01": "12 reais e 1 centavo", "R$ 12,5": "12 reais e 50 centavos",
            "versão 3.5": "versão 3,5", "1.000 pessoas": "1.000 pessoas", "29.09.2026": "29.09.2026",
            "90 km/h": "90 quilômetros por hora",
            "Pronto 👍🏽 **abri** o Spotify ✅": "Pronto abri o Spotify",
            "veja [o site](https://exemplo.com)": "veja o site",
            "1º lugar e 2ª feira": "1º lugar e 2ª feira",
            "15% de chance, 22 graus em SC": "15% de chance, 22 graus em SC",
            "Custa R$ 1,5 milhão.": "Custa 1,5 milhão de reais.", "uns R$ 50 mil": "uns 50 mil reais",
            "R$ 2,5 bilhões": "2,5 bilhões de reais", "R$ 0,99": "99 centavos",
            "Chego em 2h30min": "Chego em duas horas e 30", "Às 10:33h": "Às 10 horas e 33",
            "aberta 24h": "aberta 24 horas", "ventos de 90km/h": "ventos de 90 quilômetros por hora",
            "90 Km/h": "90 quilômetros por hora",
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(voz.normalizar(texto), esperado)


class TestVoz(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.pasta = Path(tmp.name)
        criar_arquivos(self.pasta)

    def criar(self, piper=None, **extra):
        self.piper = piper or PiperFalso()
        return voz.Voz(NOME, self.pasta, carregar=lambda arquivo: self.piper, **extra)

    def test_gera_wav_do_texto_normalizado(self):
        objeto = self.criar()
        dados = objeto.sintetizar("São 10h33 👍")
        with wave.open(io.BytesIO(dados)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 22050))
            self.assertEqual(wav.getnframes(), 2205)
        self.assertEqual(self.piper.textos, [voz.TESTE, "São 10 horas e 33"])  # o 1º é o teste ao carregar

    def test_nada_para_falar(self):
        objeto = self.criar()
        for texto in ("", "   ", "...", "👍", None):
            with self.subTest(texto=texto):
                self.assertIsNone(objeto.sintetizar(texto))
        self.assertEqual(self.piper.textos, [voz.TESTE])

    def test_falha_na_sintese_sobe_com_o_erro_certo(self):
        objeto = self.criar()
        self.piper.erro_na_sintese = RuntimeError("onnx quebrou")
        with self.assertRaisesRegex(RuntimeError, "onnx quebrou"):  # e não um wave.Error
            objeto.sintetizar("oi")

    def test_erros_ao_preparar(self):
        def carregar_com(erro):
            def carregar(arquivo):
                raise erro
            return carregar

        bloqueio = ImportError("DLL load failed while importing espeakbridge: Uma política de Controle de "
                               "Aplicativo bloqueou este arquivo.")
        casos = [
            (carregar_com(ImportError("No module named 'piper'", name="piper")), "sem_piper", "falta o piper-tts"),
            (carregar_com(bloqueio), "bloqueado", "Smart App Control"),
            (carregar_com(RuntimeError("INVALID_PROTOBUF")), "falhou", "--force-redownload"),
        ]
        for carregar, codigo, trecho in casos:
            with self.subTest(codigo=codigo), self.assertRaises(voz.ErroNaVoz) as erro:
                voz.Voz(NOME, self.pasta, carregar=carregar)
            self.assertEqual(erro.exception.codigo, codigo)
            self.assertIn(trecho, str(erro.exception))

    def test_falta_o_visual_cpp(self):
        erro_dll = ImportError("DLL load failed while importing onnxruntime_pybind11_state: Não foi possível "
                               "encontrar o módulo especificado.", name="onnxruntime.capi.onnxruntime_pybind11_state")

        def carregar(arquivo):
            raise erro_dll

        with self.assertRaises(voz.ErroNaVoz) as erro:
            voz.Voz(NOME, self.pasta, carregar=carregar)
        self.assertIn("Visual C++ Redistributable", str(erro.exception))

    def test_erro_na_voz_ao_carregar_sobe_como_esta(self):
        def carregar(arquivo):
            raise voz.ErroNaVoz("o Piper não funciona com acento no caminho da pasta")

        with self.assertRaisesRegex(voz.ErroNaVoz, "acento no caminho"):
            voz.Voz(NOME, self.pasta, carregar=carregar)

    def test_no_android_nao_manda_instalar(self):
        with mock.patch.object(voz.importlib.util, "find_spec", return_value=None), \
                mock.patch.dict(voz.os.environ, {"TERMUX_VERSION": "0.119"}), \
                self.assertRaises(voz.ErroNaVoz) as erro:
            voz.Voz(NOME, self.pasta)
        self.assertEqual(erro.exception.codigo, "sem_piper")
        self.assertNotIn("pip install", str(erro.exception))
        self.assertIn("Android", str(erro.exception))

    def test_caminho_sem_acento(self):
        if str(self.pasta).isascii():  # sem acento, o caminho fica como está
            self.assertEqual(voz.caminho_sem_acento(self.pasta), str(self.pasta))
        com_acento = self.pasta / "Área de Trabalho"
        com_acento.mkdir()
        try:
            curto = voz.caminho_sem_acento(com_acento)
        except voz.ErroNaVoz as erro:  # sem nomes curtos (8.3) no disco, ou fora do Windows
            self.assertIn("mova o projeto", str(erro))
        else:
            self.assertTrue(curto.isascii(), curto)
            self.assertTrue(Path(curto).samefile(com_acento))

    def test_bloqueio_do_windows_so_na_primeira_sintese(self):
        # O Smart App Control só barra a parte do Piper que lê o texto quando ela é usada.
        with self.assertRaises(voz.ErroNaVoz) as erro:
            self.criar(PiperFalso(erro_na_sintese=ImportError("DLL load failed while importing espeakbridge")))
        self.assertEqual(erro.exception.codigo, "bloqueado")

    def test_arquivo_da_voz_faltando(self):
        (self.pasta / f"{NOME}.onnx.json").unlink()
        with self.assertRaises(voz.ErroNaVoz) as erro:
            self.criar()
        self.assertEqual(erro.exception.codigo, "sem_arquivo")
        self.assertIn(f"python -m piper.download_voices {NOME} --data-dir vozes", str(erro.exception))

    def test_sem_piper_instalado_nem_procura_o_arquivo(self):
        with mock.patch.object(voz.importlib.util, "find_spec", return_value=None), \
                self.assertRaises(voz.ErroNaVoz) as erro:
            voz.Voz(NOME, self.pasta / "nao-existe")
        self.assertEqual(erro.exception.codigo, "sem_piper")
        self.assertIn("pip install -r requirements-voz.txt", str(erro.exception))

    def test_preparar(self):
        self.assertEqual(voz.preparar(None)[:2], (None, None))
        self.assertIn("a do navegador", voz.preparar(None)[2])
        objeto, erro, linha = voz.preparar(NOME, self.pasta, carregar=lambda arquivo: PiperFalso())
        self.assertIsNotNone(objeto)
        self.assertIsNone(erro)
        self.assertEqual(linha, f"[servidor] voz: {NOME}, gerada no servidor.")
        objeto, erro, linha = voz.preparar("pt_BR-outra-medium", self.pasta, carregar=lambda arquivo: PiperFalso())
        self.assertIsNone(objeto)
        self.assertEqual(erro.codigo, "sem_arquivo")
        self.assertTrue(linha.startswith("[servidor] voz do servidor desligada, vale a do navegador: "))


VOZ_DE_VERDADE = voz.PASTA_VOZES / "pt_BR-cadu-medium.onnx"


@unittest.skipUnless(importlib.util.find_spec("piper") and VOZ_DE_VERDADE.is_file(),
                     "o piper-tts e a voz pt_BR-cadu-medium não estão instalados (a voz do servidor é opcional)")
class TestPiperDeVerdade(unittest.TestCase):
    def test_sintetiza_uma_frase(self):
        objeto = voz.Voz("pt_BR-cadu-medium")
        dados = objeto.sintetizar("Pronto, abri o Spotify. São 10h33.")
        with wave.open(io.BytesIO(dados)) as wav:
            self.assertEqual(wav.getframerate(), 22050)
            self.assertGreater(wav.getnframes() / wav.getframerate(), 1.0)  # mais de 1 s de fala


@PRECISA_DEPENDENCIAS
class TestVozNoServidor(unittest.TestCase):
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
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        self.terminal = pilha.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def com_voz(self, piper):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        criar_arquivos(Path(tmp.name))
        objeto = voz.Voz(NOME, Path(tmp.name), carregar=lambda arquivo: piper)
        patcher = mock.patch.object(self.servidor, "VOZ", objeto)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enviar(self, texto: str) -> dict:
        self.llm.programar(resposta_texto("São 10h33."))
        return self.servidor.app.test_client().post("/texto", json={"texto": texto}).get_json()

    def test_resposta_traz_o_audio(self):
        piper = PiperFalso()
        self.com_voz(piper)
        dados = self.enviar("que horas são?")
        self.assertEqual(dados["resposta"], "São 10h33.")
        with wave.open(io.BytesIO(base64.b64decode(dados["audio"]))) as wav:
            self.assertEqual(wav.getframerate(), 22050)
        self.assertEqual(piper.textos[-1], "São 10 horas e 33.")

    def test_sem_voz_a_resposta_nao_muda(self):
        dados = self.enviar("que horas são?")
        self.assertNotIn("audio", dados)

    def test_falha_da_voz_nao_derruba_a_resposta(self):
        piper = PiperFalso()
        self.com_voz(piper)
        piper.erro_na_sintese = RuntimeError("onnx quebrou")
        dados = self.enviar("que horas são?")
        self.assertEqual(dados["resposta"], "São 10h33.")
        self.assertNotIn("audio", dados)
        self.assertIn("a voz do servidor falhou", self.terminal.getvalue())

    def test_config_da_voz(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            for valor, mensagem in ((5, '"voz" precisa ser um texto entre aspas'),
                                    ("../fora", '"voz" precisa ser o nome de uma voz do Piper')):
                with self.subTest(valor=valor), self.assertRaises(SystemExit) as saida:
                    carregar_servidor(Path(tmp) / f"s{len(mensagem)}", voz=valor)
                self.assertIn(mensagem, str(saida.exception.code))
            servidor = carregar_servidor(Path(tmp) / "exemplo", voz="pt_BR-cadu-medium")
        # Nas cópias dos testes a pasta vozes nunca vai junto: a voz fica desligada, com o motivo.
        self.assertIsNone(servidor.VOZ)
        self.assertIn(servidor.ERRO_DA_VOZ.codigo, ("sem_piper", "sem_arquivo"))
        self.assertTrue(servidor.AVISO_DA_VOZ.startswith("[servidor] voz do servidor desligada"))

    def test_exemplo_de_config_traz_a_voz(self):
        exemplo = json.loads((PASTA_SERVIDOR / "config_servidor.example.json").read_text(encoding="utf-8"))
        self.assertEqual(exemplo["voz"], "pt_BR-cadu-medium")


if __name__ == "__main__":
    unittest.main()
