# Assistente de voz caseira: fase 0.1a (abrir programas)

Você segura o botão de falar numa página, fala "abre a calculadora" e o programa abre no PC. Por enquanto, tudo roda no mesmo notebook.

O projeto tem duas partes:

- `celular_servidor/`: o servidor, que é o cérebro. Mostra a página com o botão de falar, transcreve o áudio no Groq, consulta o LLM e manda o PC agir. O nome da pasta vem da primeira versão: hoje ele roda no notebook, também roda num celular com Termux e, no futuro, vai rodar num Raspberry Pi.
- `pc_agente/`: roda no computador e abre programas quando o servidor pede. Usa só a biblioteca padrão do Python.

O LLM nunca executa comandos: ele só escolhe um nome da lista de programas do agente, que roda o comando cadastrado sem shell, e todo pedido ao PC exige token.

## Arquitetura

### Protótipo atual: modo notebook (o padrão para testar agora)

O notebook faz os dois papéis: roda o servidor e o agente, e a página abre em `http://localhost:8000` no próprio notebook. No `config_servidor.json`, o `pc_url` fica em `http://127.0.0.1:8765`. Os passos estão em [Como rodar no notebook](#como-rodar-no-notebook-modo-padrão).

```
Notebook
  página em http://localhost:8000 (botão de falar)
  celular_servidor/servidor.py ──▶ Groq: transcrição (Whisper) + LLM Qwen com ferramentas
            │
            └──▶ pc_agente/agente.py em http://127.0.0.1:8765: abre só programas da lista
```

Rodar o servidor num celular Android com Termux continua possível, como alternativa opcional: veja [Rodando no celular](#rodando-no-celular-opcional).

### Visão final

- **Cérebro:** um Raspberry Pi sempre ligado em casa, rodando o mesmo `servidor.py`.
- **Satélites:** um ESP32-S3 com PSRAM por cômodo, com microfone, alto-falante e a palavra de ativação "hey Jarvis" detectada no próprio chip (microWakeWord). Eles falam com o cérebro pelo endpoint `/voz`.
- **PC:** apenas o agente, instalado para rodar em segundo plano.

O cérebro, os satélites, o agente e a lâmpada ficam na rede de casa; só a transcrição e o LLM saem para a internet, no Groq. Não há servidor na nuvem: a lâmpada, o agente e o Wake-on-LAN só existem na rede local, e um servidor exposto na internet, capaz de abrir programas no PC, seria um risco de segurança permanente. O acesso de fora de casa, quando existir, será via Tailscale. As decisões e as próximas fases estão no [roadmap.md](roadmap.md).

## Requisitos

- Python 3.11 ou mais novo no notebook (e no celular, se usar o modo Termux). No Windows, instale pelo python.org: o instalador principal (Python install manager) já deixa os comandos `python` e `py` disponíveis; se usar o instalador tradicional, marque **Add python.exe to PATH**.
- Notebook ou PC com Windows ou Linux.
- Uma chave gratuita da API do Groq.
- Só para o modo celular (opcional): um celular Android na mesma rede Wi-Fi do PC, com o Termux instalado pelo **F-Droid** (a versão da Play Store é experimental e tem recursos faltando).

## Instalação

Clone o repositório no PC:

```
git clone https://github.com/furlanettoeduardo/assistente-voz.git
cd assistente-voz
```

O agente do PC não precisa de nada além do Python. O servidor precisa de Flask e requests, listados em `celular_servidor/requirements.txt`. Para testar o servidor e rodar os testes no PC, use um ambiente virtual:

Windows (PowerShell):

```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r celular_servidor\requirements.txt
```

Se o PowerShell bloquear o `Activate.ps1`, rode antes `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Linux:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r celular_servidor/requirements.txt
```

No Debian e no Ubuntu, instale antes o pacote do venv: `sudo apt install python3-venv`. Fora do ambiente virtual, use `python3` onde este guia diz `python`.

## Configuração

Os arquivos de configuração de verdade guardam chaves e tokens, por isso ficam fora do Git (estão no `.gitignore`, junto com cópias como `config_agente (1).json`). O repositório traz só os modelos `*.example.json`. Copie cada um e preencha:

Windows:

```
copy pc_agente\config_agente.example.json pc_agente\config_agente.json
copy celular_servidor\config_servidor.example.json celular_servidor\config_servidor.json
```

Linux ou Termux:

```
cp pc_agente/config_agente.example.json pc_agente/config_agente.json
cp celular_servidor/config_servidor.example.json celular_servidor/config_servidor.json
```

Salve os arquivos em UTF-8 (o padrão do Bloco de Notas e do VS Code). Se um config faltar ou tiver algum valor errado, o programa encerra dizendo o que corrigir.

### Agente do PC: `pc_agente/config_agente.json`

| Chave | O que é |
|---|---|
| `token` | Segredo compartilhado com o servidor, sem acentos. O agente não inicia com o token vazio ou com o valor de exemplo. |
| `porta` | Porta em que o agente escuta. Padrão: `8765`. |
| `programas` | Nome falado → comando que o PC executa, como lista de strings. Precisa ter pelo menos um programa. Maiúsculas e espaços extras no nome não importam. |

1. Só se o servidor for rodar em outro aparelho (modo celular): [descubra o IP do PC](#1-descubra-o-ip-do-pc) e reserve-o no roteador (reserva de DHCP), para ele não mudar. Por exemplo, `192.168.0.10`. No modo notebook, pule este passo.
2. Gere um token e cole em `token`:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
3. Ajuste a lista de programas. Os nomes são o que você vai falar; o comando é o que o PC executa.

Exemplos no Linux: `["firefox"]`, `["code"]`, `["spotify"]`, `["nautilus"]`.

No Windows, `["cmd", "/c", "start", "", "nome"]` funciona para a maioria dos programas instalados. Nesse formato, um `&` dentro de uma URL precisa virar `^&`. Para caminhos completos, dobre as barras invertidas, que é como o JSON exige: `["C:\\Program Files\\Pasta\\programa.exe"]`. Com barra simples, o JSON dá erro ou, pior, transforma `\t` e `\n` em outros caracteres e o caminho não abre.

### Servidor: `celular_servidor/config_servidor.json`

| Chave | O que é |
|---|---|
| `groq_api_key` | Chave do Groq usada na transcrição. |
| `stt_model` | Modelo de transcrição. No modelo: `whisper-large-v3-turbo`. |
| `llm_base_url` | Endereço da API do LLM, compatível com OpenAI. No modelo: Groq. |
| `llm_api_key` | Chave do LLM. No Groq, é a mesma de `groq_api_key`. |
| `llm_model` | Nome exato do modelo Qwen, como aparece em [console.groq.com/docs/models](https://console.groq.com/docs/models). Precisa suportar tool calling. |
| `pc_url` | Endereço do agente, com `http://`, IP e porta. No modo notebook, `http://127.0.0.1:8765`; com o servidor em outro aparelho, o IP do PC na rede, como `http://192.168.0.10:8765`. |
| `pc_token` | O mesmo valor de `token` do agente. |
| `host` e `porta` | Onde o servidor escuta. Padrão: `127.0.0.1:8000`, só o próprio aparelho acessa. |

Em setembro de 2026, o Qwen disponível no Groq é o `qwen/qwen3.8-27b`, na categoria Preview. Modelos Preview trocam de nome ou saem do ar com pouco aviso, então confira a lista antes de preencher.

Para usar Ollama no lugar do Groq, troque `llm_api_key` para `ollama`, `llm_model` para o nome de um modelo com suporte a ferramentas e `llm_base_url` para o endereço do Ollama. No modo notebook, com o Ollama na mesma máquina, use `http://127.0.0.1:11434/v1`; não precisa mexer em mais nada. Com o servidor em outro aparelho, use `http://IP-DO-PC:11434/v1`: como por padrão o Ollama só aceita conexões do próprio PC, defina a variável de ambiente `OLLAMA_HOST=0.0.0.0`, reinicie o Ollama e libere a porta 11434 no firewall, só em redes privadas. A transcrição continua no Groq.

## Como rodar no notebook (modo padrão)

Este é o modo para testar agora: servidor e agente no mesmo notebook.

### 1. Agente

No Windows, dê dois cliques em `pc_agente\iniciar_agente.bat`. Ele acha o Python, inicia o agente e deixa a janela aberta: feche a janela ou aperte Ctrl+C para parar (o agente mostra `[agente] encerrado.`). Se aparecer um erro, a janela espera uma tecla antes de fechar, para dar tempo de ler.

No terminal, em qualquer sistema:

```
cd pc_agente
python agente.py
```

Na primeira vez, o firewall do Windows pergunta se libera o Python: permita **apenas em redes privadas**. No modo notebook o servidor fala com o agente por `127.0.0.1`, dentro da própria máquina, então o perfil da rede não atrapalha. Ele só importa quando outro aparelho precisa alcançar o agente, como o celular no modo opcional (veja [como deixar a rede como privada](#2-deixe-a-rede-wi-fi-do-windows-como-privada)).

Depois de mudar o `config_agente.json`, feche o agente e abra de novo.

### 2. Servidor no mesmo notebook

No `config_servidor.json`, troque o `pc_url` do exemplo por `"pc_url": "http://127.0.0.1:8765"`, para o servidor falar com o agente na própria máquina. Depois, com o ambiente virtual ativado:

```
cd celular_servidor
python verificar.py
python servidor.py
```

O `verificar.py` confere tudo o que o servidor precisa (veja [Diagnóstico](#diagnóstico)). Depois, abra `http://localhost:8000` no navegador do notebook. Comece pelo campo de texto ("abre a calculadora"); depois teste o botão de voz.

## Diagnóstico

Para rodar só o diagnóstico, a qualquer hora:

```
cd celular_servidor
python verificar.py
```

Ele confere, em ordem, e explica em português o que falhou e como resolver:

1. Python 3.11 ou mais novo, com Flask e requests instalados;
2. `config_servidor.json` existe, é válido e não tem mais valores de exemplo;
3. a chave do Groq funciona e o modelo de transcrição existe;
4. o modelo em `llm_model` existe na API configurada;
5. o agente do PC responde em `pc_url`;
6. o agente aceita o `pc_token`.

Para testar as chaves, ele só pede a lista de modelos da API: não grava áudio nem gasta tokens. Ele termina com código 0 quando está tudo certo, 1 quando há problemas mas o servidor consegue subir, 2 quando o servidor nem sobe, 3 quando o próprio diagnóstico quebra e 130 quando é interrompido com Ctrl+C. As dicas mostram o caminho completo dos arquivos, então funcionam de qualquer pasta.

Algumas mensagens do diagnóstico e do servidor ainda falam em celular e Termux, da época em que o servidor rodava no celular. No modo notebook, leia "celular" como o aparelho que roda o servidor.

## Rodando no celular (opcional)

Alternativa ao modo notebook: o servidor roda no Termux e fala com o agente do PC pela rede Wi-Fi. Deixe o agente aberto no PC antes de começar.

### 1. Descubra o IP do PC

No PC com Windows, abra o PowerShell ou o Prompt de Comando e rode:

```
ipconfig
```

Procure a seção **Adaptador de Rede sem Fio Wi-Fi** e, dentro dela, a linha **Endereço IPv4**:

```
Adaptador de Rede sem Fio Wi-Fi:
   ...
   Endereço IPv4. . . . . . . .  . . . . . . . : 192.168.0.10
```

Esse é o IP que vai em `pc_url` (`http://192.168.0.10:8765`). Ignore as outras seções com "Endereço IPv4", como a `vEthernet (WSL ...)`, que usam IPs internos (172.x). Se o PC estiver no cabo, use a seção **Adaptador Ethernet Ethernet**. No Linux, `hostname -I` mostra o IP.

Para o IP não mudar, reserve-o no roteador (reserva de DHCP).

### 2. Deixe a rede Wi-Fi do Windows como "Privada"

> **Importante:** o Windows 11 marca toda rede nova como **Rede pública**, e nesse perfil o firewall bloqueia o celular, mesmo com o Python liberado. A rede Wi-Fi de casa precisa estar como **Rede privada** para o celular alcançar o agente.

Em Configurações > Rede e Internet > Wi-Fi > Propriedades da sua rede, em **Tipo de perfil de rede** (em algumas versões, "Tipo de perfil da rede"), escolha **Rede privada**.

Para conferir pelo PowerShell, rode `Get-NetConnectionProfile`: a linha `NetworkCategory` deve dizer `Private`. Para trocar pelo PowerShell aberto como administrador:

```
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
```

### 3. Instale no Termux

Instale o Termux pelo **F-Droid** e, dentro dele:

```
pkg install git
git clone https://github.com/furlanettoeduardo/assistente-voz.git
cd assistente-voz
bash scripts/termux-instalar.sh
```

O `termux-instalar.sh` atualiza os pacotes do Termux, instala `python`, `python-pip` e `git`, instala as bibliotecas do `requirements.txt` e cria o `celular_servidor/config_servidor.json` a partir do exemplo, se ele ainda não existir. Pode levar alguns minutos.

O repositório é público, então o `git clone` não pede senha. Se um dia ele virar privado, o Git vai pedir o usuário do GitHub e, no lugar da senha, um token de acesso pessoal.

### 4. Preencha o config

```
nano celular_servidor/config_servidor.json
```

Preencha `groq_api_key`, `llm_api_key` e `llm_model` (veja a [tabela do servidor](#servidor-celular_servidorconfig_servidorjson)), `pc_url` com o IP do passo 1 e `pc_token` com o mesmo `token` do `config_agente.json` do PC. No nano, Ctrl+O e Enter salvam, e Ctrl+X sai. O Ctrl fica na fileira de teclas extras do Termux.

### 5. Suba o servidor

```
bash scripts/termux-iniciar.sh
```

O script liga o `termux-wake-lock` (para o Android não derrubar o servidor com a tela apagada), roda o diagnóstico e sobe o servidor. Se o diagnóstico achar um problema que impede o servidor de subir, como um config inválido, ele para e mostra o que corrigir. Se o problema não impede, como o PC desligado, ele avisa e sobe mesmo assim. Se o servidor já estiver rodando em outra sessão do Termux, ele não sobe um segundo.

Quando aparecer `[servidor] pronto`, abra o Chrome do celular em **http://localhost:8000** e permita o microfone. Use sempre `localhost`: o Chrome guarda a permissão do microfone por endereço, e `127.0.0.1` conta como outro site.

Para parar, aperte Ctrl+C no Termux; o wake lock é solto junto.

Nas configurações do Android, desative a otimização de bateria para o Termux. No Android 12 ou mais novo, o sistema ainda pode encerrar o servidor (`[Process completed (signal 9)]`); veja as soluções na [discussão do Termux sobre o "phantom process killer"](https://github.com/termux/termux-app/issues/2366).

### Atualizar o celular

```
cd ~/assistente-voz
git pull
bash scripts/termux-instalar.sh
```

O `git pull` não mexe no seu `config_servidor.json`, que fica fora do Git. Rodar o `termux-instalar.sh` de novo só é preciso quando o `requirements.txt` mudar, mas não faz mal.

## Testes

Na raiz do repositório, com o ambiente virtual ativado:

```
python -m unittest -v
```

Os testes não chamam nenhuma API real. O LLM, a transcrição e a lista de modelos são um servidor HTTP falso em `127.0.0.1`, e o agente roda de verdade numa porta livre, também em `127.0.0.1`, então o firewall não é acionado. Eles cobrem:

- token errado, ausente ou com acento recusado pelo agente, que também não inicia com token vazio ou de exemplo;
- programa fora da lista recusado, inclusive quando o LLM inventa um nome ou uma ferramenta;
- fluxo completo: texto ou voz → LLM com tool call → agente → programa aberto. O "programa" de teste é o próprio Python criando um arquivo temporário, e o teste confere que ele foi chamado sem shell;
- a última rodada de ferramenta só aceita texto, e a mesma chamada repetida não abre o programa de novo;
- remoção dos blocos `<think>` da resposta, inclusive sem abertura ou sem fechamento;
- mensagens em português para erros da API, queda da rede, PC desligado, servidor fora do ar e configuração ausente ou com valor errado;
- o diagnóstico `verificar.py`, os scripts do Termux e o `iniciar_agente.bat`.

Os testes copiam o código para uma pasta temporária com configs próprios, então nunca leem nem alteram os seus `config_*.json`. Passam no Windows e no Linux. Alguns são de uma plataforma só e aparecem como `skipped` nas outras: o `.bat` só roda no Windows; os scripts do Termux e os dois testes que sobem o agente em `0.0.0.0` (Ctrl+C e porta ocupada), só no Linux, para não acionar o firewall do Windows; o JavaScript da página, só com o Node instalado. Sem Flask, os testes do servidor e do diagnóstico também aparecem como `skipped`: instale o `requirements.txt` para rodá-los.

## Solução de problemas

Comece pelo diagnóstico: `python verificar.py` na pasta `celular_servidor`. O terminal do servidor também explica cada falha; quando ele mostra um "detalhe técnico", é o texto original da API ou do sistema, em inglês, para ajudar a achar a causa.

### Configuração

- **"Arquivo de configuração não encontrado"**: copie o `.example.json` da mesma pasta, como em [Configuração](#configuração).
- **"Erro de JSON em ..., linha X, coluna Y"**: o motivo vem logo depois (vírgula sobrando ou faltando, aspas abertas, barra invertida simples). Em JSON, o caminho `C:\Pasta` do Windows se escreve `C:\\Pasta`.
- **"Faltam chaves em ..."**: o config não tem todas as chaves do `.example.json`. Compare os dois e copie as que faltam.
- **"... precisa ser um texto entre aspas"**, **"... precisa ser um objeto entre chaves"** ou **'"porta" precisa ser um número'**: o valor está com o formato errado. Compare com o `.example.json`.
- **'"pc_url" precisa ser um endereço completo'** (ou `"llm_base_url"`): escreva o endereço inteiro, com `http://` ou `https://`, host e porta, como `http://192.168.0.10:8765`.
- **"... está vazio"** ou **"... tem um caractere que não pode ir numa chave"**: a chave ou o token ficou em branco, ou veio com aspas curvas (“ ”) ou um espaço invisível ao colar. Apague o valor e cole de novo a partir da fonte original.
- **"Cadastre pelo menos um programa"** ou **"o comando de ... precisa ser uma lista de textos"**: confira a lista `programas` do agente, que usa o formato `"calculadora": ["calc.exe"]`.
- **"... não está em UTF-8"**: o arquivo foi salvo em ANSI ou UTF-16. Abra no Bloco de Notas, vá em Salvar como e escolha a codificação UTF-8.
- **"Defina um token próprio"**: o `token` do agente está vazio, com acento ou ainda com o valor de exemplo. Gere um novo com o comando da [configuração do agente](#agente-do-pc-pc_agenteconfig_agentejson).

### Servidor e agente

- **A assistente diz que o computador está desligado ou inacessível** (o texto exato varia, porque é o LLM que escreve): o servidor não conseguiu a lista de programas. O terminal do servidor diz o motivo, e o console do agente no PC ajuda:
  - se o agente mostra `[agente] pedido recusado de ...: token inválido`, a rede está certa e o `pc_token` do servidor está diferente do `token` do agente;
  - se o agente não mostra nada, o pedido nem chegou até ele. No modo notebook, confira se o agente está aberto e se o `pc_url` é `http://127.0.0.1:8765`. Com o servidor em outro aparelho, confira também se o IP em `pc_url` é o do `ipconfig`, se a [rede do Windows está como privada](#2-deixe-a-rede-wi-fi-do-windows-como-privada) e se o firewall liberou o Python. No Linux com firewall ativo, libere a porta: `sudo ufw allow 8765/tcp`.

  Para testar só a conexão, abra no navegador `http://127.0.0.1:8765/programas` (modo notebook) ou `http://IP-DO-PC:8765/programas` (no Chrome do celular). A resposta `{"erro": "token inválido"}` prova que o pedido chega até o agente (o navegador não manda token).
- **Cancelei o aviso do firewall do Windows**: procure "Permitir um aplicativo pelo Firewall do Windows" no menu Iniciar e marque o Python na coluna Privada.
- **A página diz "Abriu", mas nada abriu no PC**: com `cmd /c start`, "Abriu" só quer dizer que o comando foi disparado. Rode o mesmo comando no terminal do PC (por exemplo `cmd /c start "" chrome`) para ver o erro.
- **"Não abriu: nome (programa 'nome' não está na lista)"**: o LLM escolheu um nome que não existe em `programas`. Fale o nome como está no config.
- **"Não abriu: nome (não encontrei '...' no PC ...)"**: o comando cadastrado para esse programa não existe no PC. Corrija o comando no `config_agente.json`.
- **"Não abriu: nome (não consegui falar com o computador)"**: o PC saiu da rede no meio do comando, ou o agente foi fechado.
- **"Não consegui escutar na porta 8765"**: a mensagem diz o motivo. "Outro agente (ou outro programa) já usa essa porta": feche a outra janela do agente. "O sistema não deixou usar essa porta": no Windows, algumas portas ficam reservadas pelo sistema (Hyper-V, WSL); troque `porta` no config e em `pc_url`, por exemplo para 8766.

### API do Groq

- **"... recusou a chave"**: a chave em `groq_api_key` (transcrição) ou `llm_api_key` (LLM) está errada ou foi revogada. Crie outra em [console.groq.com/keys](https://console.groq.com/keys).
- **"... respondeu, mas não com a lista de modelos esperada"** (no diagnóstico): o endereço aponta para uma página web, não para a API. Confira `llm_base_url`, que precisa terminar em `/v1`.
- **"... não encontrou o modelo ou o endereço"**: o nome em `llm_model` está diferente do que a API lista, ou falta o `/v1` no fim de `llm_base_url` (acontece ao configurar o Ollama). O `verificar.py` sugere os nomes certos.
- **"... avisou que o modelo configurado saiu do ar"**: escolha outro Qwen na lista de modelos do Groq.
- **"O modelo se confundiu ao usar a ferramenta"**: repita o pedido; se acontecer sempre, troque de modelo.
- **"... avisou que o limite de uso acabou por enquanto"**: estourou o limite do plano gratuito. Espere um pouco e tente de novo.
- **"... achou o pedido grande demais"**: fale um comando mais curto.
- **"Sem conexão com a API ..."**: o aparelho que roda o servidor está sem internet (a mensagem fala em celular; no modo notebook, é o notebook). Com Ollama, confira se ele está aberto; com o servidor em outro aparelho, confira também `OLLAMA_HOST=0.0.0.0` e se o firewall liberou a porta 11434.
- **A transcrição mostra frases que você não disse** (por exemplo "Legendas pela comunidade Amara.org"): o Whisper inventa texto quando o áudio sai quase mudo. Segure o botão durante toda a fala e fale mais perto do microfone.
- **Aparece texto de raciocínio na resposta**: o servidor remove os blocos `<think>`. Se ainda aparecer, o backend usa outro formato; com Ollama, atualize para uma versão recente.

### Página, Termux e instalação

- **"Sem conexão com o servidor do celular"** na página: o servidor parou (a mensagem fala em celular, mas vale para qualquer aparelho). No modo notebook, veja o erro no terminal onde você rodou `python servidor.py` e rode de novo; no celular, rode `bash scripts/termux-iniciar.sh` de novo.
- **"A instalação parou com erro"**: veja a mensagem logo acima (normalmente é falta de internet) e rode `bash scripts/termux-instalar.sh` de novo.
- **"Endereço não encontrado no servidor"** ao abrir a página: a pasta `static` com o `index.html` precisa estar dentro de `celular_servidor`. Com `git clone`, ela já vem.
- **Microfone não liga**: abra a página como `http://localhost:8000` no próprio aparelho que roda o servidor (o notebook, no modo padrão); o navegador só libera o microfone em `localhost` ou HTTPS. No celular, na primeira vez o Android também pede a permissão de microfone para o Chrome.
- **No celular, o servidor para com a tela apagada**: use o `termux-iniciar.sh`, que liga o wake lock, e desative a otimização de bateria do Termux.
- **"Não consegui escutar em 127.0.0.1:8000"**: a mensagem diz o motivo. "Outro servidor já usa essa porta": feche a outra sessão do Termux ou troque `porta` no config. "Não é um endereço deste aparelho": deixe `host` como `127.0.0.1`. "O sistema não deixou usar essa porta": use uma porta acima de 1024; testando no PC com Windows, a porta pode estar reservada pelo Hyper-V ou pelo WSL, então troque para outra, como 8001.
- **"O servidor já está rodando em outra sessão do Termux"**: use a sessão que já está aberta ou pare o servidor dela com Ctrl+C antes de rodar o `termux-iniciar.sh` de novo.
- **"Não encontrei o Python. Rode antes: bash scripts/termux-instalar.sh"** ou **"Falta a biblioteca ..."**: a instalação não foi feita (ou, no PC, o `.venv` não está ativado). Rode o que a mensagem pede.
- **"O diagnóstico não terminou"** ou **"O diagnóstico parou por um erro inesperado"**: o `verificar.py` quebrou antes do fim; o detalhe técnico vem na mensagem. O `termux-iniciar.sh` não sobe o servidor nesse caso.
- **`pip: command not found` no Termux**: o pip é um pacote separado; rode `pkg install python-pip` ou o `termux-instalar.sh`.
- **"WARNING: The C extension could not be compiled" ao instalar no Termux**: é o MarkupSafe (dependência do Flask) sem compilador. Ele instala a versão em Python puro e funciona normalmente.
- **"Não encontrei o Python neste PC"** (no `.bat`) ou **"Python não foi encontrado; executar sem argumentos para instalar do Microsoft Store"**: o Python não está instalado ou não está no PATH. Instale pelo python.org, como em [Requisitos](#requisitos).
- **Acentos aparecem estranhos no terminal do Windows (Git Bash)**: use o PowerShell ou defina a variável `PYTHONUTF8=1`.
- **Nos testes, tudo do servidor aparece como `skipped`**: o ambiente não tem Flask e requests. Ative o `.venv` e instale o `requirements.txt`.
