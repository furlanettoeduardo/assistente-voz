"""
Testes do Spotify (celular_servidor/spotify.py e spotify_conectar.py). O Spotify é um servidor falso em
127.0.0.1, com as contas e a API no mesmo servidor, separadas pelo caminho, e o arquivo de token fica numa
pasta temporária: nenhum pedido vai para o Spotify de verdade e nenhum navegador é aberto.
"""
import contextlib
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.auxiliares import PASTA_SERVIDOR, ServidorLocal, copiar_componente, importar_copia

try:
    import requests
    DEPENDENCIAS = True
except ImportError:
    DEPENDENCIAS = False

if DEPENDENCIAS:
    conectar = importar_copia(PASTA_SERVIDOR, "spotify_conectar")
    spotify = conectar.spotify  # o mesmo módulo que o spotify_conectar usa, com as mesmas classes de erro

PRECISA_DEPENDENCIAS = unittest.skipUnless(
    DEPENDENCIAS, "instale celular_servidor/requirements.txt para testar o Spotify")

AGORA = 1_800_000_000.0
CLIENT_ID = "client-id-de-teste"
VERIFIER_DO_RFC = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636, apêndice B
CHALLENGE_DO_RFC = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

FAIXA = {"type": "track", "name": "Tempo Perdido", "uri": "spotify:track:tempo",
         "artists": [{"name": "Legião Urbana", "uri": "spotify:artist:legiao"}],
         "album": {"name": "Dois", "uri": "spotify:album:dois"}}
ARTISTA = {"type": "artist", "name": "Legião Urbana", "uri": "spotify:artist:legiao"}
ALBUM = {"type": "album", "name": "Dois", "uri": "spotify:album:dois", "artists": [{"name": "Legião Urbana"}]}
PLAYLIST_DA_BUSCA = {"type": "playlist", "name": "Rock Nacional Anos 80", "uri": "spotify:playlist:busca"}

PC = {"id": "pc-1", "is_active": False, "is_restricted": False, "name": "NOTEBOOK", "type": "Computer",
      "volume_percent": 50}
ESCRITORIO = {**PC, "id": "pc-2", "name": "Escritório"}
PC_RESTRITO = {**PC, "id": "pc-restrito", "is_restricted": True}
CELULAR = {**PC, "id": "celular-1", "name": "Galaxy", "type": "Smartphone"}
TV = {**PC, "id": "tv-1", "name": "Sala", "type": "TV"}

SEM_APARELHO = (404, {"error": {"status": 404, "message": "Player command failed: No active device found",
                                "reason": "NO_ACTIVE_DEVICE"}})
PAGINA_DE_ERRO = b"<html><body>Bad gateway</body></html>"
TROCA_CERTA = (200, {"access_token": "access-da-troca", "token_type": "Bearer", "expires_in": 3600,
                     "refresh_token": "refresh-da-troca", "scope": "user-read-private"})


def resultado_da_busca(tipo: str, *itens) -> dict:
    return {f"{tipo}s": {"href": "x", "items": list(itens), "limit": 5, "total": len(itens)}}


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SpotifyFalso:
    """
    Imita as contas (/contas/api/token) e a API (/v1/...) do Spotify. Cada rota responde, em ordem, o que
    foi programado com `programar`; depois, o padrão da rota. Os pedidos ficam em `pedidos`.
    """

    PADROES = {
        ("POST", "/contas/api/token"): (200, {"access_token": "access-novo", "token_type": "Bearer",
                                              "expires_in": 3600, "scope": "user-read-private"}),
        ("PUT", "/v1/me/player/play"): (204, None),
        ("PUT", "/v1/me/player/pause"): (204, None),
        ("POST", "/v1/me/player/next"): (204, None),
        ("POST", "/v1/me/player/previous"): (204, None),
        ("GET", "/v1/me/playlists"): (200, {"items": [], "total": 0}),
        ("GET", "/v1/me/player/devices"): (200, {"devices": [PC]}),
    }

    def __init__(self):
        self._trava = threading.Lock()
        self.zerar()
        self._servidor = ServidorLocal(self._criar_handler())
        self.contas_url = f"{self._servidor.url}/contas"
        self.api_url = f"{self._servidor.url}/v1"

    def zerar(self) -> None:
        with self._trava:
            self.filas: dict[tuple[str, str], list] = {}
            self.pedidos: list[dict] = []
            self.atraso_do_token = 0.0

    def programar(self, metodo: str, caminho: str, *respostas) -> None:
        """Cada resposta: (status, corpo) ou (status, corpo, cabeçalhos). Corpo None = sem corpo; bytes = cru."""
        with self._trava:
            self.filas.setdefault((metodo, caminho), []).extend(respostas)

    def feitos(self, metodo: str | None = None, caminho: str | None = None) -> list[dict]:
        with self._trava:
            return [p for p in self.pedidos
                    if (metodo is None or p["metodo"] == metodo) and (caminho is None or p["caminho"] == caminho)]

    def parar(self) -> None:
        self._servidor.parar()

    def _criar_handler(self):
        falso = self

        class Handler(BaseHTTPRequestHandler):
            def _atender(self, metodo: str) -> None:
                partes = urlsplit(self.path)
                corpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                pedido = {"metodo": metodo, "caminho": partes.path, "cabecalhos": dict(self.headers),
                          "query": {k: v[0] for k, v in parse_qs(partes.query).items()}, "corpo": corpo,
                          "form": {k: v[0] for k, v in parse_qs(corpo.decode("utf-8")).items()}}
                try:
                    pedido["json"] = json.loads(corpo) if corpo else None
                except ValueError:
                    pedido["json"] = None
                with falso._trava:
                    falso.pedidos.append(pedido)
                    fila = falso.filas.get((metodo, partes.path))
                    resposta = fila.pop(0) if fila else falso.PADROES.get(
                        (metodo, partes.path), (404, {"error": {"status": 404, "message": "Service not found"}}))
                    atraso = falso.atraso_do_token if partes.path == "/contas/api/token" else 0
                if atraso:
                    time.sleep(atraso)
                status, dados, cabecalhos = (*resposta, {})[:3]
                if dados is None:
                    conteudo, tipo = b"", None
                elif isinstance(dados, bytes):
                    conteudo, tipo = dados, "text/html"
                else:
                    conteudo, tipo = json.dumps(dados, ensure_ascii=False).encode("utf-8"), "application/json"
                self.send_response(status)
                if tipo:
                    self.send_header("Content-Type", tipo)
                for nome, valor in cabecalhos.items():
                    self.send_header(nome, valor)
                self.send_header("Content-Length", str(len(conteudo)))
                self.end_headers()
                self.wfile.write(conteudo)

            def do_GET(self):
                self._atender("GET")

            def do_PUT(self):
                self._atender("PUT")

            def do_POST(self):
                self._atender("POST")

            def log_message(self, formato, *args):
                pass

        return Handler


class SessaoQueAnota:
    """Passa os pedidos para o requests e guarda os argumentos, para conferir o timeout."""

    def __init__(self, falha: Exception | None = None):
        self.falha = falha
        self.chamadas = []

    def request(self, metodo, url, **kwargs):
        self.chamadas.append((metodo, url, kwargs))
        if self.falha:
            raise self.falha
        return requests.request(metodo, url, **kwargs)


