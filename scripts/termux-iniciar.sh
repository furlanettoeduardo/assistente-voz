#!/usr/bin/env bash
# Sobe o servidor do celular no Termux: liga o wake lock, roda o diagnóstico e inicia o servidor.
# Na raiz do repositório:
#   bash scripts/termux-iniciar.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ/celular_servidor" || exit 1

if command -v termux-wake-lock > /dev/null 2>&1; then
    termux-wake-lock
    trap 'termux-wake-unlock' EXIT  # solta o wake lock quando o servidor para
    echo "==> Wake lock ligado: o Android não deve derrubar o servidor com a tela apagada."
else
    echo "==> termux-wake-lock não encontrado (fora do Termux?); seguindo sem ele."
fi

echo "==> Rodando o diagnóstico"
python verificar.py
codigo=$?

if [ "$codigo" -eq 2 ]; then
    echo
    echo "O servidor não consegue subir assim. Corrija o que está marcado com FALHOU e rode de novo."
    exit 1
elif [ "$codigo" -ne 0 ]; then
    echo
    echo "O diagnóstico achou problemas (veja acima), mas o servidor consegue subir. Subindo mesmo assim."
fi

echo
echo "==> Subindo o servidor"
python servidor.py
