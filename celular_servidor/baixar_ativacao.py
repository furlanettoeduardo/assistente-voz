"""
Baixa os modelos do "hey Jarvis" para a página reconhecer a palavra de ativação no navegador. Rode uma vez
(de qualquer pasta; só usa a biblioteca padrão):

    python baixar_ativacao.py

Os três arquivos (cerca de 3,5 MB) vêm da versão 0.5.1 do openWakeWord, no GitHub, e vão para
static/ativacao/modelos, que fica fora do Git. O SHA-256 de cada um é conferido: um arquivo diferente do
esperado é apagado, e nada fica pela metade. Os que já estão certos não são baixados de novo.

Licença: o código do openWakeWord é Apache-2.0, mas os modelos pré-treinados são CC BY-NC-SA 4.0 (uso não
comercial, com atribuição a David Scripka, e obras derivadas com a mesma licença).

    python baixar_ativacao.py --origem URL   baixa de outro endereço com os mesmos arquivos (um espelho)
"""
import hashlib
import http.client
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ORIGEM = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
PASTA = Path(__file__).resolve().parent / "static" / "ativacao" / "modelos"
# nome: (tamanho em bytes, SHA-256)
MODELOS = {
    "melspectrogram.onnx": (1087958, "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f"),
    "embedding_model.onnx": (1326578, "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f"),
    "hey_jarvis_v0.1.onnx": (1271370, "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb"),
}
USO = "Uso: python baixar_ativacao.py [--origem URL]"
TEMPO_LIMITE = 60  # segundos sem resposta do servidor


class ErroNoDownload(Exception):
    """Uma falha já explicada em português, para o usuário."""


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 16), b""):
            h.update(bloco)
    return h.hexdigest()


def megabytes(n: int) -> str:
    return f"{n / 1e6:.1f} MB".replace(".", ",")


def baixar(nome: str, origem: str, pasta: Path) -> None:
    """Baixa um arquivo para um temporário na mesma pasta, confere o SHA-256 e só então o põe no lugar."""
    tamanho, esperado = MODELOS[nome]
    url = origem + nome
    temporario = None
    try:
        with urllib.request.urlopen(url, timeout=TEMPO_LIMITE) as resposta:
            with tempfile.NamedTemporaryFile(dir=pasta, prefix=f".{nome}.", suffix=".parcial", delete=False) as f:
                temporario = Path(f.name)
                h = hashlib.sha256()
                for bloco in iter(lambda: resposta.read(1 << 16), b""):
                    h.update(bloco)
                    f.write(bloco)
        recebido = h.hexdigest()
        if recebido != esperado:
            raise ErroNoDownload(
                f"O {nome} baixado de {url} não é o arquivo esperado (SHA-256 {recebido}, e o certo é {esperado}). "
                "Ele foi apagado. Tente de novo mais tarde; se continuar, o endereço mudou de conteúdo.")
        os.replace(temporario, pasta / nome)
        temporario = None
    except urllib.error.HTTPError as e:
        e.close()  # o erro traz a resposta aberta
        raise ErroNoDownload(f"O servidor respondeu com erro {e.code} ao baixar {url}. Tente de novo mais tarde.") from e
    except urllib.error.URLError as e:
        raise ErroNoDownload(f"Sem conexão para baixar {url}: confira a internet e rode de novo "
                             f"(detalhe técnico: {e.reason}).") from e
    except (TimeoutError, ConnectionError, http.client.HTTPException) as e:
        raise ErroNoDownload(f"A conexão caiu ao baixar {url}: rode de novo (detalhe técnico: {e}).") from e
    except ValueError as e:  # o urllib recusa um endereço sem http:// ou https://
        raise ErroNoDownload(f"O endereço {url} não é válido: use um que comece com https:// ou http://.") from e
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)


def main(argumentos: list[str] | None = None, pasta: Path = PASTA) -> int:
    argumentos = sys.argv[1:] if argumentos is None else argumentos
    origem = ORIGEM
    if argumentos[:1] == ["--origem"] and len(argumentos) == 2:
        origem = argumentos[1].rstrip("/") + "/"
    elif argumentos:
        print(f"Não entendi {' '.join(argumentos)}.\n{USO}", file=sys.stderr)
        return 2
    try:
        pasta.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Não consegui criar a pasta {pasta} (detalhe técnico: {e}).", file=sys.stderr)
        return 1
    for nome, (tamanho, esperado) in MODELOS.items():
        destino = pasta / nome
        if destino.is_file() and sha256(destino) == esperado:
            print(f"{nome}: já está baixado.")
            continue
        print(f"{nome}: baixando {megabytes(tamanho)}...", flush=True)
        try:
            baixar(nome, origem, pasta)
        except ErroNoDownload as e:
            print(e, file=sys.stderr)
            return 1
        except OSError as e:
            print(f"Não consegui gravar o {nome} em {pasta} (detalhe técnico: {e}).", file=sys.stderr)
            return 1
    print(f"Pronto: os modelos do \"hey Jarvis\" estão em {pasta}.\n"
          "Recarregue a página e ligue \"Ouvir hey Jarvis\". Lembre: os modelos são só para uso não comercial "
          "(CC BY-NC-SA 4.0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