@PRECISA_DEPENDENCIAS
class BaseSpotify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sem_proxy = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost",
                                                     "no_proxy": "127.0.0.1,localhost"})
        cls.sem_proxy.start()
        cls.falso = SpotifyFalso()

    @classmethod
    def tearDownClass(cls):
        cls.falso.parar()
        cls.sem_proxy.stop()

    def setUp(self):
        self.falso.zerar()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.pasta = Path(tmp.name)
        self.arquivo = self.pasta / "spotify_token.json"
        self.esperas = []

    def gravar(self, access="access-valido", expira_em=AGORA + 3600, refresh="refresh-1") -> None:
        token = {"refresh_token": refresh, "access_token": access, "expira_em": expira_em}
        self.arquivo.write_text(json.dumps(token), encoding="utf-8")

    def lido(self) -> dict:
        return json.loads(self.arquivo.read_text(encoding="utf-8"))

    def cliente(self, dispositivo=None, **extra):
        return spotify.Spotify(CLIENT_ID, self.arquivo, dispositivo, relogio=lambda: AGORA,
                               contas_url=self.falso.contas_url, api_url=self.falso.api_url,
                               dormir=self.esperas.append, **extra)

    def erro(self, funcao, *args) -> "spotify.ErroNoSpotify":
        with self.assertRaises(spotify.ErroNoSpotify) as contexto:
            funcao(*args)
        return contexto.exception

    def plays(self) -> list[dict]:
        return self.falso.feitos("PUT", "/v1/me/player/play")

    def listas(self) -> list[dict]:
        """Os pedidos da lista de aparelhos."""
        return self.falso.feitos("GET", "/v1/me/player/devices")


