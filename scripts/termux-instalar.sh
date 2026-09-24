#!/usr/bin/env bash
# Prepara o Termux para rodar o servidor do celular. Na raiz do repositório:
#   bash scripts/termux-instalar.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVIDOR="$RAIZ/celular_servidor"
CONFIG="$SERVIDOR/config_servidor.json"
# -y não basta: sem essas opções o dpkg ainda pergunta o que fazer com arquivos de configuração alterados.
SEM_PERGUNTAS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

trap 'echo; echo "A instalação parou com erro (veja a mensagem acima). Confira a internet do celular e rode de novo."' ERR

if ! command -v pkg > /dev/null 2>&1; then
    echo "Este script é para o Termux: não encontrei o comando pkg."
    echo "Em outro Linux, instale Python 3.11 ou mais novo, pip e git, e rode:"
    echo "  pip install -r celular_servidor/requirements.txt"
    exit 1
fi

echo "==> Atualizando os pacotes do Termux (pode levar alguns minutos)"
pkg update
pkg upgrade "${SEM_PERGUNTAS[@]}"

echo "==> Instalando python, pip e git"
pkg install "${SEM_PERGUNTAS[@]}" python python-pip git

echo "==> Instalando as bibliotecas do servidor (Flask e requests)"
python -m pip install -r "$SERVIDOR/requirements.txt"

if [ -f "$CONFIG" ]; then
    echo "==> celular_servidor/config_servidor.json já existe; não mexi nele."
else
    cp "$SERVIDOR/config_servidor.example.json" "$CONFIG"
    echo "==> Criei celular_servidor/config_servidor.json a partir do exemplo."
fi

echo
echo "Instalação concluída. Agora:"
echo "  1. Preencha o config:  nano celular_servidor/config_servidor.json"
echo "  2. Suba o servidor:    bash scripts/termux-iniciar.sh"
