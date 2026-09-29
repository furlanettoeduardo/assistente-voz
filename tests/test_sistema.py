"""
Testes das ações no sistema (pc_agente/sistema.py). O subprocess.run e o ctypes são trocados por
falsos: nada muda de verdade no PC. A única chamada real é uma leitura do volume no Windows.
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import threading
import unittest
import uuid
from unittest import mock

from tests.auxiliares import PASTA_AGENTE, importar_copia

sistema = importar_copia(PASTA_AGENTE, "sistema")
CORE_AUDIO_REAL = sistema._core_audio  # guardado antes de qualquer mock.patch

CREATE_NO_WINDOW = 0x08000000


class TerminalFalso:
    """
    Faz o papel do subprocess.run: responde conforme o comando e guarda cada chamada.
    `respostas`: comando (tupla) -> resposta ou lista de respostas, na ordem (a última se repete).
    Resposta: (código, saída) ou (código, saída, erro), ou uma exceção para levantar.
    """

    def __init__(self, respostas: dict):
        self.respostas = {comando: list(r) if isinstance(r, list) else [r] for comando, r in respostas.items()}
        self.chamadas = []

    def __call__(self, comando, **opcoes):
        self.chamadas.append((comando, opcoes))
        fila = self.respostas.get(tuple(comando))
        if fila is None:
            raise AssertionError(f"comando inesperado: {comando}")
        resposta = fila.pop(0) if len(fila) > 1 else fila[0]
        if isinstance(resposta, BaseException):
            raise resposta
        codigo, saida, *erro = resposta
        return subprocess.CompletedProcess(comando, codigo, saida, erro[0] if erro else "")

    def comandos(self) -> list[list[str]]:
        return [comando for comando, _ in self.chamadas]


class BaseSistema(unittest.TestCase):
    plataforma = "win32"

    def setUp(self):
        self.esperas = []
        # Os PIDs dos testes são inventados e podem ser de um aplicativo aberto de verdade: nenhum teste pode
        # mandar WM_CLOSE para uma janela real.
        self.molduras = mock.Mock(return_value=0)
        for alvo, valor in (("PLATAFORMA", self.plataforma), ("dormir", self.esperas.append),
                            ("fechar_molduras_da_loja", self.molduras)):
            patcher = mock.patch.object(sistema, alvo, valor)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.erros = io.StringIO()  # o detalhe técnico que o agente mostra no terminal
        pilha = contextlib.ExitStack()
        self.addCleanup(pilha.close)
        pilha.enter_context(contextlib.redirect_stderr(self.erros))

    def terminal(self, respostas: dict | None = None) -> TerminalFalso:
        """Troca o subprocess.run; sem respostas, qualquer comando faz o teste falhar."""
        falso = TerminalFalso(respostas or {})
        patcher = mock.patch.object(sistema.subprocess, "run", falso)
        patcher.start()
        self.addCleanup(patcher.stop)
        return falso

    def conferir_opcoes(self, terminal: TerminalFalso) -> None:
        """Todo comando vai como lista, sem shell, com timeout e com a saída capturada."""
        self.assertTrue(terminal.chamadas)
        for comando, opcoes in terminal.chamadas:
            with self.subTest(comando=comando):
                self.assertIsInstance(comando, list)
                self.assertTrue(all(isinstance(parte, str) for parte in comando))
                self.assertFalse(opcoes.get("shell", False))
                self.assertTrue(opcoes.get("capture_output"))
                self.assertGreater(opcoes.get("timeout", 0), 0)
                self.assertIs(opcoes.get("stdin"), subprocess.DEVNULL)
                self.assertEqual(opcoes.get("errors"), "replace")
                if self.plataforma == "win32":
                    self.assertEqual(opcoes.get("creationflags"), CREATE_NO_WINDOW)
                    self.assertEqual(opcoes.get("encoding"), "oem")
                else:
                    self.assertNotIn("creationflags", opcoes)
                    self.assertEqual(opcoes.get("encoding"), "utf-8")


# ---------- Windows ----------

TASKKILL = ("taskkill", "/IM", "notepad.exe")
TASKLIST = ("tasklist", "/FI", "IMAGENAME eq notepad.exe", "/FO", "CSV", "/NH")
ABERTO = '"notepad.exe","4242","Console","1","25.000 K"\n'
NENHUM = "INFORMAÇÕES: nenhuma tarefa em execução correspondente aos critérios \nespecificados.\n"
NAO_ENCONTRADO = 'ERRO: o processo "notepad.exe" não foi encontrado.'
SO_FORCANDO = ('ERRO: o processo "notepad.exe" com PID 4242 não pôde ser finalizado.\n'
               "Razão: A finalização deste processo só pode ser forçada ( com a opção /F ).")


def comandos_do_taskkill(processo: str) -> tuple[tuple, tuple]:
    return ("taskkill", "/IM", processo), ("tasklist", "/FI", f"IMAGENAME eq {processo}", "/FO", "CSV", "/NH")


class TestFecharWindows(BaseSistema):
    def test_pede_para_fechar_sem_forcar_e_confere_com_o_tasklist(self):
        terminal = self.terminal({
            TASKKILL: (0, 'ÊXITO: sinal de encerramento enviado ao processo "notepad.exe" com PID 4242.'),
            TASKLIST: [(0, ABERTO), (0, ABERTO), (0, ABERTO), (0, NENHUM)],  # o 1º acha o PID
        })
        self.assertEqual(sistema.fechar(["notepad.exe"]), "fechou")
        self.assertEqual(terminal.comandos(), [list(TASKKILL)] + [list(TASKLIST)] * 4)
        self.molduras.assert_called_once_with({4242})
        self.assertEqual(self.esperas, [sistema.INTERVALO_FECHAR] * 2)
        self.conferir_opcoes(terminal)
        for comando in terminal.comandos():  # /F perderia o que não foi salvo; /T impede o WM_CLOSE
            self.assertNotIn("/F", [parte.upper() for parte in comando])
            self.assertNotIn("/T", [parte.upper() for parte in comando])

    def test_codigo_128_e_nao_aberto_sem_esperar(self):
        terminal = self.terminal({TASKKILL: (128, "", NAO_ENCONTRADO)})
        self.assertEqual(sistema.fechar(["notepad.exe"]), "nao_aberto")
        self.assertEqual(terminal.comandos(), [list(TASKKILL)])
        self.assertEqual(self.esperas, [])

    def test_outro_codigo_e_nao_fechou_sem_esperar(self):
        terminal = self.terminal({TASKKILL: (1, "", SO_FORCANDO), TASKLIST: (0, ABERTO)})
        self.assertEqual(sistema.fechar(["notepad.exe"]), "nao_fechou")
        self.assertEqual(terminal.comandos(), [list(TASKKILL), list(TASKLIST)])
        self.assertEqual(self.esperas, [])
        self.assertIn("taskkill exit code 1", self.erros.getvalue())  # o detalhe fica só no terminal

    def test_continua_aberto_depois_de_uns_4_segundos_e_pediu(self):
        terminal = self.terminal({TASKKILL: (0, "ÊXITO"), TASKLIST: (0, ABERTO)})
        self.assertEqual(sistema.fechar(["notepad.exe"]), "pediu")
        self.assertTrue(3.5 <= sum(self.esperas) <= 4.5, self.esperas)
        self.assertEqual(terminal.comandos().count(list(TASKLIST)), len(self.esperas) + 2)  # +1: o PID

    def test_aplicativo_da_loja_fecha_pela_moldura(self):
        # A Calculadora não tem janela própria: o taskkill "envia" (código 0), mas quem fecha é a moldura.
        calculadora, lista = comandos_do_taskkill("CalculatorApp.exe")
        linha = '"CalculatorApp.exe","31988","Console","1","30.000 K"'
        for codigo in (0, 1):  # com 1 (só forçando), a moldura também conta como pedido enviado
            with self.subTest(codigo=codigo):
                self.molduras.reset_mock(return_value=True)
                self.molduras.return_value = 1
                self.terminal({calculadora: (codigo, ""), lista: [(0, linha), (0, NENHUM)]})
                self.assertEqual(sistema.fechar(["CalculatorApp.exe"]), "fechou")
                self.molduras.assert_called_once_with({31988})

    def test_le_os_pids_do_tasklist(self):
        self.terminal({TASKLIST: (0, '"notepad.exe","4242","Console","1","1 K"\n"NOTEPAD.EXE","77","Console","1","1 K"\n'
                                     '"notepad.exe.bak","9","Console","1","1 K"\n')})
        self.assertEqual(sistema._pids_windows("notepad.exe"), {4242, 77})

    def test_varios_processos(self):
        code, lista_code = comandos_do_taskkill("Code.exe")
        ajudante, lista_ajudante = comandos_do_taskkill("CodeHelper.exe")
        extra, lista_extra = comandos_do_taskkill("Extra.exe")
        linha_ajudante = '"CodeHelper.exe","7","Console","1","1 K"'
        casos = [
            # O pedido chegou ao principal; o ajudante recusou mas saiu junto; o extra nem estava aberto.
            ({code: (0, ""), ajudante: (1, "", "só forçando"), extra: (128, ""),
              lista_code: (0, NENHUM), lista_ajudante: [(0, linha_ajudante), (0, NENHUM)]}, "fechou"),
            ({code: (0, ""), ajudante: (1, "", "só forçando"), extra: (128, ""),
              lista_code: (0, NENHUM), lista_ajudante: (0, linha_ajudante)}, "pediu"),
            ({code: (128, ""), ajudante: (1, "", "só forçando"), extra: (128, ""), lista_ajudante: (0, NENHUM)},
             "nao_fechou"),
            ({code: (128, ""), ajudante: (128, ""), extra: (128, "")}, "nao_aberto"),
        ]
        for respostas, esperado in casos:
            with self.subTest(esperado=esperado):
                terminal = self.terminal(respostas)
                self.assertEqual(sistema.fechar(["Code.exe", "CodeHelper.exe", "Extra.exe"]), esperado)
                self.assertNotIn(list(lista_extra), terminal.comandos())  # o que não estava aberto não é conferido

    def test_le_o_csv_do_tasklist(self):
        casos = [
            ('"NOTEPAD.EXE","1","Console","1","1.000 K"\n', True),  # sem diferença de maiúsculas
            ('"chrome.exe","9","Console","1","1 K"\r\n"notepad.exe","1","Console","1","1 K"\r\n', True),
            ('"notepad.exe.bak","1","Console","1","1 K"\n"xnotepad.exe","2","Console","1","1 K"\n', False),
            (NENHUM, False),
            ("INFO: No tasks are running which match the specified criteria.\n", False),
            ("", False),
        ]
        for saida, esperado in casos:
            with self.subTest(saida=saida):
                self.terminal({TASKLIST: (0, saida)})
                self.assertIs(sistema._existe_windows("notepad.exe"), esperado)

    def test_tasklist_com_erro_vira_erro_em_portugues(self):
        self.terminal({TASKKILL: (0, "ÊXITO"), TASKLIST: (1, "", "ERRO: filtro inválido")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.fechar(["notepad.exe"])
        self.assertEqual(str(erro.exception), "não consegui conferir se o programa fechou")
        self.assertIn("ERRO: filtro inválido", erro.exception.detalhe)

    def test_comando_que_nao_roda_vira_erro_em_portugues(self):
        casos = [
            (FileNotFoundError(2, "The system cannot find the file specified"),
             "não encontrei o comando taskkill neste PC"),
            (subprocess.TimeoutExpired(list(TASKKILL), 10), "o comando taskkill demorou demais para responder"),
            (PermissionError(13, "Access is denied"), "o PC não conseguiu fechar o programa"),
        ]
        for excecao, mensagem in casos:
            with self.subTest(mensagem=mensagem):
                self.terminal({TASKKILL: excecao})
                with self.assertRaises(sistema.ErroNoSistema) as erro:
                    sistema.fechar(["notepad.exe"])
                self.assertEqual(erro.exception.mensagem, mensagem)
                self.assertEqual(str(erro.exception), mensagem)
                self.assertTrue(erro.exception.detalhe)


class TestEnergiaWindows(BaseSistema):
    DESLIGAR = ("shutdown", "/s", "/t", "30", "/c", "A assistente de voz vai desligar o PC em 30 segundos.")

    def test_desligar_em_30_segundos(self):
        terminal = self.terminal({self.DESLIGAR: (0, "")})
        self.assertEqual(sistema.desligar(), "30 segundos")
        self.assertEqual(terminal.comandos(), [list(self.DESLIGAR)])
        self.conferir_opcoes(terminal)

    def test_desligar_com_erro(self):
        for codigo, mensagem in ((1190, "já existe um desligamento agendado"),
                                 (5, "o PC não conseguiu agendar o desligamento")):
            with self.subTest(codigo=codigo):
                self.terminal({self.DESLIGAR: (codigo, "", "Acesso negado.")})
                with self.assertRaises(sistema.ErroNoSistema) as erro:
                    sistema.desligar()
                self.assertEqual(str(erro.exception), mensagem)
                self.assertIn(f"exit code {codigo}", erro.exception.detalhe)

    def test_cancelar(self):
        cancelar = ("shutdown", "/a")
        for codigo, esperado in ((0, True), (1116, False)):
            with self.subTest(codigo=codigo):
                terminal = self.terminal({cancelar: (codigo, "")})
                self.assertIs(sistema.cancelar_desligamento(), esperado)
                self.assertEqual(terminal.comandos(), [list(cancelar)])
                self.conferir_opcoes(terminal)
        self.terminal({cancelar: (5, "", "Acesso negado.")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.cancelar_desligamento()
        self.assertEqual(str(erro.exception), "o PC não conseguiu cancelar o desligamento")

    def test_bloquear_chama_lock_workstation(self):
        self.terminal()  # nenhum comando
        user32 = mock.Mock()
        user32.LockWorkStation.return_value = 1
        with mock.patch("ctypes.WinDLL", create=True, return_value=user32) as windll:
            sistema.bloquear()
        windll.assert_called_once_with("user32", use_last_error=True)
        user32.LockWorkStation.assert_called_once_with()

    def test_bloquear_recusado(self):
        self.terminal()
        user32 = mock.Mock()
        user32.LockWorkStation.return_value = 0
        with mock.patch("ctypes.WinDLL", create=True, return_value=user32), \
                mock.patch("ctypes.get_last_error", create=True, return_value=5):
            with self.assertRaises(sistema.ErroNoSistema) as erro:
                sistema.bloquear()
        self.assertEqual(str(erro.exception), "o Windows não deixou bloquear a tela")
        self.assertIn("WinError 5", erro.exception.detalhe)


class TestVolumeWindows(BaseSistema):
    @staticmethod
    def erro_com(hresult: int) -> OSError:
        erro = OSError("[WinError] falha falsa")
        erro.winerror = hresult - 2**32  # o ctypes entrega o HRESULT com sinal
        return erro

    def test_traduz_os_erros_do_core_audio(self):
        sem_saida = sistema._erro_de_volume(self.erro_com(0x80070490))
        self.assertEqual(str(sem_saida), "o PC não tem uma saída de som ativa")
        self.assertIn("0x80070490", sem_saida.detalhe)
        outro = sistema._erro_de_volume(self.erro_com(0x88890004))  # AUDCLNT_E_DEVICE_INVALIDATED
        self.assertEqual(str(outro), "não consegui controlar o volume do PC")
        self.assertIn("0x88890004", outro.detalhe)
        self.assertEqual(str(sistema._erro_de_volume(OSError("sem código"))), "não consegui controlar o volume do PC")


@unittest.skipUnless(sys.platform == "win32", "as janelas dos aplicativos da loja só existem no Windows")
class TestMoldurasDeVerdade(unittest.TestCase):
    def test_busca_de_verdade_so_le(self):
        """Percorre as janelas reais (só lê) com um PID que não tem janela: confere as chamadas do ctypes."""
        self.assertEqual(sistema.molduras_da_loja(set()), [])
        self.assertEqual(sistema.molduras_da_loja({0}), [])


@unittest.skipUnless(sys.platform == "win32", "o Core Audio só existe no Windows")
class TestVolumeWindowsDeVerdade(unittest.TestCase):
    def test_leitura_real_do_volume(self):
        """Só lê o volume (é inofensivo): confere a vtable do Core Audio e o COM em cada modo de thread."""
        import ctypes
        try:
            nivel, mudo = sistema.volume_ler()
        except sistema.ErroNoSistema as e:
            if e.mensagem == sistema.SEM_SAIDA_DE_SOM:
                self.skipTest("este PC não tem uma saída de som ativa")
            raise
        self.assertIsInstance(nivel, int)
        self.assertTrue(0 <= nivel <= 100, nivel)
        self.assertIsInstance(mudo, bool)

        # Uma thread que já iniciou o COM (em qualquer modo) continua com ele iniciado depois da leitura:
        # CoInitializeEx de novo devolve S_FALSE (1). Com o modo STA, a leitura recebe RPC_E_CHANGED_MODE.
        ole32 = ctypes.WinDLL("ole32")  # WinDLL: devolve o HRESULT como número, sem exceção
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.restype = None
        resultados = {}

        def em_thread(modo: int) -> None:
            ole32.CoInitializeEx(None, modo)
            try:
                leitura = sistema.volume_ler()
                resultados[modo] = (leitura, ole32.CoInitializeEx(None, modo))
                ole32.CoUninitialize()
            except BaseException as e:
                resultados[modo] = e
            finally:
                ole32.CoUninitialize()

        for modo in (2, 0):  # COINIT_APARTMENTTHREADED, COINIT_MULTITHREADED
            with self.subTest(modo=modo):
                thread = threading.Thread(target=em_thread, args=(modo,))
                thread.start()
                thread.join(timeout=15)
                leitura, reinicio = resultados[modo]
                self.assertIsInstance(leitura[0], int)
                self.assertIsInstance(leitura[1], bool)
                self.assertEqual(reinicio, 1)


# GUIDs do Core Audio (mmdeviceapi.h e endpointvolume.h), escritos à mão como os índices da vtable abaixo.
CLSID_MMDEVICEENUMERATOR = uuid.UUID("bcde0395-e52f-467c-8e3d-c4579291692e").bytes_le
IID_IMMDEVICEENUMERATOR = uuid.UUID("a95664d2-9614-4f35-a746-de8db63617e6").bytes_le
IID_IAUDIOENDPOINTVOLUME = uuid.UUID("5cdf2c82-841e-4546-9722-0cf74078229a").bytes_le


def com_sinal(hresult: int) -> int:
    """O ctypes entrega o HRESULT como número com sinal: 0x80070490 vira -2147023728."""
    return hresult - 2**32


class CoreAudioFalso:
    """
    Core Audio de mentira, feito com ctypes: objetos COM cuja vtable aponta para funções Python, e um ole32
    falso. Confere índices e argumentos de cada método, inclusive SetMasterVolumeLevelScalar e SetMute, que
    os testes não podem chamar de verdade, e a liberação de tudo. Só funciona no Windows (WINFUNCTYPE).
    """

    def __init__(self, nivel: float = 0.35, mudo: bool = True, sem_saida: bool = False,
                 erro_ao_iniciar: OSError | None = None):
        import ctypes
        import types
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.nivel, self.mudo, self.sem_saida, self.erro_ao_iniciar = nivel, mudo, sem_saida, erro_ao_iniciar
        self.registro = []  # cada chamada, na ordem
        self.falhas = []  # exceções dentro das funções falsas (o ctypes só as imprimiria e seguiria)
        self._vivos = []  # funções e tabelas precisam continuar vivas enquanto o teste roda
        # Índices da vtable escritos à mão (endpointvolume.idl e mmdeviceapi.idl), e não lidos de sistema.py:
        # assim um índice errado lá faz o teste falhar. Os outros índices só registram a chamada errada.
        p, bool_ = ctypes.c_void_p, wintypes.BOOL
        self.volume = self._objeto("volume", {
            7: ((ctypes.c_float, p), self._definir),  # SetMasterVolumeLevelScalar
            9: ((p,), self._ler_nivel),  # GetMasterVolumeLevelScalar
            14: ((bool_, p), self._definir_mudo),  # SetMute
            15: ((p,), self._ler_mudo),  # GetMute
        })
        self.dispositivo = self._objeto("dispositivo", {3: ((p, wintypes.DWORD, p, p), self._ativar)})  # Activate
        self.enumerador = self._objeto("enumerador", {  # GetDefaultAudioEndpoint
            4: ((ctypes.c_uint, ctypes.c_uint, p), self._saida_padrao)})
        # As peças de ctypes (e os GUIDs) de sistema.py, com este objeto no lugar do ole32.
        self.com = types.SimpleNamespace(**{**vars(CORE_AUDIO_REAL()), "ole32": self})

    def _objeto(self, nome: str, metodos: dict) -> int:
        """Objeto COM: a 1ª palavra aponta para a vtable. O índice 2 (Release) registra a liberação."""
        ctypes = self.ctypes
        tabela = (ctypes.c_void_p * 20)()
        todos = {indice: (ctypes.c_long, (), lambda i=indice: self.registro.append(("índice errado", nome, i)) or
                          com_sinal(0x80004001))  # E_NOTIMPL
                 for indice in range(len(tabela))}
        todos[2] = (ctypes.c_ulong, (), lambda: self.registro.append(("Release", nome)))
        todos.update({indice: (ctypes.c_long, tipos, funcao) for indice, (tipos, funcao) in metodos.items()})
        for indice, (retorno, tipos, funcao) in todos.items():
            chamada = ctypes.WINFUNCTYPE(retorno, ctypes.c_void_p, *tipos)(self._protegida(funcao))
            tabela[indice] = ctypes.cast(chamada, ctypes.c_void_p)
            self._vivos.append(chamada)
        bloco = ctypes.c_void_p(ctypes.addressof(tabela))
        self._vivos += [tabela, bloco]
        return ctypes.addressof(bloco)

    def _protegida(self, funcao):
        def chamada(_this, *argumentos):
            try:
                return funcao(*argumentos) or 0
            except BaseException as e:
                self.falhas.append(e)
                return com_sinal(0x80004005)  # E_FAIL
        return chamada

    # ole32
    def CoInitializeEx(self, reservado, modo):
        self.registro.append(("CoInitializeEx", reservado, modo))
        if self.erro_ao_iniciar is not None:
            raise self.erro_ao_iniciar
        return 0

    def CoUninitialize(self):
        self.registro.append(("CoUninitialize",))

    def CoCreateInstance(self, clsid, externo, contexto, iid, saida):
        self.registro.append(("CoCreateInstance", bytes(clsid._obj) == CLSID_MMDEVICEENUMERATOR, externo, contexto,
                              bytes(iid._obj) == IID_IMMDEVICEENUMERATOR))
        saida._obj.value = self.enumerador
        return 0

    # IMMDeviceEnumerator e IMMDevice
    def _saida_padrao(self, fluxo, papel, saida):
        self.registro.append(("GetDefaultAudioEndpoint", fluxo, papel))
        if self.sem_saida:
            return com_sinal(sistema.E_NOTFOUND)
        self.ctypes.c_void_p.from_address(saida).value = self.dispositivo

    def _ativar(self, iid, contexto, parametros, saida):
        certo = self.ctypes.string_at(iid, 16) == IID_IAUDIOENDPOINTVOLUME
        self.registro.append(("Activate", certo, contexto, parametros))
        self.ctypes.c_void_p.from_address(saida).value = self.volume

    # IAudioEndpointVolume
    def _definir(self, nivel, contexto):
        self.registro.append(("SetMasterVolumeLevelScalar", round(nivel, 4), contexto))

    def _ler_nivel(self, saida):
        self.registro.append(("GetMasterVolumeLevelScalar",))
        self.ctypes.c_float.from_address(saida).value = self.nivel

    def _definir_mudo(self, mudo, contexto):
        self.registro.append(("SetMute", mudo, contexto))

    def _ler_mudo(self, saida):
        self.registro.append(("GetMute",))
        self.wintypes.BOOL.from_address(saida).value = self.mudo


ABRIR = [("CoInitializeEx", None, sistema.COINIT_MULTITHREADED),
         ("CoCreateInstance", True, None, sistema.CLSCTX_ALL, True),
         ("GetDefaultAudioEndpoint", 0, 0),  # eRender, eConsole
         ("Activate", True, sistema.CLSCTX_ALL, None)]
LIBERAR = [("Release", "volume"), ("Release", "dispositivo"), ("Release", "enumerador")]


@unittest.skipUnless(sys.platform == "win32", "o WINFUNCTYPE do ctypes só existe no Windows")
class TestCoreAudioFalso(BaseSistema):
    def usar(self, **opcoes) -> CoreAudioFalso:
        falso = CoreAudioFalso(**opcoes)
        patcher = mock.patch.object(sistema, "_core_audio", lambda: falso.com)
        patcher.start()
        self.addCleanup(patcher.stop)
        return falso

    def conferir(self, falso: CoreAudioFalso, esperado: list) -> None:
        self.assertEqual(falso.falhas, [])
        self.assertEqual(falso.registro, esperado)

    def test_ler(self):
        falso = self.usar(nivel=0.35, mudo=True)  # 0.35 em float32 é 0.3499999...: arredonda para 35
        self.assertEqual(sistema.volume_ler(), (35, True))
        self.conferir(falso, ABRIR + [("GetMasterVolumeLevelScalar",), ("GetMute",)] + LIBERAR + [("CoUninitialize",)])
        falso = self.usar(nivel=1.0, mudo=False)
        self.assertEqual(sistema.volume_ler(), (100, False))

    def test_definir_usa_o_indice_7_com_a_escala_de_0_a_1(self):
        for nivel, escala in ((35, 0.35), (0, 0.0), (100, 1.0), (150, 1.0), (-5, 0.0)):
            with self.subTest(nivel=nivel):
                falso = self.usar()
                self.assertIsNone(sistema.volume_definir(nivel))
                self.conferir(falso, ABRIR + [("SetMasterVolumeLevelScalar", escala, None)] + LIBERAR
                              + [("CoUninitialize",)])

    def test_mudo_usa_o_indice_14(self):
        for mudo, valor in ((True, 1), (False, 0)):
            with self.subTest(mudo=mudo):
                falso = self.usar()
                self.assertIsNone(sistema.volume_mudo(mudo))
                self.conferir(falso, ABRIR + [("SetMute", valor, None)] + LIBERAR + [("CoUninitialize",)])

    def test_sem_saida_de_som(self):
        falso = self.usar(sem_saida=True)
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.volume_definir(50)
        self.assertEqual(str(erro.exception), "o PC não tem uma saída de som ativa")
        self.assertIn("0x80070490", erro.exception.detalhe)
        # Só o enumerador chegou a existir; o COM é finalizado mesmo com a falha.
        self.conferir(falso, ABRIR[:3] + [("Release", "enumerador"), ("CoUninitialize",)])

    def test_com_ja_iniciado_em_outro_modo_nao_e_finalizado(self):
        import ctypes
        falso = self.usar(erro_ao_iniciar=ctypes.WinError(com_sinal(sistema.RPC_E_CHANGED_MODE)))
        self.assertEqual(sistema.volume_ler(), (35, True))
        self.conferir(falso, ABRIR + [("GetMasterVolumeLevelScalar",), ("GetMute",)] + LIBERAR)  # sem CoUninitialize

    def test_falha_ao_iniciar_o_com(self):
        import ctypes
        falso = self.usar(erro_ao_iniciar=ctypes.WinError(com_sinal(0x8007000E)))  # E_OUTOFMEMORY
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.volume_mudo(True)
        self.assertEqual(str(erro.exception), "não consegui controlar o volume do PC")
        self.assertIn("0x8007000E", erro.exception.detalhe)
        self.conferir(falso, ABRIR[:1])  # nada foi criado nem finalizado


# ---------- Linux ----------

class BaseLinux(BaseSistema):
    plataforma = "linux"

    def ferramentas(self, *disponiveis: str, pasta: str = "/usr/bin") -> mock.Mock:
        """Troca o shutil.which: só os programas em `disponiveis` existem, todos em `pasta`."""
        patcher = mock.patch.object(sistema.shutil, "which", side_effect=lambda nome, path=None:
                                    f"{pasta}/{nome}" if nome in disponiveis else None)
        which = patcher.start()
        self.addCleanup(patcher.stop)
        return which


class TestFecharLinux(BaseLinux):
    PKILL = ("pkill", "-x", "firefox")
    PGREP = ("pgrep", "-x", "firefox")

    def test_pkill_e_depois_confere_com_pgrep(self):
        terminal = self.terminal({self.PKILL: (0, ""), self.PGREP: [(0, "4242\n"), (1, "")]})
        self.assertEqual(sistema.fechar(["firefox"]), "fechou")
        self.assertEqual(terminal.comandos(), [list(self.PKILL), list(self.PGREP), list(self.PGREP)])
        self.assertEqual(self.esperas, [sistema.INTERVALO_FECHAR])
        self.conferir_opcoes(terminal)

    def test_codigo_1_do_pkill_e_desempatado_pelo_pgrep(self):
        casos = [((1, ""), "nao_aberto"),  # nada casou
                 ((0, "4242\n"), "nao_fechou")]  # existe, mas o sinal não pôde ser mandado (outro usuário)
        for pgrep, esperado in casos:
            with self.subTest(esperado=esperado):
                terminal = self.terminal({self.PKILL: (1, ""), self.PGREP: pgrep})
                self.assertEqual(sistema.fechar(["firefox"]), esperado)
                self.assertEqual(terminal.comandos(), [list(self.PKILL), list(self.PGREP)])
                self.assertEqual(self.esperas, [])

    def test_continua_aberto_e_pediu(self):
        self.terminal({self.PKILL: (0, ""), self.PGREP: (0, "4242\n")})
        self.assertEqual(sistema.fechar(["firefox"]), "pediu")
        self.assertTrue(3.5 <= sum(self.esperas) <= 4.5, self.esperas)

    def test_erro_do_pkill_e_nao_fechou(self):
        self.terminal({self.PKILL: (3, "", "pkill: fatal error")})
        self.assertEqual(sistema.fechar(["firefox"]), "nao_fechou")
        self.assertIn("pkill exit code 3", self.erros.getvalue())

    def test_pgrep_com_erro_vira_erro_em_portugues(self):
        self.terminal({self.PKILL: (0, ""), self.PGREP: (2, "", "pgrep: bad option")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.fechar(["firefox"])
        self.assertEqual(str(erro.exception), "não consegui conferir se o programa fechou")

    def test_nome_com_ponto_nao_vira_curinga(self):
        # O pkill lê o nome como expressão regular: sem escapar, "code.bin" casaria com "codexbin".
        pkill, pgrep = ("pkill", "-x", r"code\.bin"), ("pgrep", "-x", r"code\.bin")
        terminal = self.terminal({pkill: (0, ""), pgrep: (1, "")})
        self.assertEqual(sistema.fechar(["code.bin"]), "fechou")
        self.assertEqual(terminal.comandos(), [list(pkill), list(pgrep)])
        self.assertEqual(sistema._escapar("a+(1)[x]{2}|y^$"), r"a\+\(1\)\[x\]\{2\}\|y\^\$")

    def test_ate_15_caracteres_compara_o_nome_com_x(self):
        self.assertEqual(sistema._busca_exata("firefox-esr"), ["-x", "firefox-esr"])
        self.assertEqual(sistema._busca_exata("gnome-calculato"), ["-x", "gnome-calculato"])  # 15, o limite
        self.assertEqual(sistema._busca_exata("org.gnome.Calcu"), ["-x", r"org\.gnome\.Calcu"])

    def test_nome_comprido_compara_o_comando_inteiro(self):
        # O Linux guarda só 15 caracteres do nome. Com o nome inteiro, o -x nunca acharia nada ("não está aberto"
        # com o programa aberto); com o nome cortado, "gnome-calculato" também fecharia o
        # gnome-calculator-search-provider, e o fechar diria "fechou" com a calculadora que nem estava aberta.
        padrao = r"^([^ ]*/)?gnome-calculator( |$)"
        pkill, pgrep = ("pkill", "-f", padrao), ("pgrep", "-f", padrao)
        terminal = self.terminal({pkill: (0, ""), pgrep: (1, "")})
        self.assertEqual(sistema.fechar(["gnome-calculator"]), "fechou")
        self.assertEqual(terminal.comandos(), [list(pkill), list(pgrep)])
        self.conferir_opcoes(terminal)
        self.assertEqual(sistema._busca_exata("org.gnome.Calculator"),
                         ["-f", r"^([^ ]*/)?org\.gnome\.Calculator( |$)"])

    def test_padrao_do_nome_comprido_so_casa_com_o_proprio_programa(self):
        # O pgrep -f compara com a linha de comando (argumentos separados por espaço); o re do Python lê este
        # padrão do mesmo jeito que o regex estendido do pgrep.
        casos = {
            "gnome-calculator": [
                ("/usr/bin/gnome-calculator", True),
                ("gnome-calculator", True),
                ("gnome-calculator --mode=advanced", True),
                ("/app/bin/gnome-calculator --gapplication-service", True),  # Flatpak
                ("/usr/libexec/gnome-calculator-search-provider", False),  # os mesmos 15 primeiros caracteres
                ("gnome-calculator-search-provider", False),
                ("xgnome-calculator", False),
                ("/usr/bin/xgnome-calculator", False),
                ("vim /tmp/gnome-calculator", False),  # o nome só num argumento
                ("/usr/bin/python3 /opt/gnome-calculator", False),
            ],
            "org.gnome.Calculator": [
                ("/usr/bin/org.gnome.Calculator", True),
                ("/usr/bin/orgxgnomexCalculator", False),  # o ponto não vira curinga
            ],
        }
        for nome, linhas in casos.items():
            opcao, padrao = sistema._busca_exata(nome)
            self.assertEqual(opcao, "-f")
            for linha, casa in linhas:
                with self.subTest(nome=nome, linha=linha):
                    self.assertIs(re.search(padrao, linha) is not None, casa)

    def test_pkill_ausente(self):
        self.terminal({self.PKILL: FileNotFoundError(2, "No such file or directory: 'pkill'")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.fechar(["firefox"])
        self.assertEqual(str(erro.exception), "não encontrei o comando pkill neste PC")


class TestVolumeLinux(BaseLinux):
    GET_WPCTL = ("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
    GET_PACTL = ("pactl", "get-sink-volume", "@DEFAULT_SINK@")
    MUDO_PACTL = ("pactl", "get-sink-mute", "@DEFAULT_SINK@")
    SAIDA_PACTL = ("Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: 26214 /  40% / -23.88 dB\n"
                   "        balance 0.00\n")

    def test_le_com_wpctl(self):
        self.ferramentas("wpctl", "pactl")  # com os dois, vale o wpctl
        casos = [("Volume: 0.40 [MUTED]\n", (40, True)), ("Volume: 0.35\n", (35, False)),
                 ("Volume: 1.50\n", (100, False)), ("Volume: 0,40\n", (40, False)), ("Volume: 0.00\n", (0, False))]
        for saida, esperado in casos:
            with self.subTest(saida=saida):
                terminal = self.terminal({self.GET_WPCTL: (0, saida)})
                self.assertEqual(sistema.volume_ler(), esperado)
                self.conferir_opcoes(terminal)
                opcoes = terminal.chamadas[0][1]
                self.assertEqual(opcoes["env"]["LC_ALL"], "C")  # saída em inglês e com ponto decimal
                self.assertIn("PATH", opcoes["env"])  # o resto do ambiente continua (o PipeWire precisa dele)

    def test_muda_com_wpctl(self):
        self.ferramentas("wpctl")
        definir = ("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@")
        mudo = ("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@")
        terminal = self.terminal({definir + ("35%",): (0, ""), definir + ("100%",): (0, ""),
                                  definir + ("0%",): (0, ""), mudo + ("1",): (0, ""), mudo + ("0",): (0, "")})
        sistema.volume_definir(35)
        sistema.volume_definir(150)
        sistema.volume_definir(-5)
        sistema.volume_mudo(True)
        sistema.volume_mudo(False)
        self.assertEqual(terminal.comandos(), [list(definir + ("35%",)), list(definir + ("100%",)),
                                               list(definir + ("0%",)), list(mudo + ("1",)), list(mudo + ("0",))])
        self.conferir_opcoes(terminal)

    def test_le_com_pactl(self):
        self.ferramentas("pactl")
        casos = [(self.SAIDA_PACTL, "Mute: yes\n", (40, True)),
                 (self.SAIDA_PACTL, "Mute: no\n", (40, False)),
                 ("Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: 32768 /  50% / -18.06 dB\n",
                  "Mute: no\n", (50, False))]  # com balanço, vale o canal mais alto
        for volume, mudo, esperado in casos:
            with self.subTest(esperado=esperado):
                terminal = self.terminal({self.GET_PACTL: (0, volume), self.MUDO_PACTL: (0, mudo)})
                self.assertEqual(sistema.volume_ler(), esperado)
                self.assertEqual(terminal.comandos(), [list(self.GET_PACTL), list(self.MUDO_PACTL)])
                self.conferir_opcoes(terminal)

    def test_muda_com_pactl(self):
        self.ferramentas("pactl")
        definir = ("pactl", "set-sink-volume", "@DEFAULT_SINK@", "35%")
        mudo = ("pactl", "set-sink-mute", "@DEFAULT_SINK@")
        terminal = self.terminal({definir: (0, ""), mudo + ("1",): (0, ""), mudo + ("0",): (0, "")})
        sistema.volume_definir(35)
        sistema.volume_mudo(True)
        sistema.volume_mudo(False)
        self.assertEqual(terminal.comandos(), [list(definir), list(mudo + ("1",)), list(mudo + ("0",))])

    def test_sem_wpctl_nem_pactl(self):
        self.ferramentas()
        self.terminal()
        for chamada in (sistema.volume_ler, lambda: sistema.volume_definir(30), lambda: sistema.volume_mudo(True)):
            with self.assertRaises(sistema.ErroNoSistema) as erro:
                chamada()
            self.assertEqual(str(erro.exception), "não achei o wpctl nem o pactl para controlar o volume neste PC")

    def test_falha_ou_saida_estranha_viram_erro_em_portugues(self):
        self.ferramentas("wpctl")
        casos = [
            ((1, "", "Could not connect to PipeWire"), sistema.volume_ler, "não consegui ler o volume do PC"),
            ((0, "algo inesperado\n"), sistema.volume_ler, "não consegui ler o volume do PC"),
        ]
        for resposta, chamada, mensagem in casos:
            with self.subTest(resposta=resposta):
                self.terminal({self.GET_WPCTL: resposta})
                with self.assertRaises(sistema.ErroNoSistema) as erro:
                    chamada()
                self.assertEqual(str(erro.exception), mensagem)
        self.terminal({("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "30%"): (1, "", "Could not connect")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.volume_definir(30)
        self.assertEqual(str(erro.exception), "não consegui mudar o volume do PC")
        self.assertIn("Could not connect", erro.exception.detalhe)

        self.ferramentas("pactl")
        self.terminal({self.GET_PACTL: (0, "Volume: ?\n"), self.MUDO_PACTL: (0, "Mute: no\n")})
        with self.assertRaises(sistema.ErroNoSistema):
            sistema.volume_ler()


class TestEnergiaLinux(BaseLinux):
    def test_bloquear_com_loginctl(self):
        comando = ("loginctl", "lock-session")
        terminal = self.terminal({comando: (0, "")})
        self.assertIsNone(sistema.bloquear())
        self.assertEqual(terminal.comandos(), [list(comando)])
        self.conferir_opcoes(terminal)
        self.terminal({comando: (1, "", "Failed to issue method call")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.bloquear()
        self.assertEqual(str(erro.exception), "o PC não conseguiu bloquear a tela")

    def test_desligar_em_1_minuto(self):
        self.ferramentas("shutdown", pasta="/usr/sbin")
        comando = ("/usr/sbin/shutdown", "-P", "+1")
        terminal = self.terminal({comando: (0, "")})
        self.assertEqual(sistema.desligar(), "1 minuto")
        self.assertEqual(terminal.comandos(), [list(comando)])
        self.conferir_opcoes(terminal)
        self.terminal({comando: (1, "", "Access denied")})
        with self.assertRaises(sistema.ErroNoSistema) as erro:
            sistema.desligar()
        self.assertEqual(str(erro.exception), "o PC não conseguiu agendar o desligamento")

    def test_cancelar(self):
        self.ferramentas("shutdown", pasta="/usr/sbin")
        comando = ("/usr/sbin/shutdown", "-c")
        terminal = self.terminal({comando: (0, "")})
        self.assertIs(sistema.cancelar_desligamento(), True)
        self.assertEqual(terminal.comandos(), [list(comando)])
        self.terminal({comando: (1, "", "Access denied")})
        with self.assertRaises(sistema.ErroNoSistema):
            sistema.cancelar_desligamento()

    def test_procura_o_shutdown_tambem_no_sbin(self):
        # No Debian o shutdown fica em /usr/sbin, fora do PATH de um usuário comum.
        which = self.ferramentas("shutdown", pasta="/usr/sbin")
        self.terminal({("/usr/sbin/shutdown", "-P", "+1"): (0, ""), ("/usr/sbin/shutdown", "-c"): (0, "")})
        with mock.patch.dict(sistema.os.environ, {"PATH": os.pathsep.join(["/usr/local/bin", "/usr/bin", "/bin"])}):
            sistema.desligar()
            sistema.cancelar_desligamento()
        esperado = mock.call("shutdown", path=os.pathsep.join(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
                                                                "/sbin"]))
        self.assertEqual(which.call_args_list, [esperado, esperado])

    def test_sem_path_usa_as_pastas_padrao_do_sistema(self):
        which = self.ferramentas("shutdown", pasta="/usr/sbin")
        self.terminal({("/usr/sbin/shutdown", "-c"): (0, "")})
        with mock.patch.dict(sistema.os.environ):
            sistema.os.environ.pop("PATH", None)
            self.assertIs(sistema.cancelar_desligamento(), True)
        which.assert_called_once_with("shutdown", path=os.pathsep.join([os.defpath, "/usr/sbin", "/sbin"]))

    def test_sem_shutdown_explica_sem_rodar_nada(self):
        self.ferramentas()
        self.terminal()  # nenhum comando pode rodar
        for chamada in (sistema.desligar, sistema.cancelar_desligamento):
            with self.subTest(chamada=chamada.__name__), self.assertRaises(sistema.ErroNoSistema) as erro:
                chamada()
            self.assertEqual(str(erro.exception), "não encontrei o comando shutdown neste PC")
            self.assertIn("/usr/sbin", erro.exception.detalhe)

    def test_erro_ao_rodar_fala_so_o_nome_do_comando(self):
        # A mensagem vai para o usuário: "shutdown", e não "/usr/sbin/shutdown".
        self.ferramentas("shutdown", pasta="/usr/sbin")
        comando = ("/usr/sbin/shutdown", "-P", "+1")
        casos = [(subprocess.TimeoutExpired(list(comando), 10), "o comando shutdown demorou demais para responder"),
                 (FileNotFoundError(2, "No such file or directory"), "não encontrei o comando shutdown neste PC")]
        for excecao, mensagem in casos:
            with self.subTest(mensagem=mensagem):
                self.terminal({comando: excecao})
                with self.assertRaises(sistema.ErroNoSistema) as erro:
                    sistema.desligar()
                self.assertEqual(str(erro.exception), mensagem)


class TestOutroSistema(BaseSistema):
    plataforma = "darwin"

    def test_explica_o_que_ainda_nao_funciona(self):
        self.terminal()  # nenhum comando pode rodar
        casos = [
            (sistema.volume_ler, "o controle de volume ainda não funciona neste sistema"),
            (lambda: sistema.volume_definir(10), "o controle de volume ainda não funciona neste sistema"),
            (lambda: sistema.volume_mudo(True), "o controle de volume ainda não funciona neste sistema"),
            (sistema.bloquear, "bloquear a tela ainda não funciona neste sistema"),
            (sistema.desligar, "desligar o PC ainda não funciona neste sistema"),
            (sistema.cancelar_desligamento, "desligar o PC ainda não funciona neste sistema"),
        ]
        for chamada, mensagem in casos:
            with self.subTest(mensagem=mensagem), self.assertRaises(sistema.ErroNoSistema) as erro:
                chamada()
            self.assertEqual(str(erro.exception), mensagem)

    def test_fechar_usa_o_pkill_sem_cortar_o_nome(self):
        # O corte em 15 caracteres é coisa do Linux.
        comandos = ("pkill", "-x", "gnome-calculator"), ("pgrep", "-x", "gnome-calculator")
        terminal = self.terminal({comandos[0]: (1, ""), comandos[1]: (1, "")})
        self.assertEqual(sistema.fechar(["gnome-calculator"]), "nao_aberto")
        self.assertEqual(terminal.comandos(), [list(c) for c in comandos])


class TestModulo(unittest.TestCase):
    def test_importa_em_qualquer_sistema_sem_ctypes_do_windows(self):
        # O ctypes do Windows só pode aparecer dentro das funções: importar não pode quebrar no Linux.
        codigo = ("import sys; sys.path.insert(0, sys.argv[1]); import sistema; "
                  "print(sistema.PLATAFORMA == sys.platform, 'ctypes.wintypes' in sys.modules)")
        saida = subprocess.run([sys.executable, "-c", codigo, str(PASTA_AGENTE)], capture_output=True, text=True,
                               timeout=30, check=True)
        self.assertEqual(saida.stdout.split(), ["True", "False"])

    def test_erro_guarda_mensagem_e_detalhe(self):
        erro = sistema.ErroNoSistema("o PC não conseguiu fechar o programa", "taskkill exit code 1")
        self.assertEqual((str(erro), erro.mensagem, erro.detalhe),
                         ("o PC não conseguiu fechar o programa", "o PC não conseguiu fechar o programa",
                          "taskkill exit code 1"))
        self.assertEqual(sistema.ErroNoSistema("x").detalhe, "")


if __name__ == "__main__":
    unittest.main()