class TestToken(BaseSpotify):
    def test_conectado_exige_arquivo_com_refresh_token(self):
        self.assertFalse(self.cliente().conectado())
        self.arquivo.write_text('{"access_token": "a"}', encoding="utf-8")
        self.assertFalse(self.cliente().conectado())
        self.arquivo.write_text("{ quebrado", encoding="utf-8")
        self.assertFalse(self.cliente().conectado())
        self.arquivo.write_text("[" * 100_000, encoding="utf-8")  # aninhado demais: RecursionError no json
        self.assertFalse(self.cliente().conectado())
        self.gravar()
        self.assertTrue(self.cliente().conectado())

    def test_arquivo_com_bom_vale(self):
        token = {"refresh_token": "refresh-1", "access_token": "access-valido", "expira_em": AGORA + 3600}
        self.arquivo.write_text(json.dumps(token), encoding="utf-8-sig")  # como o Bloco de Notas salva
        self.assertTrue(self.cliente().conectado())
        self.assertEqual(self.cliente().controlar("pausar"), "pausei a música")
        self.assertEqual(self.falso.feitos("POST", "/contas/api/token"), [])

    def test_validade_invalida_vale_uma_hora(self):
        for validade in (None, "3600", True, 0, -5, float("inf"), float("nan")):
            with self.subTest(validade=validade):
                token = spotify.token_de_resposta({"access_token": "a", "expires_in": validade}, AGORA)
                self.assertEqual(token["expira_em"], AGORA + 3600)
        self.assertEqual(spotify.token_de_resposta({"access_token": "a", "expires_in": 60}, AGORA)["expira_em"],
                         AGORA + 60)

    def test_sem_arquivo_orienta_a_conectar(self):
        erro = self.erro(self.cliente().tocar, "tempo perdido")
        self.assertEqual(erro.codigo, "autorizacao")
        self.assertEqual(str(erro), "o Spotify ainda não foi conectado: na pasta celular_servidor, rode "
                                    "python spotify_conectar.py")
        self.assertEqual(self.falso.feitos(), [])

    def test_arquivo_estragado_orienta_a_conectar_de_novo(self):
        self.arquivo.write_text("não é json", encoding="utf-8")
        erro = self.erro(self.cliente().controlar, "pausar")
        self.assertEqual(erro.codigo, "autorizacao")
        self.assertIn("python spotify_conectar.py de novo", str(erro))

    def test_token_valido_vai_no_cabecalho_sem_renovar(self):
        self.gravar()
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.cliente().tocar("tempo perdido")
        self.assertEqual(self.falso.feitos("POST", "/contas/api/token"), [])
        for pedido in self.falso.feitos():
            self.assertEqual(pedido["cabecalhos"]["Authorization"], "Bearer access-valido")

    def test_renova_quando_falta_menos_de_um_minuto_e_grava(self):
        self.gravar(expira_em=AGORA + 59)
        self.assertEqual(self.cliente().controlar("pausar"), "pausei a música")
        renovacoes = self.falso.feitos("POST", "/contas/api/token")
        self.assertEqual(len(renovacoes), 1)
        self.assertEqual(renovacoes[0]["form"], {"grant_type": "refresh_token", "refresh_token": "refresh-1",
                                                 "client_id": CLIENT_ID})
        self.assertEqual(renovacoes[0]["cabecalhos"]["Content-Type"], "application/x-www-form-urlencoded")
        # Sem refresh token na resposta, continua valendo o anterior.
        self.assertEqual(self.lido(), {"refresh_token": "refresh-1", "access_token": "access-novo",
                                       "expira_em": AGORA + 3600})
        self.assertEqual(self.falso.feitos("PUT")[0]["cabecalhos"]["Authorization"], "Bearer access-novo")

    def test_com_um_minuto_ou_mais_nao_renova(self):
        self.gravar(expira_em=AGORA + 60)
        self.cliente().controlar("pausar")
        self.assertEqual(self.falso.feitos("POST", "/contas/api/token"), [])

    def test_sem_prazo_ou_sem_access_token_renova(self):
        for token in ({"refresh_token": "refresh-1"}, {"refresh_token": "refresh-1", "access_token": "a",
                                                       "expira_em": "amanhã"}):
            with self.subTest(token=token):
                self.falso.zerar()
                self.arquivo.write_text(json.dumps(token), encoding="utf-8")
                self.cliente().controlar("pausar")
                self.assertEqual(len(self.falso.feitos("POST", "/contas/api/token")), 1)
                self.assertEqual(self.lido()["access_token"], "access-novo")

    def test_refresh_token_novo_e_gravado(self):
        self.gravar(expira_em=AGORA - 10)
        self.falso.programar("POST", "/contas/api/token", (200, {
            "access_token": "access-2", "token_type": "Bearer", "expires_in": 1800, "refresh_token": "refresh-2"}))
        self.cliente().controlar("proxima")
        self.assertEqual(self.lido(), {"refresh_token": "refresh-2", "access_token": "access-2",
                                       "expira_em": AGORA + 1800})

    def test_gravacao_atomica(self):
        self.gravar(expira_em=AGORA)
        with mock.patch.object(spotify.os, "replace", wraps=os.replace) as trocar:
            self.cliente().controlar("pausar")
        trocar.assert_called_once()
        temporario, destino = trocar.call_args.args
        self.assertEqual(Path(temporario).parent, self.pasta)  # na mesma pasta, para o replace ser atômico
        self.assertEqual(Path(destino), self.arquivo)
        self.assertEqual(sorted(self.pasta.iterdir()), [self.arquivo])  # o temporário não sobra

    def test_falha_ao_gravar_mantem_o_arquivo_antigo(self):
        self.gravar(expira_em=AGORA)
        antes = self.arquivo.read_text(encoding="utf-8")
        with mock.patch.object(spotify.os, "replace", side_effect=PermissionError("arquivo em uso")):
            erro = self.erro(self.cliente().controlar, "pausar")
        self.assertIn("não consegui gravar o arquivo spotify_token.json", str(erro))
        self.assertEqual(self.arquivo.read_text(encoding="utf-8"), antes)
        self.assertEqual(sorted(self.pasta.iterdir()), [self.arquivo])

    @unittest.skipIf(sys.platform == "win32", "permissões de arquivo no estilo Unix")
    def test_arquivo_gravado_so_para_o_dono(self):
        spotify.gravar_token(self.arquivo, {"refresh_token": "r", "access_token": "a", "expira_em": 1})
        self.assertEqual(stat.S_IMODE(self.arquivo.stat().st_mode), 0o600)

    def test_invalid_grant_pede_para_conectar_de_novo(self):
        self.gravar(expira_em=AGORA)
        antes = self.arquivo.read_text(encoding="utf-8")
        self.falso.programar("POST", "/contas/api/token",
                             (400, {"error": "invalid_grant", "error_description": "Refresh token revoked"}))
        erro = self.erro(self.cliente().tocar, "tempo perdido")
        self.assertEqual(erro.codigo, "autorizacao")
        self.assertEqual(str(erro), "a autorização do Spotify venceu (ela dura 6 meses): na pasta celular_servidor, "
                                    "rode python spotify_conectar.py de novo")
        self.assertIn("invalid_grant", erro.detalhe)
        self.assertEqual(self.arquivo.read_text(encoding="utf-8"), antes)
        self.assertEqual(self.falso.feitos("GET"), [])

    def test_outras_falhas_da_renovacao(self):
        casos = [((400, {"error": "invalid_client", "error_description": "Invalid client"}), "autorizacao",
                  "não reconheceu"),
                 ((502, PAGINA_DE_ERRO), "outro", "problemas"),  # corpo que não é JSON
                 ((200, PAGINA_DE_ERRO), "outro", "não entendi"),
                 ((429, {"error": "rate_limited"}), "limite", "esperar")]
        for resposta, codigo, trecho in casos:
            with self.subTest(resposta=resposta):
                self.falso.zerar()
                self.gravar(expira_em=AGORA)
                self.falso.programar("POST", "/contas/api/token", resposta)
                erro = self.erro(self.cliente().controlar, "pausar")
                self.assertEqual(erro.codigo, codigo)
                self.assertIn(trecho, str(erro))

    def test_resposta_estranha_da_renovacao_nao_mostra_o_token_no_terminal(self):
        self.gravar(expira_em=AGORA)
        self.falso.programar("POST", "/contas/api/token", (200, {"refresh_token": "segredo-novo", "expires_in": 3600}))
        erro = self.erro(self.cliente().controlar, "pausar")
        self.assertIn("não entendi", str(erro))
        self.assertIn("refresh_token", erro.detalhe)  # o nome do campo ajuda a entender; o valor, não
        self.assertNotIn("segredo-novo", erro.detalhe)

    def test_401_renova_uma_vez_e_repete(self):
        self.gravar()
        recusado = (401, {"error": {"status": 401, "message": "The access token expired"}})
        self.falso.programar("GET", "/v1/search", recusado, (200, resultado_da_busca("artist", ARTISTA)))
        self.assertEqual(self.cliente().tocar("legião", "artista"), "coloquei Legião Urbana para tocar")
        buscas = self.falso.feitos("GET", "/v1/search")
        self.assertEqual([b["cabecalhos"]["Authorization"] for b in buscas],
                         ["Bearer access-valido", "Bearer access-novo"])
        self.assertEqual(len(self.falso.feitos("POST", "/contas/api/token")), 1)
        self.assertEqual(self.lido()["access_token"], "access-novo")

    def test_401_que_persiste_vira_erro_de_autorizacao(self):
        self.gravar()
        recusado = (401, {"error": {"status": 401, "message": "Invalid access token"}})
        self.falso.programar("POST", "/v1/me/player/next", recusado, recusado, recusado)
        erro = self.erro(self.cliente().controlar, "proxima")
        self.assertEqual(erro.codigo, "autorizacao")
        self.assertIn("python spotify_conectar.py de novo", str(erro))
        self.assertEqual(len(self.falso.feitos("POST", "/v1/me/player/next")), 2)
        self.assertEqual(len(self.falso.feitos("POST", "/contas/api/token")), 1)

    def test_401_depois_de_outra_thread_renovar_usa_o_token_novo(self):
        self.gravar()
        self.falso.programar("POST", "/v1/me/player/next", (401, {"error": {"status": 401, "message": "expired"}}))
        teste = self

        class OutraThreadRenova(SessaoQueAnota):
            def request(self, metodo, url, **kwargs):
                resposta = super().request(metodo, url, **kwargs)
                if resposta.status_code == 401:  # enquanto o 401 volta, outra thread grava um token novo
                    teste.gravar(access="access-da-outra-thread")
                return resposta

        self.assertEqual(self.cliente(sessao=OutraThreadRenova()).controlar("proxima"), "pulei para a próxima")
        self.assertEqual(self.falso.feitos("POST", "/contas/api/token"), [])
        self.assertEqual([p["cabecalhos"]["Authorization"] for p in self.falso.feitos("POST", "/v1/me/player/next")],
                         ["Bearer access-valido", "Bearer access-da-outra-thread"])

    def test_renovacao_ao_mesmo_tempo_faz_um_pedido_so(self):
        self.gravar(expira_em=AGORA)
        self.falso.atraso_do_token = 0.3
        cliente, resultados = self.cliente(), []
        threads = [threading.Thread(target=lambda: resultados.append(cliente.controlar("pausar")))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(resultados, ["pausei a música"] * 4)
        self.assertEqual(len(self.falso.feitos("POST", "/contas/api/token")), 1)


class TestTocar(BaseSpotify):
    def setUp(self):
        super().setUp()
        self.gravar()

    def test_faixa_toca_no_album_a_partir_dela(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.assertEqual(self.cliente().tocar("tempo perdido legião"), 'coloquei "Tempo Perdido", de Legião Urbana')
        busca = self.falso.feitos("GET", "/v1/search")[0]
        self.assertEqual(busca["query"], {"q": "tempo perdido legião", "type": "track", "limit": "5"})
        play = self.plays()[0]
        self.assertEqual(play["json"], {"context_uri": "spotify:album:dois", "offset": {"uri": "spotify:track:tempo"}})
        self.assertEqual(play["query"], {"device_id": "pc-1"})
        self.assertEqual(play["cabecalhos"]["Content-Type"], "application/json")

    def test_faixa_sem_album_toca_so_a_faixa(self):
        faixa = {k: v for k, v in FAIXA.items() if k != "album"}
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", faixa)))
        self.cliente().tocar("tempo perdido", "musica")
        self.assertEqual(self.plays()[0]["json"], {"uris": ["spotify:track:tempo"]})

    def test_varios_artistas(self):
        for nomes, esperado in ((["A", "B"], 'coloquei "X", de A e B'), (["A", "B", "C"], 'coloquei "X", de A, B e C'),
                                ([], 'coloquei "X"')):
            with self.subTest(nomes=nomes):
                faixa = {**FAIXA, "name": "X", "artists": [{"name": n} for n in nomes]}
                self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", faixa)))
                self.assertEqual(self.cliente().tocar("x"), esperado)

    def test_artista_sem_offset(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("artist", ARTISTA)))
        self.assertEqual(self.cliente().tocar("legião urbana", "artista"), "coloquei Legião Urbana para tocar")
        self.assertEqual(self.falso.feitos("GET", "/v1/search")[0]["query"]["type"], "artist")
        self.assertEqual(self.plays()[0]["json"], {"context_uri": "spotify:artist:legiao"})

    def test_album(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("album", ALBUM)))
        self.assertEqual(self.cliente().tocar("dois", "álbum"), 'coloquei o álbum "Dois", de Legião Urbana')
        self.assertEqual(self.plays()[0]["json"], {"context_uri": "spotify:album:dois"})

    def test_tipo_invalido_ou_busca_vazia(self):
        erro = self.erro(self.cliente().tocar, "algo", "podcast")
        self.assertEqual(erro.codigo, "outro")
        self.assertIn("música, artista, álbum ou playlist", str(erro))
        self.erro(self.cliente().tocar, "   ", "musica")
        self.assertEqual(self.falso.feitos(), [])

    def test_playlist_achada_nas_playlists_da_conta(self):
        self.falso.programar("GET", "/v1/me/playlists", (200, {"items": [
            None, {"name": "Sertanejo", "uri": "spotify:playlist:sertanejo"},
            {"name": "Músicas de Domingo", "uri": "spotify:playlist:domingo"}], "total": 3}))
        self.assertEqual(self.cliente().tocar("musicas de DOMINGO", "playlist"),
                         'coloquei a playlist "Músicas de Domingo"')
        self.assertEqual(self.falso.feitos("GET", "/v1/me/playlists")[0]["query"], {"limit": "50"})
        self.assertEqual(self.falso.feitos("GET", "/v1/search"), [])
        self.assertEqual(self.plays()[0]["json"], {"context_uri": "spotify:playlist:domingo"})

    def playlist_escolhida(self, pedido: str, *nomes: str) -> str:
        """Toca `pedido` com estas playlists na conta; devolve o nome da que tocou (a da busca, se nenhuma)."""
        self.falso.zerar()
        itens = [{"name": nome, "uri": f"spotify:playlist:conta-{i}"} for i, nome in enumerate(nomes)]
        self.falso.programar("GET", "/v1/me/playlists", (200, {"items": itens}))
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("playlist", PLAYLIST_DA_BUSCA)))
        self.cliente().tocar(pedido, "playlist")
        tocada = self.plays()[0]["json"]["context_uri"]
        return next((i["name"] for i in itens if i["uri"] == tocada), PLAYLIST_DA_BUSCA["name"])

    def test_playlist_que_contem_o_pedido_em_palavras_inteiras(self):
        casos = [("rock nacional", ["Rock Nacional 2024"], "Rock Nacional 2024"),
                 ("funk", ["Punk", "Funk Brasil"], "Funk Brasil"),
                 ("anos 80", ["Rock/Anos 80"], "Rock/Anos 80")]  # a pontuação separa as palavras
        for pedido, nomes, esperado in casos:
            with self.subTest(pedido=pedido):
                self.assertEqual(self.playlist_escolhida(pedido, *nomes), esperado)
                self.assertEqual(self.falso.feitos("GET", "/v1/search"), [])

    def test_pedido_que_contem_o_nome_inteiro_da_playlist(self):
        self.assertEqual(self.playlist_escolhida("a minha de rock nacional", "Sertanejo", "Rock Nacional"),
                         "Rock Nacional")
        # Nome de uma palavra só não vale: "rock" está no pedido, mas "Rock" é outra playlist.
        self.assertEqual(self.playlist_escolhida("rock nacional anos 80", "Rock"), "Rock Nacional Anos 80")
        self.assertEqual(self.falso.feitos("GET", "/v1/search")[0]["query"]["q"], "rock nacional anos 80")

    def test_playlist_com_erro_de_digitacao_no_pedido(self):
        self.assertEqual(self.playlist_escolhida("rock nacionl", "Sertanejo", "Rock Nacional"), "Rock Nacional")
        self.assertEqual(self.playlist_escolhida("musica de domingu", "Músicas de Domingo"), "Músicas de Domingo")

    def test_playlist_com_outra_palavra_de_ligacao(self):
        # "do" no lugar de "de" e "e" no lugar de "&" não mudam a playlist.
        self.assertEqual(self.playlist_escolhida("musicas do domingo", "Músicas de Domingo"), "Músicas de Domingo")
        self.assertEqual(self.playlist_escolhida("rock e blues", "Rock & Blues"), "Rock & Blues")
        self.assertEqual(self.falso.feitos("GET", "/v1/search"), [])

    def test_ordem_dos_criterios(self):
        # O nome igual ganha de quem o contém; quem contém o pedido ganha do parecido.
        self.assertEqual(self.playlist_escolhida("rock", "Rock Nacional", "Rock"), "Rock")
        self.assertEqual(self.playlist_escolhida("sertanejo", "Sertaneja", "Sertanejo Raiz"), "Sertanejo Raiz")

    def test_palavra_parecida_nao_troca_a_playlist(self):
        casos = [("funk", "Punk"), ("trap", "Rap"), ("reggae", "Reggaeton"), ("reggaeton", "Reggae"),
                 ("metal", "Mental"), ("funk brasil", "Punk Brasil"), ("rock anos 80", "Rock Anos 90"),
                 # O nome todo passa de 0.8, mas uma palavra do pedido não está nele.
                 ("rock internacional", "Rock Nacional"), ("rock nacional", "Rock Internacional"),
                 ("musica internacional", "Música Nacional"), ("musica com letra", "Música sem Letra")]
        for pedido, nome in casos:
            with self.subTest(pedido=pedido, nome=nome):
                self.assertEqual(self.playlist_escolhida(pedido, nome), "Rock Nacional Anos 80")  # a da busca
                busca, = self.falso.feitos("GET", "/v1/search")
                self.assertEqual((busca["query"]["q"], busca["query"]["type"]), (pedido, "playlist"))

    def test_playlist_fora_da_conta_usa_a_busca(self):
        self.falso.programar("GET", "/v1/me/playlists", (200, {"items": [
            {"name": "Sertanejo", "uri": "spotify:playlist:sertanejo"}]}))
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("playlist", None, PLAYLIST_DA_BUSCA)))
        self.assertEqual(self.cliente().tocar("rock nacional anos 80", "playlist"),
                         'coloquei a playlist "Rock Nacional Anos 80"')
        self.assertEqual(self.falso.feitos("GET", "/v1/search")[0]["query"]["type"], "playlist")
        self.assertEqual(self.plays()[0]["json"], {"context_uri": "spotify:playlist:busca"})

    def test_nada_encontrado(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track")))
        erro = self.erro(self.cliente().tocar, "xyzzy qwerty")
        self.assertEqual(erro.codigo, "nao_encontrado")
        self.assertEqual(str(erro), 'não encontrei "xyzzy qwerty" no Spotify')
        self.assertEqual(self.plays(), [])

    def test_itens_null_sao_descartados(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", None, FAIXA)),
                             (200, resultado_da_busca("track", None, None)))
        self.assertEqual(self.cliente().tocar("tempo perdido"), 'coloquei "Tempo Perdido", de Legião Urbana')
        self.assertEqual(self.erro(self.cliente().tocar, "tempo perdido").codigo, "nao_encontrado")

    def test_escolhe_o_computador_antes_de_tocar(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [CELULAR, PC_RESTRITO, TV, PC]}))
        self.assertEqual(self.cliente().tocar("tempo perdido"), 'coloquei "Tempo Perdido", de Legião Urbana')
        self.assertEqual([(p["metodo"], p["caminho"]) for p in self.falso.feitos()],
                         [("GET", "/v1/search"), ("GET", "/v1/me/player/devices"), ("PUT", "/v1/me/player/play")])
        self.assertEqual(self.plays()[0]["query"], {"device_id": "pc-1"})

    def test_com_o_celular_ativo_nenhum_comando_sai_sem_o_device_id_do_pc(self):
        # Um comando sem device_id iria para o aparelho ativo: aqui, o celular.
        celular_tocando = {**CELULAR, "is_active": True}
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("GET", "/v1/me/player/devices", *[(200, {"devices": [celular_tocando, PC]})] * 5)
        cliente = self.cliente()
        cliente.tocar("tempo perdido")
        for acao in spotify.ACOES:
            cliente.controlar(acao)
        comandos = [p for p in self.falso.feitos() if p["caminho"].startswith("/v1/me/player/")
                    and p["caminho"] != "/v1/me/player/devices"]
        self.assertEqual([p["caminho"] for p in comandos],
                         ["/v1/me/player/play", "/v1/me/player/pause", "/v1/me/player/play", "/v1/me/player/next",
                          "/v1/me/player/previous"])
        self.assertEqual([p["query"] for p in comandos], [{"device_id": "pc-1"}] * 5)
        self.assertEqual(len(self.listas()), 5)

    def test_escolhe_o_de_nome_configurado(self):
        for dispositivo in ("escritorio", "  ESCRITÓRIO "):
            with self.subTest(dispositivo=dispositivo):
                self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("artist", ARTISTA)))
                self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [PC, ESCRITORIO]}))
                self.cliente(dispositivo).tocar("legião", "artista")
                self.assertEqual(self.plays()[-1]["query"], {"device_id": "pc-2"})

    def test_nome_configurado_ausente_nao_usa_outro_computador(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("artist", ARTISTA)))
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [CELULAR, PC, ESCRITORIO]}))
        erro = self.erro(self.cliente("Desktop da Sala").tocar, "legião", "artista")
        self.assertEqual((erro.codigo, str(erro)),
                         ("sem_dispositivo", "o Spotify não está aberto no PC: abra o aplicativo e peça de novo"))
        self.assertIn('procurei o de nome "desktop da sala"', erro.detalhe)
        self.assertEqual(self.plays(), [])

    def test_sem_computador_nao_toca_em_outro_aparelho(self):
        for aparelhos in ([CELULAR, TV, PC_RESTRITO], [], [{**PC, "id": None}]):
            with self.subTest(aparelhos=aparelhos):
                self.falso.zerar()
                self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
                self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": aparelhos}))
                erro = self.erro(self.cliente().tocar, "tempo perdido")
                self.assertEqual(erro.codigo, "sem_dispositivo")
                self.assertEqual(str(erro), "o Spotify não está aberto no PC: abra o aplicativo e peça de novo")
                self.assertIn("procurei um computador", erro.detalhe)
                self.assertEqual(self.plays(), [])  # nenhum comando sai
                self.assertEqual(len(self.listas()), 1)

    def test_computador_que_some_depois_da_lista_busca_a_lista_de_novo(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("PUT", "/v1/me/player/play", SEM_APARELHO)
        # O Spotify do PC reabriu e voltou com outro id.
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [PC]}), (200, {"devices": [ESCRITORIO]}))
        self.assertEqual(self.cliente().tocar("tempo perdido"), 'coloquei "Tempo Perdido", de Legião Urbana')
        primeiro, segundo = self.plays()
        self.assertEqual((primeiro["query"], segundo["query"]), ({"device_id": "pc-1"}, {"device_id": "pc-2"}))
        self.assertEqual(segundo["json"], primeiro["json"])
        self.assertEqual(len(self.listas()), 2)

    def test_computador_que_continua_sumido_repete_uma_vez_so(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("PUT", "/v1/me/player/play", SEM_APARELHO,
                             (404, {"error": {"status": 404, "message": "Device not found"}}), SEM_APARELHO)
        erro = self.erro(self.cliente().tocar, "tempo perdido")
        self.assertEqual((erro.codigo, str(erro)),
                         ("sem_dispositivo", "o Spotify não está aberto no PC: abra o aplicativo e peça de novo"))
        self.assertEqual([p["query"] for p in self.plays()], [{"device_id": "pc-1"}] * 2)
        self.assertEqual(len(self.listas()), 2)

    def test_computador_que_sai_da_lista_nao_repete_o_comando(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("PUT", "/v1/me/player/play", SEM_APARELHO)
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [PC]}), (200, {"devices": [CELULAR]}))
        self.assertEqual(self.erro(self.cliente().tocar, "tempo perdido").codigo, "sem_dispositivo")
        self.assertEqual(len(self.plays()), 1)
        self.assertEqual(len(self.listas()), 2)

    def test_404_com_corpo_que_nao_e_json_tambem_busca_a_lista_de_novo(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("PUT", "/v1/me/player/play", (404, b"Not Found"))
        self.cliente().tocar("tempo perdido")
        self.assertEqual([p["query"] for p in self.plays()], [{"device_id": "pc-1"}] * 2)
        self.assertEqual(len(self.listas()), 2)

    def test_404_que_nao_e_falta_de_aparelho_nao_repete(self):
        for corpo in ({"error": {"status": 404, "message": "Not found."}},
                      {"error": {"status": 404, "message": "Player command failed", "reason": "UNKNOWN"}}):
            with self.subTest(corpo=corpo):
                self.falso.zerar()
                self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
                self.falso.programar("PUT", "/v1/me/player/play", (404, corpo))
                erro = self.erro(self.cliente().tocar, "tempo perdido")
                self.assertEqual(erro.codigo, "outro")
                self.assertIn("erro 404", str(erro))
                self.assertEqual(len(self.listas()), 1)
                self.assertEqual(len(self.plays()), 1)

    def test_segundo_404_que_nao_e_do_aparelho_nao_manda_abrir_o_spotify(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("album", ALBUM)))
        self.falso.programar("PUT", "/v1/me/player/play", SEM_APARELHO,
                             (404, {"error": {"status": 404, "message": "Not found."}}))
        erro = self.erro(self.cliente().tocar, "dois", "album")
        self.assertEqual(erro.codigo, "outro")
        self.assertEqual([p["query"] for p in self.plays()], [{"device_id": "pc-1"}] * 2)

    def test_403_premium(self):
        for corpo in ({"error": {"status": 403, "message": "Player command failed: Premium required",
                                 "reason": "PREMIUM_REQUIRED"}}, PAGINA_DE_ERRO):
            with self.subTest(corpo=corpo):
                self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
                self.falso.programar("PUT", "/v1/me/player/play", (403, corpo))
                erro = self.erro(self.cliente().tocar, "tempo perdido")
                self.assertEqual(erro.codigo, "premium")
                self.assertEqual(str(erro), "o Spotify recusou: a conta precisa ser Premium e estar liberada em "
                                            "Users Management no app do painel de desenvolvedor")

    def test_403_de_comando_impossivel_nao_culpa_o_premium(self):
        self.falso.programar("PUT", "/v1/me/player/pause", (403, {"error": {
            "status": 403, "message": "Player command failed: Restriction violated", "reason": "UNKNOWN"}}))
        erro = self.erro(self.cliente().controlar, "pausar")
        self.assertEqual(erro.codigo, "outro")
        self.assertIn("já está pausada", str(erro))

    def test_429_com_quota_exceeded_nao_repete(self):
        self.falso.programar("GET", "/v1/search", (429, {"error": {
            "status": 429, "message": "Quota exceeded", "reason": "QUOTA_EXCEEDED"}}, {"Retry-After": "1"}))
        erro = self.erro(self.cliente().tocar, "tempo perdido")
        self.assertEqual((erro.codigo, str(erro)), ("limite", "a cota do app do Spotify acabou por enquanto"))
        self.assertEqual(len(self.falso.feitos("GET", "/v1/search")), 1)
        self.assertEqual(self.esperas, [])

    def test_429_curto_espera_e_repete_uma_vez(self):
        muitos = (429, {"error": {"status": 429, "message": "API rate limit exceeded"}}, {"Retry-After": "2"})
        self.falso.programar("GET", "/v1/search", muitos, (200, resultado_da_busca("track", FAIXA)))
        self.cliente().tocar("tempo perdido")
        self.assertEqual(self.esperas, [2.0])
        self.assertEqual(len(self.falso.feitos("GET", "/v1/search")), 2)
        # Se insistir depois da espera, desiste.
        self.falso.zerar()
        self.esperas.clear()
        self.falso.programar("POST", "/v1/me/player/next", muitos, muitos, muitos)
        erro = self.erro(self.cliente().controlar, "proxima")
        self.assertEqual((erro.codigo, str(erro)), ("limite", "o Spotify pediu para esperar um pouco"))
        self.assertEqual((self.esperas, len(self.falso.feitos("POST", "/v1/me/player/next"))), ([2.0], 2))

    def test_429_longo_ou_sem_retry_after_nao_espera(self):
        for cabecalhos in ({"Retry-After": "30"}, {}, {"Retry-After": "amanhã"}):
            with self.subTest(cabecalhos=cabecalhos):
                self.falso.zerar()
                self.falso.programar("GET", "/v1/search", (429, PAGINA_DE_ERRO, cabecalhos))
                erro = self.erro(self.cliente().tocar, "tempo perdido")
                self.assertEqual((erro.codigo, str(erro)), ("limite", "o Spotify pediu para esperar um pouco"))
                self.assertEqual(len(self.falso.feitos("GET", "/v1/search")), 1)
                self.assertEqual(self.esperas, [])

    def test_corpo_que_nao_e_json_nao_quebra(self):
        casos = [((200, PAGINA_DE_ERRO), "outro", "não entendi"),
                 ((502, PAGINA_DE_ERRO), "outro", "problemas agora"),
                 ((400, PAGINA_DE_ERRO), "outro", "erro 400"),
                 ((200, {"tracks": None}), "nao_encontrado", "não encontrei"),
                 ((200, ["não", "é", "objeto"]), "outro", "não entendi")]
        for resposta, codigo, trecho in casos:
            with self.subTest(resposta=resposta):
                self.falso.zerar()
                self.falso.programar("GET", "/v1/search", resposta)
                erro = self.erro(self.cliente().tocar, "tempo perdido")
                self.assertEqual(erro.codigo, codigo)
                self.assertIn(trecho, str(erro))

    def test_devices_que_nao_e_json(self):
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.falso.programar("GET", "/v1/me/player/devices", (200, PAGINA_DE_ERRO))
        self.assertIn("não entendi", str(self.erro(self.cliente().tocar, "tempo perdido")))
        self.assertEqual(self.plays(), [])

    def test_erro_de_rede(self):
        sessao = SessaoQueAnota(falha=requests.ConnectionError("conexão recusada"))
        erro = self.erro(self.cliente(sessao=sessao).tocar, "tempo perdido")
        self.assertEqual((erro.codigo, str(erro)), ("rede", "não consegui falar com o Spotify"))
        self.assertIn("conexão recusada", erro.detalhe)
        self.gravar(expira_em=AGORA)  # a renovação também
        erro = self.erro(self.cliente(sessao=sessao).controlar, "pausar")
        self.assertEqual(erro.codigo, "rede")
        self.assertEqual(sessao.chamadas[-1][1], f"{self.falso.contas_url}/api/token")

    def test_timeout_de_10_segundos_em_cada_chamada(self):
        self.gravar(expira_em=AGORA)
        sessao = SessaoQueAnota()
        self.falso.programar("GET", "/v1/search", (200, resultado_da_busca("track", FAIXA)))
        self.cliente(sessao=sessao).tocar("tempo perdido")
        self.assertEqual(len(sessao.chamadas), 4)  # renovação, busca, lista de aparelhos e play
        self.assertEqual([kwargs["timeout"] for _, _, kwargs in sessao.chamadas], [10, 10, 10, 10])


