"""
Testes dos scripts de apoio. Os do Termux rodam com bash no Linux, trocando pkg, python e
termux-wake-lock por programas falsos que só registram a chamada; o .bat roda só no Windows.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.auxiliares import PASTA_AGENTE, RAIZ, copiar_componente

SCRIPTS = RAIZ / "scripts"
BASH = shutil.which("bash") if sys.platform != "win32" else None  # no Windows, "bash" pode ser o do WSL


class TestFimDeLinha(unittest.TestCase):
    def test_scripts_do_termux_usam_lf(self):
        for script in SCRIPTS.glob("*.sh"):
            with self.subTest(script=script.name):
                conteudo = script.read_bytes()
                self.assertNotIn(b"\r", conteudo, "com CRLF o bash acusa $'\\r': command not found")
                self.assertTrue(conteudo.startswith(b"#!/usr/bin/env bash\n"))

    def test_bat_usa_crlf_sem_bom(self):
        conteudo = (PASTA_AGENTE / "iniciar_agente.bat").read_bytes()
        self.assertTrue(conteudo.startswith(b"@echo off\r\nchcp 65001 >nul\r\n"),
                        "sem BOM, e o chcp antes de qualquer linha com acento")
        self.assertEqual(conteudo.count(b"\n"), conteudo.count(b"\r\n"), "o cmd lê .bat com LF de forma errada")


@unittest.skipUnless(sys.platform == "win32", "o .bat só roda no Windows")
class TestIniciarAgenteBat(unittest.TestCase):
    def test_sem_config_mostra_a_mensagem_e_espera_uma_tecla(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pasta = copiar_componente(PASTA_AGENTE, Path(tmp) / "Pasta Com Espaço")
            saida = subprocess.run(
                ["cmd", "/c", str(pasta / "iniciar_agente.bat")], cwd=tmp, input=b"\r\n",
                capture_output=True, timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        texto = (saida.stdout + saida.stderr).decode("utf-8", errors="replace")
        self.assertEqual(saida.returncode, 1, texto)
        self.assertIn("Arquivo de configuração não encontrado", texto)
        self.assertIn("O agente parou. Se apareceu um erro acima", texto)


@unittest.skipUnless(BASH, "os scripts do Termux são testados com bash no Linux")
class TestScriptsDoTermux(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.raiz = Path(tmp.name) / "assistente-voz"
        shutil.copytree(SCRIPTS, self.raiz / "scripts")
        (self.raiz / "celular_servidor").mkdir()
        for arquivo in ("requirements.txt", "config_servidor.example.json"):
            shutil.copy(RAIZ / "celular_servidor" / arquivo, self.raiz / "celular_servidor")
        self.falsos = Path(tmp.name) / "falsos"
        self.falsos.mkdir()
        self.registro = Path(tmp.name) / "chamadas.log"
        self.tmpdir = Path(tmp.name) / "tmp"  # o $TMPDIR do Termux, onde fica a trava do termux-iniciar.sh
        self.tmpdir.mkdir()

    def programa_falso(self, nome: str, corpo: str = "") -> None:
        """Cria um executável que anota a chamada em chamadas.log e roda `corpo`."""
        caminho = self.falsos / nome
        caminho.write_text(f'#!/bin/sh\necho "{nome} $*" >> "{self.registro}"\n{corpo}\n', encoding="utf-8")
        caminho.chmod(caminho.stat().st_mode | stat.S_IXUSR)

    def rodar(self, script: str, **ambiente) -> subprocess.CompletedProcess:
        caminho = f"{self.falsos}:/usr/bin:/bin"  # sem o python e o pkg de verdade
        return subprocess.run([BASH, str(self.raiz / "scripts" / script)], cwd=self.raiz, capture_output=True,
                              text=True, encoding="utf-8", timeout=60,
                              env={"PATH": caminho, "TMPDIR": str(self.tmpdir), **ambiente})

    def chamadas(self) -> list[str]:
        return self.registro.read_text(encoding="utf-8").splitlines() if self.registro.exists() else []

    def test_instalar_cria_o_config_uma_vez_so(self):
        self.programa_falso("pkg")
        self.programa_falso("python")
        saida = self.rodar("termux-instalar.sh")

        self.assertEqual(saida.returncode, 0, saida.stdout + saida.stderr)
        config = self.raiz / "celular_servidor" / "config_servidor.json"
        exemplo = self.raiz / "celular_servidor" / "config_servidor.example.json"
        self.assertEqual(config.read_bytes(), exemplo.read_bytes())
        self.assertIn("Criei celular_servidor/config_servidor.json", saida.stdout)
        sem_perguntas = "-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
        self.assertEqual(self.chamadas(), [
            "pkg update",
            f"pkg upgrade {sem_perguntas}",
            f"pkg install {sem_perguntas} python python-pip git",
            f"python -m pip install -r {self.raiz}/celular_servidor/requirements.txt",
        ])

        config.write_text('{"meu": "config"}', encoding="utf-8")
        saida = self.rodar("termux-instalar.sh")
        self.assertEqual(config.read_text(encoding="utf-8"), '{"meu": "config"}')
        self.assertIn("já existe; não mexi nele", saida.stdout)

    def test_instalar_para_com_mensagem_se_um_passo_falha(self):
        self.programa_falso("pkg", "exit 100")
        saida = self.rodar("termux-instalar.sh")
        self.assertNotEqual(saida.returncode, 0)
        self.assertIn("A instalação parou com erro", saida.stdout)
        self.assertFalse((self.raiz / "celular_servidor" / "config_servidor.json").exists())

    def test_instalar_fora_do_termux(self):
        saida = self.rodar("termux-instalar.sh")
        self.assertEqual(saida.returncode, 1)
        self.assertIn("Este script é para o Termux", saida.stdout)

    def iniciar(self, codigo_do_diagnostico: int) -> subprocess.CompletedProcess:
        self.programa_falso("termux-wake-lock")
        self.programa_falso("termux-wake-unlock")
        self.programa_falso("python", f'[ "$1" = verificar.py ] && exit {codigo_do_diagnostico}\nexit 0')
        return self.rodar("termux-iniciar.sh")

    def test_iniciar_com_diagnostico_ok(self):
        saida = self.iniciar(0)
        self.assertEqual(saida.returncode, 0, saida.stdout + saida.stderr)
        self.assertEqual(self.chamadas(), ["termux-wake-lock ", "python verificar.py", "python servidor.py",
                                           "termux-wake-unlock "])

    def test_iniciar_com_problemas_sobe_mesmo_assim(self):
        saida = self.iniciar(1)
        self.assertIn("Subindo mesmo assim", saida.stdout)
        self.assertIn("python servidor.py", self.chamadas())

    def test_iniciar_nao_sobe_quando_o_config_impede(self):
        saida = self.iniciar(2)
        self.assertEqual(saida.returncode, 1)
        self.assertIn("O servidor não consegue subir assim", saida.stdout)
        self.assertNotIn("python servidor.py", self.chamadas())
        self.assertEqual(self.chamadas()[-1], "termux-wake-unlock ", "o wake lock precisa ser solto")

    def test_iniciar_nao_sobe_se_o_diagnostico_quebra_ou_e_interrompido(self):
        for codigo, mensagem in ((3, "O diagnóstico não terminou (código 3)"),
                                 (127, "O diagnóstico não terminou (código 127)"),
                                 (130, "Diagnóstico interrompido; o servidor não foi iniciado.")):
            with self.subTest(codigo=codigo):
                self.registro.unlink(missing_ok=True)
                saida = self.iniciar(codigo)
                self.assertEqual(saida.returncode, 1)
                self.assertIn(mensagem, saida.stdout)
                self.assertNotIn("python servidor.py", self.chamadas())
                self.assertFalse((self.tmpdir / "assistente-voz-servidor.pid").exists(), "a trava precisa sumir")

    def test_iniciar_sem_python(self):
        self.programa_falso("termux-wake-lock")
        saida = self.rodar("termux-iniciar.sh")
        self.assertEqual(saida.returncode, 1)
        self.assertIn("Não encontrei o Python. Rode antes: bash scripts/termux-instalar.sh", saida.stdout)
        self.assertEqual(self.chamadas(), [], "sem Python, nem mexe no wake lock")

    def test_segunda_execucao_nao_solta_o_wake_lock_da_primeira(self):
        (self.tmpdir / "assistente-voz-servidor.pid").write_text(str(os.getpid()))  # um processo vivo
        saida = self.iniciar(0)
        self.assertEqual(saida.returncode, 1)
        self.assertIn("O servidor já está rodando em outra sessão do Termux", saida.stdout)
        self.assertEqual(self.chamadas(), [])

    def test_trava_de_uma_execucao_que_morreu_nao_impede_de_subir(self):
        morto = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                               capture_output=True, text=True).stdout.strip()
        (self.tmpdir / "assistente-voz-servidor.pid").write_text(morto)
        saida = self.iniciar(0)
        self.assertEqual(saida.returncode, 0, saida.stdout + saida.stderr)
        self.assertIn("python servidor.py", self.chamadas())


if __name__ == "__main__":
    unittest.main()
