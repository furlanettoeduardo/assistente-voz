"""
Spotify pela Web API: toca música, artista, álbum ou playlist no PC e controla o player (conta Premium).

A conta é conectada uma vez com o spotify_conectar.py (fluxo PKCE, sem client secret), que grava o
spotify_token.json. Daí em diante o access token, que vale 1 hora, é renovado aqui com o refresh token,
que vale 6 meses a partir da autorização. Os comandos do player vão sempre com o device_id do Spotify do PC
(o aparelho de nome `dispositivo` ou, sem ele, o primeiro computador), mesmo com o celular ou a TV tocando:
nunca vão para outro aparelho.
"""
import contextlib
import difflib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import requests

CONTAS_URL = "https://accounts.spotify.com"
API_URL = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
ESCOPOS = "user-modify-playback-state user-read-playback-state playlist-read-private user-read-private"
ARQUIVO_TOKEN = Path(__file__).parent / "spotify_token.json"
TIPOS = {"musica": "track", "artista": "artist", "album": "album", "playlist": "playlist"}
ACOES = ("pausar", "continuar", "proxima", "anterior")

TEMPO_LIMITE = 10  # segundos por chamada
MARGEM = 60  # renova o access token quando faltar menos que isto para ele vencer
VALIDADE_PADRAO = 3600  # o Spotify sempre manda expires_in; se não vier, vale o que ele documenta
ESPERA_MAXIMA = 3  # num 429 com Retry-After até este valor, espera e repete uma vez
LIMITE_DA_BUSCA = 5  # o máximo da busca caiu para 10 em 2026
# Playlist da conta parecida com o pedido (o 3º critério de _playlist_da_conta):
SEMELHANCA_MINIMA = 0.8  # o difflib precisa passar disto ("reggae" e "reggaeton" dão 0.8 exato)
PEDIDO_PARA_PARECIDO = 6  # só vale para pedidos com pelo menos estes caracteres
PALAVRA_CURTA = 5  # as palavras do pedido até este tamanho ("funk", "80") precisam estar iguais no nome
# Palavras do pedido que podem faltar ou mudar no nome ("músicas do domingo" e "Músicas de Domingo").
LIGACOES = {"a", "o", "e", "as", "os", "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "pra", "para"}

RECONECTAR = "na pasta celular_servidor, rode python spotify_conectar.py"
NAO_CONECTADO = f"o Spotify ainda não foi conectado: {RECONECTAR}"
AUTORIZACAO_VENCEU = f"a autorização do Spotify venceu (ela dura 6 meses): {RECONECTAR} de novo"
AUTORIZACAO_RECUSADA = f"o Spotify recusou a autorização: {RECONECTAR} de novo"
ARQUIVO_ESTRAGADO = f"não consegui ler o arquivo spotify_token.json: {RECONECTAR} de novo"
CLIENTE_RECUSADO = (f'o Spotify não reconheceu o "client_id" do config_servidor.json: confira o Client ID e '
                    f"{RECONECTAR} de novo")
PREMIUM = ("o Spotify recusou: a conta precisa ser Premium e estar liberada em Users Management no app do "
           "painel de desenvolvedor")
RESTRICAO = ("o Spotify não aceitou esse comando agora (acontece, por exemplo, ao pausar uma música que já "
             "está pausada)")
COTA = "a cota do app do Spotify acabou por enquanto"
ESPERAR = "o Spotify pediu para esperar um pouco"
SEM_REDE = "não consegui falar com o Spotify"
SEM_DISPOSITIVO = "o Spotify não está aberto no PC: abra o aplicativo e peça de novo"
FORA_DO_AR = "o Spotify está com problemas agora: tente de novo em instantes"
RESPOSTA_ESTRANHA = "o Spotify mandou uma resposta que não entendi"

DESCRICOES = {"pausar": "pausei a música", "continuar": "continuei a música", "proxima": "pulei para a próxima",
              "anterior": "voltei para a anterior"}
COMANDOS = {"pausar": ("PUT", "/me/player/pause"), "continuar": ("PUT", "/me/player/play"),
            "proxima": ("POST", "/me/player/next"), "anterior": ("POST", "/me/player/previous")}


class ErroNoSpotify(Exception):
    """
    Falha ao falar com o Spotify: a mensagem é em português, para o usuário; `detalhe` é o técnico, para o
    terminal. `codigo` diz o tipo: "sem_dispositivo", "autorizacao", "premium", "nao_encontrado", "limite",
    "rede" ou "outro".
    """

    def __init__(self, mensagem: str, codigo: str = "outro", detalhe: str = ""):
        super().__init__(mensagem)
        self.codigo = codigo
        self.detalhe = detalhe


# ---------- Arquivo de token (usado também pelo spotify_conectar.py) ----------

def ler_token(arquivo: Path) -> dict | None:
    """Conteúdo do arquivo de token, ou None se ele não existe. Arquivo estragado levanta ErroNoSpotify."""
    try:
        dados = json.loads(Path(arquivo).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    # ValueError inclui JSON inválido e texto que não é UTF-8; RecursionError, um JSON aninhado demais.
    except (OSError, ValueError, RecursionError) as e:
        raise ErroNoSpotify(ARQUIVO_ESTRAGADO, "autorizacao", repr(e)) from e
    if not isinstance(dados, dict) or not texto_preenchido(dados.get("refresh_token")):
        raise ErroNoSpotify(ARQUIVO_ESTRAGADO, "autorizacao", "sem refresh_token no arquivo")
    return dados


def gravar_token(arquivo: Path, token: dict) -> None:
    """
    Grava o token de uma vez: escreve num arquivo temporário na mesma pasta e troca com os.replace, para
    uma queda no meio não deixar o arquivo pela metade. O mkstemp cria o arquivo só para o dono (no Linux).
    """
    arquivo = Path(arquivo)
    descritor, temporario = tempfile.mkstemp(prefix=".spotify_token-", suffix=".tmp", dir=arquivo.parent)
    try:
        with os.fdopen(descritor, "w", encoding="utf-8") as f:
            json.dump(token, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporario, arquivo)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporario)
        raise


def token_de_resposta(dados: dict, agora: float, anterior: dict | None = None) -> dict:
    """
    Monta o conteúdo do arquivo a partir da resposta do /api/token. Nem toda renovação traz um refresh
    token novo: sem ele, continua valendo o anterior.
    """
    validade = dados.get("expires_in")
    if isinstance(validade, bool) or not isinstance(validade, (int, float)) or not 0 < validade < math.inf:
        validade = VALIDADE_PADRAO
    refresh = dados.get("refresh_token")
    if not texto_preenchido(refresh):
        refresh = (anterior or {}).get("refresh_token")
    return {"refresh_token": refresh, "access_token": dados["access_token"], "expira_em": agora + validade}


def pedir_token(formulario: dict, sessao=requests, contas_url: str = CONTAS_URL,
                recusado: str = AUTORIZACAO_VENCEU) -> dict:
    """
    POST {contas_url}/api/token com o formulário dado. Devolve o JSON da resposta; qualquer falha vira
    ErroNoSpotify. `recusado` é a mensagem do 400 invalid_grant (refresh token vencido ou código usado).
    """
    try:
        r = sessao.request("POST", f"{contas_url}/api/token", data=formulario, timeout=TEMPO_LIMITE)
    except requests.RequestException as e:
        raise ErroNoSpotify(SEM_REDE, "rede", repr(e)) from e
    detalhe = f"HTTP {r.status_code} em /api/token: {r.text[:300]}"
    dados = ler_json(r)
    if r.status_code == 200:
        if isinstance(dados, dict) and texto_preenchido(dados.get("access_token")):
            return dados
        # Uma resposta 200 pode trazer o refresh token: no terminal vão só os nomes dos campos.
        campos = sorted(dados) if isinstance(dados, dict) else type(dados).__name__
        raise ErroNoSpotify(RESPOSTA_ESTRANHA, "outro", f"HTTP 200 em /api/token sem access_token: {campos}")
    erro = dados.get("error") if isinstance(dados, dict) else None
    if r.status_code == 400 and erro == "invalid_grant":
        raise ErroNoSpotify(recusado, "autorizacao", detalhe)
    if erro == "invalid_client":
        raise ErroNoSpotify(CLIENTE_RECUSADO, "autorizacao", detalhe)
    if r.status_code == 429:
        raise ErroNoSpotify(ESPERAR, "limite", detalhe)
    if r.status_code >= 500:
        raise ErroNoSpotify(FORA_DO_AR, "outro", detalhe)
    raise ErroNoSpotify(f"o Spotify recusou a autorização (erro {r.status_code})", "autorizacao", detalhe)


# ---------- Auxiliares ----------

def texto_preenchido(valor) -> bool:
    return isinstance(valor, str) and bool(valor.strip())


def ler_json(resposta):
    """JSON da resposta, ou None se o corpo não for JSON (uma página de erro, por exemplo)."""
    try:
        return resposta.json()
    except ValueError:  # o JSONDecodeError do requests é um ValueError
        return None


def normalizar(texto) -> str:
    """Sem acentos, sem maiúsculas e com os espaços arrumados, para comparar nomes."""
    decomposto = unicodedata.normalize("NFKD", str(texto or ""))
    return " ".join("".join(c for c in decomposto if not unicodedata.combining(c)).casefold().split())


def palavras(texto: str) -> list[str]:
    """As palavras de um texto já normalizado, sem a pontuação."""
    return re.findall(r"\w+", texto)


def contem_palavras(maior: list[str], menor: list[str]) -> bool:
    """`menor` aparece dentro de `maior` em palavras inteiras e seguidas."""
    n = len(menor)
    return n > 0 and any(maior[i:i + n] == menor for i in range(len(maior) - n + 1))


def parecido(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def palavra_no_nome(palavra: str, do_nome: list[str]) -> bool:
    """
    A palavra do pedido está no nome: igual ou, se for longa, com um erro de digitação pequeno ("nacionl" e
    "nacional"). Assim "internacional" não vira "nacional" nem "funk" vira "punk".
    """
    if palavra in LIGACOES or palavra in do_nome:
        return True
    return len(palavra) > PALAVRA_CURTA and any(parecido(palavra, p) > SEMELHANCA_MINIMA for p in do_nome)


def juntar(nomes: list[str]) -> str:
    """Junta os nomes como na fala: "A", "A e B", "A, B e C"."""
    nomes = [n for n in nomes if n]
    if len(nomes) <= 1:
        return "".join(nomes)
    return f"{', '.join(nomes[:-1])} e {nomes[-1]}"


def artistas(item: dict) -> str:
    lista = item.get("artists")
    return juntar([a.get("name") for a in lista if isinstance(a, dict) and texto_preenchido(a.get("name"))]
                  if isinstance(lista, list) else [])


def motivo_do_erro(dados) -> str:
    """O error.reason que o Spotify manda em alguns erros, como NO_ACTIVE_DEVICE e QUOTA_EXCEEDED."""
    erro = dados.get("error") if isinstance(dados, dict) else None
    return str(erro.get("reason") or "") if isinstance(erro, dict) else ""


def mensagem_do_erro(dados) -> str:
    erro = dados.get("error") if isinstance(dados, dict) else None
    return str(erro.get("message") or "") if isinstance(erro, dict) else ""


def falta_aparelho(resposta) -> bool:
    """
    Um 404 do player é falta de aparelho (NO_ACTIVE_DEVICE ou "device" na mensagem) ou não se explica (corpo
    sem motivo nem mensagem)? Um 404 com outro motivo, ou que diz "Not found", é outro problema.
    """
    dados = ler_json(resposta)
    motivo = motivo_do_erro(dados)
    if motivo:
        return motivo == "NO_ACTIVE_DEVICE"
    mensagem = mensagem_do_erro(dados).lower()
    return not mensagem or "device" in mensagem


def segundos_para_esperar(resposta) -> float | None:
    """O Retry-After em segundos, ou None se não veio ou não é um número."""
    try:
        segundos = float(resposta.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return max(0.0, segundos) if math.isfinite(segundos) else None


def itens_validos(lista) -> list[dict]:
    """A API às vezes manda null no meio dos itens: fica só o que tem uri."""
    if not isinstance(lista, list):
        return []
    return [i for i in lista if isinstance(i, dict) and texto_preenchido(i.get("uri"))]


def tipo_da_api(tipo) -> str | None:
    """Converte "musica", "música" ou "track" em "track"; None se o tipo não for um dos de TIPOS."""
    chave = normalizar(tipo)
    return TIPOS.get(chave) or (chave if chave in TIPOS.values() else None)


def descrever(tipo_api: str, item: dict) -> str:
    nome = item.get("name") if texto_preenchido(item.get("name")) else "sem nome"
    de_quem = artistas(item)
    if tipo_api == "track":
        return f'coloquei "{nome}", de {de_quem}' if de_quem else f'coloquei "{nome}"'
    if tipo_api == "album":
        return f'coloquei o álbum "{nome}", de {de_quem}' if de_quem else f'coloquei o álbum "{nome}"'
    if tipo_api == "artist":
        return f"coloquei {nome} para tocar"
    return f'coloquei a playlist "{nome}"'


def corpo_do_play(tipo_api: str, item: dict) -> dict:
    """
    Faixa: toca dentro do álbum, a partir dela, para a música seguir depois; sem o álbum, só a faixa.
    Artista, álbum e playlist: o contexto inteiro (o offset não vale para artista).
    """
    if tipo_api == "track":
        album = item.get("album")
        if isinstance(album, dict) and texto_preenchido(album.get("uri")):
            return {"context_uri": album["uri"], "offset": {"uri": item["uri"]}}
        return {"uris": [item["uri"]]}
    return {"context_uri": item["uri"]}


# ---------- Cliente ----------

class Spotify:
    def __init__(self, client_id: str, arquivo_token: Path = ARQUIVO_TOKEN, dispositivo: str | None = None,
                 sessao=requests, relogio=time.time, contas_url: str = CONTAS_URL, api_url: str = API_URL,
                 dormir=time.sleep):
        self.client_id = str(client_id).strip()
        self.arquivo_token = Path(arquivo_token)
        self.dispositivo = normalizar(dispositivo) if texto_preenchido(dispositivo) else None
        self._sessao = sessao
        self._relogio = relogio
        self._contas_url = contas_url.rstrip("/")
        self._api_url = api_url.rstrip("/")
        self._dormir = dormir
        self._trava_renovacao = threading.Lock()  # uma renovação por vez: o refresh token pode mudar
        self._trava_arquivo = threading.Lock()  # no Windows, o os.replace falha com o arquivo aberto

    # --- token ---

    def conectado(self) -> bool:
        """O arquivo de token existe e tem refresh_token."""
        try:
            with self._trava_arquivo:
                return ler_token(self.arquivo_token) is not None
        except ErroNoSpotify:
            return False

    def _ler_token(self) -> dict:
        with self._trava_arquivo:
            token = ler_token(self.arquivo_token)
        if token is None:
            raise ErroNoSpotify(NAO_CONECTADO, "autorizacao", f"{self.arquivo_token} não existe")
        return token

    def _access_token(self, recusado: str | None = None) -> str:
        """
        Access token válido, renovado se faltar menos de MARGEM segundos para vencer. `recusado` é um token
        que a API acabou de recusar com 401: se o arquivo ainda tem esse, renova mesmo dentro do prazo (se
        outra thread já renovou, usa o novo).
        """
        with self._trava_renovacao:
            token = self._ler_token()
            atual = token.get("access_token")
            expira_em = token.get("expira_em")
            valido = (texto_preenchido(atual) and atual != recusado and not isinstance(expira_em, bool)
                      and isinstance(expira_em, (int, float)) and expira_em - self._relogio() >= MARGEM)
            if valido:
                return atual
            agora = self._relogio()
            dados = pedir_token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                                 "client_id": self.client_id}, self._sessao, self._contas_url)
            novo = token_de_resposta(dados, agora, token)
            try:
                with self._trava_arquivo:
                    gravar_token(self.arquivo_token, novo)
            except OSError as e:
                raise ErroNoSpotify("não consegui gravar o arquivo spotify_token.json: confira se dá para gravar "
                                    "na pasta celular_servidor", "outro", repr(e)) from e
            return novo["access_token"]

    # --- chamadas ---

    def _chamar(self, metodo: str, caminho: str, params: dict | None = None, corpo: dict | None = None):
        """
        Chama a API com o token. Num 401, renova e repete uma vez; num 429 curto (Retry-After até
        ESPERA_MAXIMA e sem QUOTA_EXCEEDED), espera e repete uma vez. Devolve a resposta sem conferir o status.
        """
        token = self._access_token()
        renovou = esperou = False
        while True:
            try:
                r = self._sessao.request(metodo, f"{self._api_url}{caminho}", params=params, json=corpo,
                                         headers={"Authorization": f"Bearer {token}"}, timeout=TEMPO_LIMITE)
            except requests.RequestException as e:
                raise ErroNoSpotify(SEM_REDE, "rede", repr(e)) from e
            if r.status_code == 401 and not renovou:
                renovou = True
                token = self._access_token(recusado=token)
                continue
            if r.status_code == 429 and not esperou and motivo_do_erro(ler_json(r)) != "QUOTA_EXCEEDED":
                espera = segundos_para_esperar(r)
                if espera is not None and espera <= ESPERA_MAXIMA:
                    esperou = True
                    self._dormir(espera)
                    continue
            return r

    def _conferir(self, r, caminho: str) -> None:
        """Levanta ErroNoSpotify, com a mensagem em português, se a resposta não for 2xx."""
        if 200 <= r.status_code < 300:
            return
        dados = ler_json(r)
        detalhe = f"HTTP {r.status_code} em {caminho}: {r.text[:300]}"
        if r.status_code == 401:
            raise ErroNoSpotify(AUTORIZACAO_RECUSADA, "autorizacao", detalhe)
        if r.status_code == 403:
            if "restriction violated" in mensagem_do_erro(dados).lower():
                raise ErroNoSpotify(RESTRICAO, "outro", detalhe)
            raise ErroNoSpotify(PREMIUM, "premium", detalhe)
        if r.status_code == 429:
            if motivo_do_erro(dados) == "QUOTA_EXCEEDED":
                raise ErroNoSpotify(COTA, "limite", detalhe)
            raise ErroNoSpotify(ESPERAR, "limite", detalhe)
        if r.status_code >= 500:
            raise ErroNoSpotify(FORA_DO_AR, "outro", detalhe)
        raise ErroNoSpotify(f"o Spotify recusou o pedido (erro {r.status_code})", "outro", detalhe)

    def _obter(self, caminho: str, params: dict | None = None) -> dict:
        """GET que precisa devolver um objeto JSON."""
        r = self._chamar("GET", caminho, params=params)
        self._conferir(r, caminho)
        dados = ler_json(r)
        if not isinstance(dados, dict):
            raise ErroNoSpotify(RESPOSTA_ESTRANHA, "outro", f"HTTP {r.status_code} em {caminho}: {r.text[:300]}")
        return dados

    def _no_player(self, metodo: str, caminho: str, corpo: dict | None = None) -> None:
        """
        Comando do player, sempre com o device_id do Spotify do PC: sem ele, o comando iria para o aparelho
        ativo, que pode ser o celular ou a TV. Se o PC sumir entre a lista e o comando (404 de falta de
        aparelho), busca a lista de novo e repete uma vez.
        """
        for _ in range(2):
            aparelho = self._id_do_computador()
            r = self._chamar(metodo, caminho, params={"device_id": aparelho}, corpo=corpo)
            if not (r.status_code == 404 and falta_aparelho(r)):
                self._conferir(r, caminho)
                return
        raise ErroNoSpotify(SEM_DISPOSITIVO, "sem_dispositivo",
                            f"HTTP 404 em {caminho} com device_id, duas vezes: {r.text[:300]}")

    def _id_do_computador(self) -> str:
        """
        Entre os aparelhos que aceitam comandos: com `dispositivo` preenchido, só o de mesmo nome; vazio, o
        primeiro computador. Nunca escolhe outro aparelho por conta própria.
        """
        dados = self._obter("/me/player/devices")
        aparelhos = dados.get("devices") if isinstance(dados.get("devices"), list) else []
        livres = [a for a in aparelhos if isinstance(a, dict) and texto_preenchido(a.get("id"))
                  and a.get("is_restricted") is not True]
        if self.dispositivo:
            escolhidos = [a for a in livres if normalizar(a.get("name")) == self.dispositivo]
        else:
            escolhidos = [a for a in livres if normalizar(a.get("type")) == "computer"]
        if escolhidos:
            return escolhidos[0]["id"]
        procurado = f'o de nome "{self.dispositivo}"' if self.dispositivo else "um computador"
        vistos = [(a.get("name"), a.get("type"), a.get("is_restricted")) for a in aparelhos if isinstance(a, dict)]
        raise ErroNoSpotify(SEM_DISPOSITIVO, "sem_dispositivo", f"procurei {procurado}; aparelhos: {vistos}")

    def testar(self) -> list[str]:
        """Para o diagnóstico: renova o token se preciso e lista os aparelhos, sem tocar nada."""
        dados = self._obter("/me/player/devices")
        aparelhos = dados.get("devices") if isinstance(dados.get("devices"), list) else []
        return [str(a.get("name")) for a in aparelhos if isinstance(a, dict) and texto_preenchido(a.get("name"))]

    # --- busca ---

    def _buscar(self, busca: str, tipo_api: str) -> dict | None:
        dados = self._obter("/search", {"q": busca, "type": tipo_api, "limit": LIMITE_DA_BUSCA})
        grupo = dados.get(f"{tipo_api}s")
        itens = itens_validos(grupo.get("items") if isinstance(grupo, dict) else None)
        return itens[0] if itens else None

    def _playlist_da_conta(self, busca: str) -> dict | None:
        """
        A playlist da própria conta que é a pedida (sem acentos e maiúsculas), ou None, e aí vale a busca
        normal. Em ordem: 1º o nome igual; 2º um nome que contém o pedido em palavras inteiras ("funk" em
        "Funk Brasil"), ou um nome de 2 palavras ou mais que está inteiro no pedido; 3º, para pedidos de
        PEDIDO_PARA_PARECIDO caracteres ou mais, o nome mais parecido acima de SEMELHANCA_MINIMA que tenha
        cada palavra do pedido (palavra_no_nome). Assim "funk" não vira "Punk", nem "reggae" vira "Reggaeton",
        nem "rock internacional" vira "Rock Nacional".
        """
        dados = self._obter("/me/playlists", {"limit": 50})
        por_nome = {}
        for item in itens_validos(dados.get("items")):
            por_nome.setdefault(normalizar(item.get("name")), item)  # nomes repetidos: fica a primeira
        pedido = normalizar(busca)
        if pedido in por_nome:
            return por_nome[pedido]
        do_pedido = palavras(pedido)
        nomes = {nome: palavras(nome) for nome in por_nome}
        for nome, do_nome in nomes.items():
            if contem_palavras(do_nome, do_pedido):
                return por_nome[nome]
        for nome, do_nome in nomes.items():
            if len(do_nome) >= 2 and contem_palavras(do_pedido, do_nome):
                return por_nome[nome]
        if len(pedido) < PEDIDO_PARA_PARECIDO:
            return None
        notas = [(parecido(pedido, nome), nome) for nome, do_nome in nomes.items()
                 if all(palavra_no_nome(p, do_nome) for p in do_pedido)]
        nota, nome = max(notas, key=lambda n: n[0], default=(0.0, None))  # empate: fica a primeira
        return por_nome[nome] if nota > SEMELHANCA_MINIMA else None

    # --- o que o servidor usa ---

    def tocar(self, busca: str, tipo: str = "musica") -> str:
        """Procura e toca no PC. `tipo`: musica, artista, album ou playlist. Devolve o que foi feito."""
        tipo_api = tipo_da_api(tipo)
        if tipo_api is None:
            raise ErroNoSpotify(f"não sei tocar o tipo {tipo}: use música, artista, álbum ou playlist")
        busca = " ".join(str(busca or "").split())
        if not busca:
            raise ErroNoSpotify("diga o nome da música, do artista, do álbum ou da playlist")
        item = self._playlist_da_conta(busca) if tipo_api == "playlist" else None
        item = item or self._buscar(busca, tipo_api)
        if item is None:
            raise ErroNoSpotify(f'não encontrei "{busca}" no Spotify', "nao_encontrado")
        self._no_player("PUT", "/me/player/play", corpo_do_play(tipo_api, item))
        return descrever(tipo_api, item)

    def controlar(self, acao: str) -> str:
        """pausar, continuar, proxima ou anterior. Devolve o que foi feito."""
        chave = normalizar(acao)
        if chave not in ACOES:
            raise ErroNoSpotify(f"não conheço a ação {acao}: use pausar, continuar, próxima ou anterior")
        metodo, caminho = COMANDOS[chave]
        self._no_player(metodo, caminho)
        return DESCRICOES[chave]