class TestControlar(BaseSpotify):
    def setUp(self):
        super().setUp()
        self.gravar()

    def test_cada_acao(self):
        casos = [("pausar", "PUT", "/v1/me/player/pause", "pausei a música"),
                 ("continuar", "PUT", "/v1/me/player/play", "continuei a música"),
                 ("proxima", "POST", "/v1/me/player/next", "pulei para a próxima"),
                 ("anterior", "POST", "/v1/me/player/previous", "voltei para a anterior")]
        for acao, metodo, caminho, descricao in casos:
            with self.subTest(acao=acao):
                self.falso.zerar()
                self.assertEqual(self.cliente().controlar(acao), descricao)
                lista, pedido = self.falso.feitos()
                self.assertEqual((lista["metodo"], lista["caminho"]), ("GET", "/v1/me/player/devices"))
                self.assertEqual((pedido["metodo"], pedido["caminho"], pedido["query"]),
                                 (metodo, caminho, {"device_id": "pc-1"}))
                self.assertEqual(pedido["corpo"], b"")  # continuar: o play sem corpo retoma o que estava tocando
                self.assertEqual(pedido["cabecalhos"].get("Content-Length"), "0")
                self.assertEqual(pedido["cabecalhos"]["Authorization"], "Bearer access-valido")

    def test_acao_com_acento_e_maiusculas(self):
        self.assertEqual(self.cliente().controlar("Próxima"), "pulei para a próxima")

    def test_acao_desconhecida(self):
        erro = self.erro(self.cliente().controlar, "embaralhar")
        self.assertIn("pausar, continuar, próxima ou anterior", str(erro))
        self.assertEqual(self.falso.feitos(), [])

    def test_falta_de_aparelho_repete_com_a_lista_nova(self):
        self.falso.programar("POST", "/v1/me/player/next", SEM_APARELHO)
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [CELULAR, PC]}),
                             (200, {"devices": [CELULAR, ESCRITORIO]}))
        self.assertEqual(self.cliente().controlar("proxima"), "pulei para a próxima")
        self.assertEqual([p["query"] for p in self.falso.feitos("POST", "/v1/me/player/next")],
                         [{"device_id": "pc-1"}, {"device_id": "pc-2"}])

    def test_sem_computador_nao_controla_outro_aparelho(self):
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [{**CELULAR, "is_active": True}]}))
        self.assertEqual(self.erro(self.cliente().controlar, "pausar").codigo, "sem_dispositivo")
        self.assertEqual(self.falso.feitos("PUT", "/v1/me/player/pause"), [])

    def test_nome_configurado_ausente_nao_controla_outro_computador(self):
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [PC]}))
        self.assertEqual(self.erro(self.cliente("Escritório").controlar, "pausar").codigo, "sem_dispositivo")
        self.assertEqual(self.falso.feitos("PUT", "/v1/me/player/pause"), [])


