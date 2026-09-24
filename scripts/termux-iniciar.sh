#!/usr/bin/env bash
# Sobe o servidor do celular no Termux: liga o wake lock, roda o diagnóstico e inicia o servidor.
# Na raiz do repositório:
#   bash scripts/termux-iniciar.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ/celular_servidor" || exit 1

if ! command -v python > /dev/null 2>&1; then
    echo "Não encontrei o Python. Rode antes: bash scripts/termux-instalar.sh"
    exit 1
fi

# O Termux tem um só wake lock para o app inteiro: uma segunda execução deste script não pode
# soltar o lock do servidor que já está rodando em outra sessão.
TRAVA="${TMPDIR:-/tmp}/assistente-voz-servidor.pid"
if [ -f "$TRAVA" ] && kill -0 "$(cat "$TRAVA")" 2> /dev/null; then
    echo "O servidor já está rodando em outra sessão do Termux. Use aquela, ou pare-a com Ctrl+C antes."
    exit 1
fi
echo $$ > "$TRAVA"

soltar() {
    rm -f "$TRAVA"
    if command -v termux-wake-unlock > /dev/null 2>&1; then
        termux-wake-unlock
    fi
}
trap soltar EXIT

if command -v termux-wake-lock > /dev/null 2>&1; then
    termux-wake-lock
    echo "==> Wake lock ligado: o Android não deve derrubar o servidor com a tela apagada."
else
    echo "==> termux-wake-lock não encontrado (fora do Termux?); seguindo sem ele."
fi

echo "==> Rodando o diagnóstico"
python verificar.py
codigo=$?

case "$codigo" in
    0) ;;
    1)
        echo
        echo "O diagnóstico achou problemas (veja acima), mas o servidor consegue subir. Subindo mesmo assim."
        ;;
    2)
        echo
        echo "O servidor não consegue subir assim. Corrija o que está marcado com FALHOU e rode de novo."
        exit 1
        ;;
    130)
        echo
        echo "Diagnóstico interrompido; o servidor não foi iniciado."
        exit 1
        ;;
    *)
        echo
        echo "O diagnóstico não terminou (código $codigo). Veja a mensagem acima e rode de novo."
        exit 1
        ;;
esac

echo
echo "==> Subindo o servidor"
python servidor.py
