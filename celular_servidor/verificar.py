"""
Diagnóstico do servidor do celular: confere, em ordem, o que a assistente precisa para funcionar
e explica em português o que falhou e como resolver. Não grava áudio nem gasta tokens: para testar
as chaves, só pede a lista de modelos.

Rodar:  python verificar.py   (na pasta celular_servidor)

Código de saída: 0 tudo certo; 1 há problemas, mas o servidor consegue subir;
2 o servidor nem sobe (Python, bibliotecas ou config); 3 o diagnóstico quebrou; 130 interrompido.
"""
import importlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

TUDO_CERTO, COM_PROBLEMAS, NAO_SOBE, QUEBROU, INTERROMPIDO = 0, 1, 2, 3, 130
TOTAL = 6
PASTA = Path(__file__).resolve().parent


def caminho(arquivo: Path) -> str:
    """Caminho completo para as dicas (a pasta atual do usuário varia); no Termux, abreviado com ~."""
    try:
        if os.name == "posix":
            return f"~/{arquivo.relative_to(Path.home())}"
    except ValueError:
        pass
    return str(arquivo)
REDE_PRIVADA = ("no Windows, a rede Wi-Fi precisa estar como Rede privada: Configurações > Rede e Internet > "
                "Wi-Fi > propriedades da sua rede > Tipo de perfil de rede")


def passo(numero: int, titulo: str) -> None:
    print(f"\n[{numero}/{TOTAL}] {titulo}")


def ok(mensagem: str) -> None:
    print(f"  OK: {mensagem}")


def falhou(mensagem: str, *como_resolver: str) -> None:
    print(f"  FALHOU: {mensagem}")
    for dica in como_resolver:
        print(f"    - {dica}")


def pulado(motivo: str) -> None:
    print(f"  PULADO: {motivo}")


# ---------- 1 e 2: o que impede o servidor de subir ----------

def verificar_instalacao() -> bool:
    passo(1, "Python e bibliotecas")
    if sys.version_info < (3, 11):
        falhou(f"este Python é o {sys.version.split()[0]}, e o servidor precisa do 3.11 ou mais novo.",
               "no Termux: pkg upgrade python")
        return False
    faltando = []
    for modulo in ("flask", "requests"):
        try:
            importlib.import_module(modulo)
        except ImportError:
            faltando.append(modulo)
    if faltando:
        falhou(f"faltam as bibliotecas: {', '.join(faltando)}.",
               f"rode: pip install -r {caminho(PASTA / 'requirements.txt')}",
               f"no Termux, dá para rodar tudo de uma vez: bash {caminho(PASTA.parent / 'scripts' / 'termux-instalar.sh')}")
        return False
    ok(f"Python {sys.version.split()[0]} com Flask e requests instalados.")
    return True


def carregar_servidor():
    """Importa servidor.py, que valida o config do mesmo jeito que na hora de subir."""
    passo(2, "Arquivo de configuração")
    try:
        return importlib.import_module("servidor")
    except SystemExit as e:
        falhou(str(e.code), "depois de corrigir, rode este diagnóstico de novo.")
        return None