class TestTestar(BaseSpotify):
    def test_lista_os_aparelhos_sem_tocar_nada(self):
        self.gravar(expira_em=AGORA + 10)  # vence logo: o teste também confere a renovação
        self.falso.programar("GET", "/v1/me/player/devices", (200, {"devices": [PC, CELULAR, {"id": "x"}]}))
        self.assertEqual(self.cliente().testar(), ["NOTEBOOK", "Galaxy"])
        self.assertEqual([(p["metodo"], p["caminho"]) for p in self.falso.feitos()],
                         [("POST", "/contas/api/token"), ("GET", "/v1/me/player/devices")])

    def test_premium_recusado(self):
        self.gravar()
        self.falso.programar("GET", "/v1/me/player/devices", (403, {"error": {"status": 403, "message": "x"}}))
        self.assertEqual(self.erro(self.cliente().testar).codigo, "premium")


class NavegadorFalso:
    """
    Faz o papel do navegador: em vez de abrir a página do Spotify, visita `antes` e depois vai direto ao
    /callback com a consulta que `consulta(state)` monta. Guarda cada página recebida.
    """

    def __init__(self, porta: int, consulta=lambda state: f"code=codigo-1&state={state}", antes=(), falhar=False):
        self.porta, self.consulta, self.antes, self.falhar = porta, consulta, antes, falhar
        self.urls, self.paginas = [], []
        self._abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __call__(self, url: str):
        self.urls.append(url)
        state = parse_qs(urlsplit(url).query)["state"][0]
        for caminho in self.antes:
            self.visitar(caminho)
        self.visitar(f"/callback?{self.consulta(state)}")
        if self.falhar:
            raise RuntimeError("navegador quebrado")
        return True

    def visitar(self, caminho: str) -> None:
        try:
            with self._abridor.open(f"http://127.0.0.1:{self.porta}{caminho}", timeout=10) as r:
                self.paginas.append((r.status, r.headers["Content-Type"], r.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            with e:
                self.paginas.append((e.code, e.headers["Content-Type"], e.read().decode("utf-8")))


@PRECISA_DEPENDENCIAS
class TestConectar(BaseSpotify):
    def setUp(self):
        super().setUp()
        # Nenhum teste pode abrir o navegador de verdade.
        nunca = mock.patch.object(conectar.webbrowser, "open", side_effect=AssertionError("abriu o navegador"))
        nunca.start()
        self.addCleanup(nunca.stop)
        self.config = self.pasta / "config_servidor.json"
        self.escrever_config({"spotify": {"client_id": CLIENT_ID}})

    def escrever_config(self, config) -> None:
        texto = config if isinstance(config, str) else json.dumps(config)
        self.config.write_text(texto, encoding="utf-8")

    def rodar_main(self, *argumentos, **extra):
        """Roda o main com tudo apontando para o falso; devolve (saída, erros, código de saída ou None)."""
        saida, erros, codigo = io.StringIO(), io.StringIO(), None
        parametros = {"config": self.config, "arquivo_token": self.arquivo, "contas_url": self.falso.contas_url,
                      "relogio": lambda: AGORA, "tempo_limite": 10, **extra}
        if callable(parametros.get("ler")):
            ler = parametros["ler"]
            parametros["ler"] = lambda prompt: ler(saida.getvalue())
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erros):
            try:
                conectar.main(list(argumentos), **parametros)
            except SystemExit as e:
                codigo = e.code
        return saida.getvalue(), erros.getvalue(), codigo

    # --- PKCE e URL ---

    def test_pkce_com_o_vetor_do_rfc_7636(self):
        self.assertEqual(conectar.calcular_challenge(VERIFIER_DO_RFC), CHALLENGE_DO_RFC)

    def test_verifier_aleatorio_dentro_das_regras(self):
        verifier = conectar.gerar_verifier()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertRegex(verifier, r"^[A-Za-z0-9._~-]+$")
        self.assertNotEqual(verifier, conectar.gerar_verifier())
        self.assertNotIn("=", conectar.calcular_challenge(verifier))

    def test_montar_url(self):
        url = conectar.montar_url(CLIENT_ID, "desafio", "estado-1", self.falso.contas_url)
        partes = urlsplit(url)
        self.assertEqual(f"{partes.scheme}://{partes.netloc}{partes.path}", f"{self.falso.contas_url}/authorize")
        self.assertEqual({k: v[0] for k, v in parse_qs(partes.query).items()}, {
            "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": "http://127.0.0.1:8888/callback",
            "code_challenge_method": "S256", "code_challenge": "desafio", "state": "estado-1",
            "scope": "user-modify-playback-state user-read-playback-state playlist-read-private user-read-private"})
        self.assertTrue(conectar.montar_url(CLIENT_ID, "d", "e").startswith("https://accounts.spotify.com/authorize?"))

    # --- endereço colado ---

    def test_extrair_codigo_de_url_colada(self):
        for colado in ("  http://127.0.0.1:8888/callback?code=AQD123&state=abc \n",
                       '"http://127.0.0.1:8888/callback?code=AQD123&state=abc"',
                       "code=AQD123&state=abc"):
            with self.subTest(colado=colado):
                self.assertEqual(conectar.extrair_codigo(colado, "abc"), "AQD123")

    def test_extrair_codigo_recusa(self):
        casos = [("http://127.0.0.1:8888/callback?code=AQD123&state=outro", "o state não confere"),
                 ("http://127.0.0.1:8888/callback?code=AQD123", "o state não confere"),
                 ("http://127.0.0.1:8888/callback?error=access_denied&state=abc", "você não autorizou"),
                 ("http://127.0.0.1:8888/callback?state=abc", "não achei o código"),
                 ("", "não achei o código"),
                 ("qualquer coisa", "não achei o código")]
        for colado, trecho in casos:
            with self.subTest(colado=colado), self.assertRaises(conectar.ErroAoConectar) as erro:
                conectar.extrair_codigo(colado, "abc")
            self.assertIn(trecho, str(erro.exception))

    def test_extrair_codigo_confere_o_state_antes_do_erro(self):
        # Um endereço de outra tentativa, ou montado por outro site, não pode nem encerrar a conexão como recusada.
        for colado in ("http://127.0.0.1:8888/callback?error=access_denied&state=outro",
                       "http://127.0.0.1:8888/callback?error=access_denied",
                       "http://127.0.0.1:8888/callback?error=server_error&state=outro"):
            with self.subTest(colado=colado), self.assertRaises(conectar.ErroAoConectar) as erro:
                conectar.extrair_codigo(colado, "abc")
            self.assertIn("o state não confere", str(erro.exception))
        with self.assertRaises(conectar.ErroAoConectar) as erro:
            conectar.extrair_codigo("http://127.0.0.1:8888/callback?error=server_error&state=abc", "abc")
        self.assertIn("devolveu o erro server_error", str(erro.exception))

    # --- troca do código ---

    def test_trocar_codigo(self):
        self.falso.programar("POST", "/contas/api/token", (200, {
            "access_token": "access-1", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "refresh-1",
            "scope": "user-read-private"}))
        token = conectar.trocar_codigo(CLIENT_ID, "codigo-1", VERIFIER_DO_RFC, contas_url=self.falso.contas_url,
                                       relogio=lambda: AGORA)
        self.assertEqual(token, {"refresh_token": "refresh-1", "access_token": "access-1", "expira_em": AGORA + 3600})
        pedido, = self.falso.feitos("POST", "/contas/api/token")
        self.assertEqual(pedido["form"], {"grant_type": "authorization_code", "code": "codigo-1",
                                          "redirect_uri": "http://127.0.0.1:8888/callback", "client_id": CLIENT_ID,
                                          "code_verifier": VERIFIER_DO_RFC})

    def test_trocar_codigo_recusado(self):
        casos = [((400, {"error": "invalid_grant", "error_description": "Invalid authorization code"}),
                  "recusou o código de autorização"),
                 ((200, {"access_token": "a", "expires_in": 3600}), "não mandou o refresh token"),
                 ((500, PAGINA_DE_ERRO), "problemas")]
        for resposta, trecho in casos:
            with self.subTest(resposta=resposta):
                self.falso.programar("POST", "/contas/api/token", resposta)
                with self.assertRaises(conectar.ErroAoConectar) as erro:
                    conectar.trocar_codigo(CLIENT_ID, "c", "v", contas_url=self.falso.contas_url)
                self.assertIn(trecho, str(erro.exception))

    # --- callback ---

    def esperar(self, navegador, state="estado-1", **extra):
        url = conectar.montar_url(CLIENT_ID, "desafio", state, self.falso.contas_url)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return conectar.esperar_callback(url, state, porta=navegador.porta, abrir_navegador=navegador,
                                             **{"tempo_limite": 10, **extra})

    def test_callback_de_ponta_a_ponta(self):
        navegador = NavegadorFalso(porta_livre(), antes=["/favicon.ico"])
        self.assertEqual(self.esperar(navegador), "codigo-1")
        (status_favicon, _, _), (status, tipo, pagina) = navegador.paginas
        self.assertEqual(status_favicon, 404)  # outro endereço não encerra a espera
        self.assertEqual((status, tipo), (200, "text/html; charset=utf-8"))
        # O código ainda não foi trocado pelo token: a página não pode dizer que já conectou.
        self.assertIn("Autorização recebida. Volte ao terminal para ver se a conexão terminou.", pagina)
        self.assertNotIn("conectado", pagina)

    def test_callback_com_navegador_que_falha_depois_de_abrir(self):
        self.assertEqual(self.esperar(NavegadorFalso(porta_livre(), falhar=True)), "codigo-1")

    def test_callback_com_state_errado(self):
        navegador = NavegadorFalso(porta_livre(), consulta=lambda state: "code=codigo-1&state=de-outra-vez")
        with self.assertRaises(conectar.ErroAoConectar) as erro:
            self.esperar(navegador)
        self.assertIn("o state não confere", str(erro.exception))
        status, _, pagina = navegador.paginas[-1]
        self.assertEqual(status, 400)
        self.assertIn("state não confere", pagina)

    def test_callback_com_access_denied(self):
        navegador = NavegadorFalso(porta_livre(), consulta=lambda state: f"error=access_denied&state={state}")
        with self.assertRaises(conectar.ErroAoConectar) as erro:
            self.esperar(navegador)
        self.assertIn("você não autorizou", str(erro.exception))
        self.assertIn("Não deu para conectar o Spotify", navegador.paginas[-1][2])

    def test_pagina_do_callback_escapa_o_html(self):
        navegador = NavegadorFalso(porta_livre(), consulta=lambda state: f"error=%3Cb%3Ex%3C%2Fb%3E&state={state}")
        with self.assertRaises(conectar.ErroAoConectar):
            self.esperar(navegador)
        pagina = navegador.paginas[-1][2]
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", pagina)
        self.assertNotIn("<b>x</b>", pagina)

    def test_navegador_so_abre_se_houver_tela(self):
        for plataforma, ambiente, abre in (("linux", {}, False), ("linux", {"DISPLAY": ":0"}, True),
                                           ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True), ("win32", {}, True)):
            with self.subTest(plataforma=plataforma, ambiente=ambiente), \
                    mock.patch.object(conectar.webbrowser, "open", return_value=True) as abrir, \
                    mock.patch.object(conectar.sys, "platform", plataforma), mock.patch.dict(os.environ, ambiente):
                if not ambiente:
                    os.environ.pop("DISPLAY", None)
                    os.environ.pop("WAYLAND_DISPLAY", None)
                self.assertEqual(conectar.abrir_no_navegador("http://exemplo"), abre)
                self.assertEqual(abrir.call_args_list, [mock.call("http://exemplo")] if abre else [])

    def test_porta_ocupada_sugere_colar(self):
        with socket.socket() as ocupante:
            # Como outro servidor em Python (o próprio spotify_conectar.py aberto duas vezes): com SO_REUSEADDR
            # dos dois lados, o Windows deixaria os dois escutarem na mesma porta.
            ocupante.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ocupante.bind(("127.0.0.1", 0))
            ocupante.listen()
            navegador = NavegadorFalso(ocupante.getsockname()[1])
            with self.assertRaises(conectar.ErroAoConectar) as erro:
                self.esperar(navegador)
        self.assertIn("já está em uso", str(erro.exception))
        self.assertIn("python spotify_conectar.py --colar", str(erro.exception))
        self.assertEqual(navegador.urls, [])

    def test_sem_resposta_do_spotify_desiste(self):
        inicio = time.monotonic()
        with self.assertRaises(conectar.ErroAoConectar) as erro:
            self.esperar(NavegadorParado(porta_livre()), tempo_limite=0.3)
        self.assertIn("não voltou a tempo", str(erro.exception))
        self.assertLess(time.monotonic() - inicio, 5)

    # --- main ---

    def test_main_de_ponta_a_ponta(self):
        self.falso.programar("POST", "/contas/api/token", TROCA_CERTA)
        navegador = NavegadorFalso(porta_livre())
        saida, erros, codigo = self.rodar_main(abrir_navegador=navegador, porta=navegador.porta)
        self.assertIsNone(codigo, erros)
        url, = navegador.urls
        self.assertIn(url, saida)  # o endereço também aparece na tela
        desafio = parse_qs(urlsplit(url).query)["code_challenge"][0]
        pedido, = self.falso.feitos("POST", "/contas/api/token")
        self.assertEqual(pedido["form"]["code"], "codigo-1")
        self.assertEqual(conectar.calcular_challenge(pedido["form"]["code_verifier"]), desafio)
        self.assertEqual(self.lido(), {"refresh_token": "refresh-da-troca", "access_token": "access-da-troca",
                                       "expira_em": AGORA + 3600})
        self.assertIn("6 meses", saida)
        self.assertIn("spotify_token.json dá acesso à sua conta", saida)
        self.assertIn(".gitignore", saida)
        self.assertIn("INVALID_CLIENT: Invalid redirect URI", saida)  # o erro de configuração mais comum

    def test_main_colar(self):
        def colar(tela: str) -> str:
            url = re.search(r"\S+/authorize\?\S+", tela).group(0)
            state = parse_qs(urlsplit(url).query)["state"][0]
            return f"http://127.0.0.1:8888/callback?code=codigo-colado&state={state}"

        self.falso.programar("POST", "/contas/api/token", TROCA_CERTA)
        saida, erros, codigo = self.rodar_main("--colar", ler=colar)
        self.assertIsNone(codigo, erros)
        self.assertEqual(self.falso.feitos("POST", "/contas/api/token")[0]["form"]["code"], "codigo-colado")
        self.assertEqual(self.lido()["refresh_token"], "refresh-da-troca")
        self.assertIn("Spotify conectado", saida)
        self.assertIn("INVALID_CLIENT: Invalid redirect URI", saida)

    def test_main_com_erro_sai_com_codigo_1_sem_gravar(self):
        casos = [(lambda state: "code=c&state=errado", None, "o state não confere"),
                 (lambda state: f"error=access_denied&state={state}", None, "você não autorizou"),
                 (lambda state: f"code=c&state={state}", (400, {"error": "invalid_grant"}),
                  "recusou o código de autorização")]
        for consulta, troca, trecho in casos:
            with self.subTest(trecho=trecho):
                self.falso.zerar()
                if troca:
                    self.falso.programar("POST", "/contas/api/token", troca)
                navegador = NavegadorFalso(porta_livre(), consulta=consulta)
                _, erros, codigo = self.rodar_main(abrir_navegador=navegador, porta=navegador.porta)
                self.assertEqual(codigo, 1)
                self.assertIn(trecho, erros)
                self.assertFalse(self.arquivo.exists())
                if not troca:
                    self.assertEqual(self.falso.feitos(), [])

    def test_main_colar_sem_nada_colado(self):
        def sem_entrada(tela):
            raise EOFError

        _, erros, codigo = self.rodar_main("--colar", ler=sem_entrada)
        self.assertEqual(codigo, 1)
        self.assertIn("nenhum endereço foi colado", erros)

    def test_argumento_desconhecido(self):
        _, _, codigo = self.rodar_main("--outro")
        self.assertIn("Uso: python spotify_conectar.py [--colar]", codigo)

    def test_ajuda_nao_conecta(self):
        for argumento in ("-h", "--help"):
            with self.subTest(argumento=argumento):
                saida, _, codigo = self.rodar_main(argumento, ler=lambda tela: self.fail("pediu o endereço"))
                self.assertIsNone(codigo)
                self.assertIn("--colar", saida)
                self.assertEqual(self.falso.feitos(), [])
                self.assertFalse(self.arquivo.exists())

    def test_config_sem_client_id_explica_como_criar_o_app(self):
        casos = [{}, {"spotify": None}, {"spotify": "abc"}, {"spotify": {}}, {"spotify": {"client_id": ""}},
                 {"spotify": {"client_id": "  "}}, {"spotify": {"client_id": 123}},
                 {"spotify": {"client_id": "CLIENT-ID-DO-APP-SPOTIFY"}}, None]
        for config in casos:
            with self.subTest(config=config):
                if config is None:
                    self.config.unlink(missing_ok=True)
                else:
                    self.escrever_config(config)
                _, _, mensagem = self.rodar_main()
                self.assertIsInstance(mensagem, str)
                for trecho in ("developer.spotify.com/dashboard", "http://127.0.0.1:8888/callback", '"Web API"',
                               "Client ID", "config_servidor.json"):
                    self.assertIn(trecho, mensagem)

    def test_config_com_problema(self):
        self.escrever_config('{"spotify": {"client_id": "abc",}}')
        self.assertIn("Erro de JSON", self.rodar_main()[2])
        self.escrever_config({"spotify": {"client_id": "abc def"}})
        self.assertIn("espaço ou acento", self.rodar_main()[2])

    def test_ler_client_id(self):
        self.config.write_text('{"spotify": {"client_id": "  abc123  "}}', encoding="utf-8-sig")  # com BOM
        self.assertEqual(conectar.ler_client_id(self.config), "abc123")


class NavegadorParado(NavegadorFalso):
    """Abre, mas o Spotify nunca volta para o /callback."""

    def __call__(self, url: str):
        self.urls.append(url)
        return True


@PRECISA_DEPENDENCIAS
class TestScriptConectar(unittest.TestCase):
    """Roda o spotify_conectar.py como o usuário roda, numa cópia da pasta, sem rede e sem navegador."""

    def rodar(self, config: dict | None, *argumentos) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pasta = copiar_componente(PASTA_SERVIDOR, Path(tmp) / "celular_servidor")
            if config is not None:
                (pasta / "config_servidor.json").write_text(json.dumps(config), encoding="utf-8")
            resultado = subprocess.run([sys.executable, "spotify_conectar.py", *argumentos], cwd=pasta,
                                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                       timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertFalse((pasta / "spotify_token.json").exists())
        return resultado

    def test_sem_config_explica_e_sai_com_erro(self):
        resultado = self.rodar(None)
        self.assertEqual(resultado.returncode, 1)
        self.assertIn("developer.spotify.com/dashboard", resultado.stderr)

    def test_colar_sem_entrada_sai_com_erro(self):
        resultado = self.rodar({"spotify": {"client_id": CLIENT_ID}}, "--colar")
        self.assertEqual(resultado.returncode, 1, resultado.stderr)
        self.assertIn("https://accounts.spotify.com/authorize?", resultado.stdout)
        self.assertIn("nenhum endereço foi colado", resultado.stderr)


if __name__ == "__main__":
    unittest.main()