def valores_de_exemplo(srv) -> list[str]:
    """Chaves que ainda estão com o texto do config_servidor.example.json."""
    try:
        exemplo = json.loads((srv.BASE / "config_servidor.example.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [chave for chave in ("groq_api_key", "llm_api_key", "llm_model", "pc_token")
            if srv.CFG[chave] == exemplo.get(chave)]


# ---------- 3 a 6: chamadas de rede ----------

def listar_modelos(srv, url: str, cabecalhos: dict):
    """GET /models: devolve (ids, None) ou (None, exceção ou resposta com erro)."""
    try:
        r = srv.requests.get(f"{url}/models", headers=cabecalhos, timeout=10)
    except srv.requests.RequestException as e:
        return None, e
    if r.status_code != 200:
        return None, r
    try:
        return [m["id"] for m in r.json()["data"]], None
    except (ValueError, KeyError, TypeError) as e:
        return None, e


def mesmo_modelo(configurado: str, disponivel: str) -> bool:
    # O Ollama lista "qwen3:latest" para quem configurou só "qwen3".
    return configurado == disponivel or f"{configurado}:latest" == disponivel


def sugestoes(ids: list[str], trecho: str) -> str:
    parecidos = [i for i in ids if trecho in i.lower()] or ids
    return ", ".join(sorted(parecidos)[:8])


def verificar_groq(srv) -> bool:
    passo(3, "Chave do Groq (transcrição)")
    ids, erro = listar_modelos(srv, srv.GROQ_URL, {"Authorization": f"Bearer {srv.CFG['groq_api_key']}"})
    if ids is None:
        explicar_falha_de_api(srv, erro, "o Groq", "groq_api_key",
                              "crie uma chave em https://console.groq.com/keys e cole em groq_api_key")
        return False
    if not any(mesmo_modelo(srv.CFG["stt_model"], i) for i in ids):
        falhou(f"a chave funciona, mas o modelo de transcrição '{srv.CFG['stt_model']}' não existe no Groq.",
               f"troque stt_model por um destes: {sugestoes(ids, 'whisper')}")
        return False
    ok(f"chave aceita e modelo de transcrição '{srv.CFG['stt_model']}' disponível.")
    return True


def verificar_modelo(srv) -> bool:
    passo(4, "Modelo do LLM")
    ids, erro = listar_modelos(srv, srv.LLM_URL, srv.LLM_HEADERS)
    if ids is None:
        dicas = ["no Groq, llm_api_key é a mesma chave de groq_api_key"]
        if srv.LLM_URL != srv.GROQ_URL:  # Ollama ou outro servidor na rede
            dicas = ["se for o Ollama no PC: abra o Ollama com a variável OLLAMA_HOST=0.0.0.0 e libere a porta "
                     "11434 no firewall, só em rede privada",
                     "llm_base_url precisa terminar em /v1, por exemplo http://192.168.0.10:11434/v1"]
        explicar_falha_de_api(srv, erro, f"a API do LLM em {srv.LLM_URL}", "llm_api_key", *dicas)
        return False
    modelo = srv.CFG["llm_model"]
    if not any(mesmo_modelo(modelo, i) for i in ids):
        falhou(f"o modelo '{modelo}' não existe em {srv.LLM_URL}.",
               f"troque llm_model por um destes: {sugestoes(ids, 'qwen')}",
               "o modelo precisa suportar ferramentas (tool calling)")
        return False
    ok(f"modelo '{modelo}' encontrado. Lembre que ele precisa suportar ferramentas (tool calling).")
    return True


def explicar_falha_de_api(srv, erro, servico: str, chave: str, *dicas: str) -> None:
    """`erro` é a exceção de rede, a resposta HTTP com erro ou a falha ao ler a lista."""
    requests = srv.requests
    # Antes da RequestException: o erro de JSON do requests é das duas classes, e aqui a rede funcionou.
    if isinstance(erro, (ValueError, KeyError, TypeError)):
        falhou(f"{servico} respondeu, mas não com a lista de modelos esperada (uma página web, talvez).",
               "confira o endereço; a API precisa terminar em /v1, como https://api.groq.com/openai/v1", *dicas)
    elif isinstance(erro, requests.RequestException):
        if isinstance(erro, requests.Timeout) and not isinstance(erro, requests.ConnectionError):
            falhou(f"{servico} demorou demais para responder.", "tente de novo em alguns minutos", *dicas)
        else:
            falhou(f"sem conexão com {servico}.", "confira a internet do celular (Wi-Fi ou dados móveis)", *dicas)
    elif erro.status_code == 401:
        falhou(f"{servico} recusou a chave em {chave}.", *dicas)
    elif erro.status_code == 404:
        falhou(f"o endereço {erro.url} não existe.", *dicas)
    elif erro.status_code == 429:
        falhou(f"{servico} avisou que o limite de uso acabou por enquanto.", "espere um pouco e rode de novo")
    else:
        falhou(f"{servico} respondeu com erro {erro.status_code}.", "tente de novo em alguns minutos", *dicas)


def pedir_programas(srv):
    """GET /programas no agente: devolve a resposta ou a exceção de rede."""
    try:
        return srv.requests.get(f"{srv.PC_URL}/programas", headers=srv.PC_HEADERS, timeout=5)
    except srv.requests.RequestException as e:
        return e


def verificar_agente(srv, resposta) -> bool:
    passo(5, f"Agente do PC em {srv.PC_URL}")
    if isinstance(resposta, srv.requests.RequestException):
        dicas = ["confira se o agente está aberto no PC (dois cliques em pc_agente/iniciar_agente.bat)",
                 "no PC, rode ipconfig e confira o Endereço IPv4 do Wi-Fi: ele tem que ser o IP em pc_url",
                 REDE_PRIVADA,
                 "o celular e o PC precisam estar na mesma rede Wi-Fi"]
        if urlsplit(srv.PC_URL).hostname == "192.168.0.10":
            dicas.insert(1, "pc_url ainda está com o IP do exemplo (192.168.0.10)")
        if isinstance(resposta, srv.requests.ConnectTimeout):
            falhou("o PC não respondeu: IP errado, PC desligado ou firewall bloqueando.", *dicas)
        else:
            falhou("não consegui conectar ao PC: agente fechado, IP ou porta errados.", *dicas)
        return False
    try:
        dados = resposta.json()
    except ValueError:
        dados = None
    parece_agente = isinstance(dados, dict) and (
        (resposta.status_code == 200 and "programas" in dados)
        or (resposta.status_code == 401 and dados.get("erro") == "token inválido"))
    if not parece_agente:
        falhou(f"algo respondeu em {srv.PC_URL}, mas não parece ser o agente (erro {resposta.status_code}).",
               "confira a porta em pc_url; o padrão do agente é 8765")
        return False
    ok("o agente respondeu.")
    return True


def verificar_token(srv, resposta, agente_ok: bool) -> bool:
    passo(6, "Token do agente")
    if not agente_ok:
        pulado("depende do passo 5.")
        return False
    if resposta.status_code == 401:
        falhou("o agente recusou o pc_token.",
               "copie o token do config_agente.json do PC para pc_token: os dois precisam ser idênticos",
               "no console do agente aparece '[agente] pedido recusado ... token inválido' a cada tentativa")
        return False
    programas = resposta.json()["programas"]
    ok(f"token aceito. Programas liberados: {', '.join(programas)}.")
    return True


def main() -> int:
    print("Diagnóstico da assistente de voz")
    if not verificar_instalacao():
        return NAO_SOBE
    srv = carregar_servidor()
    if srv is None:
        return NAO_SOBE

    problemas = 0
    exemplo = valores_de_exemplo(srv)
    if exemplo:
        arquivo = caminho(srv.ARQUIVO_CONFIG)
        falhou(f"ainda com o valor de exemplo: {', '.join(exemplo)}.",
               f"preencha esses valores em {arquivo} (no Termux: nano {arquivo})")
        problemas += 1
    else:
        ok(f"config_servidor.json válido. O servidor vai abrir em http://localhost:{srv.PORTA}.")

    if "groq_api_key" in exemplo:
        passo(3, "Chave do Groq (transcrição)")
        pulado("preencha groq_api_key primeiro.")
        problemas += 1
    elif not verificar_groq(srv):
        problemas += 1

    if "llm_api_key" in exemplo or "llm_model" in exemplo:
        passo(4, "Modelo do LLM")
        pulado("preencha llm_api_key e llm_model primeiro.")
        problemas += 1
    elif not verificar_modelo(srv):
        problemas += 1

    resposta = pedir_programas(srv)
    agente_ok = verificar_agente(srv, resposta)
    if not agente_ok:
        problemas += 1
    if not verificar_token(srv, resposta, agente_ok):
        problemas += 1

    if problemas:
        print(f"\nEncontrei {problemas} problema(s). Corrija o que está marcado com FALHOU e rode de novo.")
        return COM_PROBLEMAS
    print("\nTudo certo! Suba o servidor com: python servidor.py")
    return TUDO_CERTO


def executar() -> int:
    """main() com códigos próprios para Ctrl+C e erro inesperado, que não podem parecer "problemas"."""
    try:
        return main()
    except KeyboardInterrupt:
        print("\nDiagnóstico interrompido.")
        return INTERROMPIDO
    except Exception as e:
        print(f"\nO diagnóstico parou por um erro inesperado (detalhe técnico: {e!r}).")
        return QUEBROU


if __name__ == "__main__":
    sys.exit(executar())
