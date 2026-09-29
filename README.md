# Assistente de voz caseira: fase 0.2 (mais ferramentas)

Você segura o botão de falar numa página e pede: "abre a calculadora", "fecha o chrome", "volume em 30", "acende a luz", "vai chover amanhã?" ou "toca Legião Urbana". Por enquanto, tudo roda no mesmo notebook. A lista completa está em [O que dá para pedir](#o-que-dá-para-pedir).

O projeto tem duas partes:

- `celular_servidor/`: o servidor, que é o cérebro. Mostra a página com o botão de falar, transcreve o áudio no Groq, consulta o LLM, manda o PC agir, controla a lâmpada Tuya pela rede de casa, consulta a previsão do tempo e toca música no Spotify. O nome da pasta vem da primeira versão: hoje ele roda no notebook, também roda num celular com Termux e, no futuro, vai rodar num Raspberry Pi.
- `pc_agente/`: roda no computador e, quando o servidor pede, abre e fecha programas, muda o volume, bloqueia a tela e desliga o PC. Usa só a biblioteca padrão do Python.

O LLM nunca executa comandos: ele só escolhe nomes das listas do agente (programas para abrir e para fechar) e ações fixas (volume, bloquear e desligar, se liberadas). O agente roda o comando cadastrado sem shell, e todo pedido ao PC exige token. Desligar o PC sempre pede confirmação por voz.

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

O cérebro, os satélites, o agente e a lâmpada ficam na rede de casa. Saem para a internet só a transcrição e o LLM (no Groq), a cidade da previsão do tempo (no Open-Meteo) e, se você configurar o Spotify, as buscas e os comandos de música. Não há servidor na nuvem: a lâmpada, o agente e o Wake-on-LAN só existem na rede local, e um servidor exposto na internet, capaz de abrir programas no PC, seria um risco de segurança permanente. O acesso de fora de casa, quando existir, será via Tailscale. As decisões e as próximas fases estão no [roadmap.md](roadmap.md).

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

O agente do PC não precisa de nada além do Python. O servidor precisa de Flask, requests e tinytuya (este para a lâmpada), listados em `celular_servidor/requirements.txt`. Para testar o servidor e rodar os testes no PC, use um ambiente virtual:

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
| `fechar` | Opcional. Nome falado → nome do processo que a assistente pode fechar, como `"chrome": "chrome.exe"`. |
| `acoes` | Opcional. O que mais o servidor pode pedir: `"volume"`, `"bloquear"` (a tela) e `"desligar"`. Sem a chave, valem `volume` e `bloquear`. |

1. Só se o servidor for rodar em outro aparelho (modo celular): [descubra o IP do PC](#1-descubra-o-ip-do-pc) e reserve-o no roteador (reserva de DHCP), para ele não mudar. Por exemplo, `192.168.0.10`. No modo notebook, pule este passo.
2. Gere um token e cole em `token`:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
3. Ajuste a lista de programas. Os nomes são o que você vai falar; o comando é o que o PC executa.

Exemplos no Linux: `["firefox"]`, `["code"]`, `["spotify"]`, `["nautilus"]`.

4. Opcional: em `fechar`, cadastre os programas que a assistente pode fechar. No Windows, o processo é o nome com `.exe` que aparece no Gerenciador de Tarefas, na aba Detalhes (na aba Processos, clique com o botão direito no programa e escolha "Ir para detalhes"). No Linux, é o nome inteiro do programa, sem a pasta, como aparece em `ps -eo args`. O `explorer.exe` (a barra de tarefas e a área de trabalho) e os processos do próprio Windows são recusados. No Windows, a assistente pede para o programa fechar como se você clicasse no X: se ele tiver algo por salvar, ele pergunta, e ela avisa que ele continua aberto. No Linux, o programa recebe o sinal de encerrar (SIGTERM), e a maioria fecha sem perguntar: salve antes.
5. Opcional: em `acoes`, libere o volume, o bloqueio da tela e o desligamento. `desligar` só funciona se estiver na lista, e o PC desliga 30 segundos depois do "sim" (no Linux, 1 minuto), fechando os programas sem perguntar: salve o que estiver aberto. Um config antigo, sem `acoes`, passa a liberar `volume` e `bloquear` quando você atualiza o agente.

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
| `pc_mac` e `pc_broadcast` | Opcionais. Para ligar o PC pela rede, como explica [Wake-on-LAN](#ligar-o-pc-pela-rede-wake-on-lan-opcional). Vazios, a assistente não liga o PC. |
| `host` e `porta` | Onde o servidor escuta. Padrão: `127.0.0.1:8000`, só o próprio aparelho acessa. |
| `cidade` | Opcional. A cidade da [previsão do tempo](#previsão-do-tempo) quando você não diz outra, como `"Curitiba, PR"`. |
| `lampada` | Opcional. `id`, `chave_local`, `ip` e `versao` da lâmpada Tuya, como explica [Lâmpada](#lâmpada-opcional). Sem lâmpada, apague o bloco. |
| `spotify` | Opcional. `client_id` do app do Spotify e, se quiser, `dispositivo`, como explica [Spotify](#spotify-opcional-conta-premium). |

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

## Lâmpada (opcional)

O servidor controla uma lâmpada Tuya, como a Elgin Smart Color, direto pela rede de casa, com a biblioteca `tinytuya`: os comandos não passam pela nuvem nem pelo app. Dá para falar "acende a luz", "apaga a luz", "deixa a luz azul" ou "diminui a luz para 30%". As cores são branco (o branco puro), branco neutro, branco quente (amarelado), vermelho, laranja, amarelo, verde, ciano, azul, roxo e rosa; o brilho vai de 1% a 100%. Sem lâmpada configurada, o resto funciona normalmente.

Para falar com a lâmpada, o servidor precisa de quatro dados dela: o `id`, a `chave_local` (a senha que ela usa na rede), o `ip` e a `versao` do protocolo. Se você instalou o servidor antes desta versão, instale de novo o `requirements.txt`, que agora traz o `tinytuya`. Os comandos abaixo usam o Python do ambiente virtual: ative o `.venv` antes (ou chame o Python dele direto, como `.venv\Scripts\python.exe -m tinytuya wizard`).

### 1. Pareie a lâmpada no app

Pareie a lâmpada no app **Smart Life** (ou Tuya Smart) e confira que ela liga e desliga pelo app.

### 2. Pegue o id e a chave local (uma vez)

A chave local só sai da nuvem da Tuya, então é preciso uma conta gratuita na plataforma de desenvolvedor dela. Ela serve só para buscar a chave: depois disso, o controle é todo local.

1. Crie uma conta em [platform.tuya.com](https://platform.tuya.com) e, em **Cloud → Development**, crie um projeto (**Create Cloud Project**) com o data center **Western America**, onde ficam as contas do Smart Life no Brasil. O projeto vem com o **IoT Core** em teste gratuito de 1 mês, que se renova de graça. Ele só precisa estar ativo quando você rodar o wizard: com o teste vencido, a lâmpada continua funcionando pelo servidor.
2. No projeto, abra **Devices → Link App Account → Add App Account** e leia o QR code com o app Smart Life (aba **Eu**, ícone de leitura no canto de cima). A lâmpada aparece na lista de aparelhos do projeto.
3. Rode o wizard na raiz do repositório:
   ```
   python -m tinytuya wizard
   ```
   Ele pede a **Access ID** e o **Access Secret** (em **Overview**, no projeto), o ID de algum aparelho da conta (a lista de aparelhos do projeto mostra) e a região: responda `us`. As outras perguntas podem ficar no padrão.

   Se ele não achar a lâmpada, tente a região `us-e`: acrescente o data center **Eastern America** ao projeto e rode o wizard de novo. Na segunda vez ele mostra os dados salvos e pergunta **Use existing credentials**: responda `n` e digite tudo outra vez, agora com `us-e`, senão ele repete a região `us`.

O wizard grava o `devices.json` na pasta onde rodou, com o `id` e a `key` de cada aparelho. Se a lâmpada estiver desligada ou você estiver fora de casa, ele avisa "No IP found": não tem problema, o IP vem no próximo passo. O `devices.json` e os outros arquivos que o wizard grava (`tinytuya.json`, `tuya-raw.json` e `snapshot.json`) guardam segredos: já estão no `.gitignore` e não devem ser enviados a ninguém.

Se um dia a lâmpada for pareada de novo no app (depois de um reset, por exemplo), a chave muda: rode o wizard outra vez.

### 3. Descubra o IP e a versão (em casa)

Com a lâmpada ligada e o PC na mesma rede Wi-Fi que ela, rode:

```
python -m tinytuya scan
```

A lâmpada aparece com `Address` (o IP) e `Version` (3.3, 3.4 ou 3.5). O scan escuta os anúncios que ela manda pela rede, nas portas UDP 6666, 6667 e 7000. No Windows, a [rede precisa estar como privada](#2-deixe-a-rede-wi-fi-do-windows-como-privada) e o Python liberado no firewall em redes privadas, senão o firewall bloqueia esses anúncios; no Linux com o ufw ativo, libere as portas com `sudo ufw allow 6666:6667/udp` e `sudo ufw allow 7000/udp`. Reserve esse IP para a lâmpada no roteador (reserva de DHCP), para ele não mudar.

### 4. Preencha o bloco da lâmpada

Acrescente o bloco `lampada` ao seu `config_servidor.json`, como no `.example.json`: `id` e `chave_local` são o `id` e a `key` do `devices.json`; `ip` e `versao` são o `Address` e a `Version` do scan. Por exemplo (valores inventados):

```
  "porta": 8000,

  "lampada": {
    "id": "eb1234567890abcdef12",
    "chave_local": "0123456789abcdef",
    "ip": "192.168.0.20",
    "versao": "3.3"
  }
}
```

Repare na vírgula depois da linha anterior ao bloco. Depois, na pasta `celular_servidor`, rode o diagnóstico, que testa a lâmpada no passo 7, e suba o servidor de novo:

```
cd celular_servidor
python verificar.py
python servidor.py
```

Ao subir, o servidor mostra a linha `[servidor] lâmpada em 192.168.0.20 (protocolo 3.3)`. Enquanto faltar algum dado, ele sobe sem a lâmpada e diz o que falta.

Deixe o interruptor da lâmpada sempre ligado: desligada no interruptor, ela sai da rede, e a assistente avisa que a lâmpada não respondeu.

No [modo celular](#rodando-no-celular-opcional), o servidor fala com a lâmpada do mesmo jeito, desde que o celular esteja na mesma rede Wi-Fi que ela.

## O que dá para pedir

| Pedido | O que acontece |
|---|---|
| "abre o chrome", "fecha o chrome" | Abre ou fecha o programa. Fechar exige o programa em `fechar` no config do agente. |
| "volume em 30", "aumenta o volume", "qual o volume?", "muta o PC" | Muda ou consulta o volume do PC (exige `volume` em `acoes`). |
| "bloqueia o PC" | Bloqueia a tela (exige `bloquear` em `acoes`). |
| "desliga o PC", e depois "sim" | Pergunta antes e desliga em 30 segundos (exige `desligar` em `acoes`). "Cancela o desligamento" cancela. |
| "que horas são?", "que dia é hoje?" | Responde com a data e a hora do aparelho que roda o servidor. |
| "vai chover amanhã?", "como está o tempo em Salvador?" | Consulta a [previsão do tempo](#previsão-do-tempo). |
| "acende a luz", "deixa a luz azul", "diminui a luz para 30%" | Controla a [lâmpada](#lâmpada-opcional). |
| "toca Legião Urbana", "toca a playlist Rock Nacional", "pausa a música", "próxima" | Toca no [Spotify](#spotify-opcional-conta-premium) do PC. |

A assistente só oferece ao LLM o que funciona naquele momento: sem o PC ligado, por exemplo, ela não tenta abrir programas e avisa que ele está desligado.

A confirmação do desligamento não passa pelo LLM: a pergunta é sempre a mesma ("Quer mesmo desligar o PC? Diga sim para confirmar."), e só um "sim" curto ("sim", "pode", "confirmo", "sim, pode desligar"), dito nos 30 segundos seguintes, desliga o PC. Um "não" curto ("não", "cancela", "deixa pra lá") recusa; qualquer outro pedido descarta a pergunta e é atendido normalmente.

## Previsão do tempo

A previsão vem do Open-Meteo, gratuito e sem chave. Preencha `"cidade"` no `config_servidor.json` no formato `"Cidade, UF"` (por exemplo `"Curitiba, PR"`) para não precisar dizer a cidade toda vez; dá para perguntar de outra cidade a qualquer momento. A previsão vai de hoje até daqui a 6 dias.

Os dados são de [Open-Meteo.com](https://open-meteo.com/), sob a licença [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.pt-br). A assistente traduz os códigos do tempo para o português e arredonda as temperaturas, e a página mostra o crédito ao lado de cada previsão.

## Spotify (opcional, conta Premium)

O servidor procura a música pela API do Spotify e manda tocar no aplicativo aberto no PC. Só funciona com Premium: o Spotify só deixa controlar a reprodução assim, e o dono do app precisa ser Premium.

### 1. Crie o app no painel do Spotify (uma vez)

1. Entre em [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) com a sua conta e clique em **Create app**.
2. Preencha o nome e a descrição. Em **Redirect URIs**, escreva exatamente `http://127.0.0.1:8888/callback` e clique em **Add** (o Spotify não aceita mais `localhost`).
3. Marque só **Web API**, aceite os termos e salve.
4. Em **Settings**, copie o **Client ID**. O Client Secret não é usado.

### 2. Preencha o config

No `config_servidor.json`:

```
  "spotify": {
    "client_id": "O-CLIENT-ID-QUE-VOCÊ-COPIOU",
    "dispositivo": ""
  }
```

`dispositivo` é opcional: o nome do PC como aparece no Spotify, em "Conectar a um dispositivo". Preenchido, só esse aparelho vale; vazio, vale o primeiro computador da lista. A música vai sempre para o PC, mesmo que o Spotify esteja tocando no celular ou na TV.

### 3. Conecte a sua conta (uma vez)

Na pasta `celular_servidor`, com o ambiente virtual ativado:

```
python spotify_conectar.py
```

O navegador abre na página do Spotify: autorize, e o terminal mostra "Spotify conectado!". O script grava o `spotify_token.json`, que dá acesso à sua conta: ele está no `.gitignore` e não deve ser enviado a ninguém. Se o navegador estiver em outro aparelho (no Raspberry Pi, por exemplo), rode `python spotify_conectar.py --colar` e cole o endereço para onde o Spotify redirecionou.

Suba o servidor de novo: ele mostra `[servidor] Spotify conectado.`, e o `verificar.py` confere a conexão no passo 9.

A autorização dura 6 meses; depois, rode o `spotify_conectar.py` de novo. Se o Spotify estiver fechado no PC e `"spotify"` estiver na lista de programas do agente, a assistente abre o aplicativo e tenta de novo por uns 15 segundos. Para usar outra conta além da dona do app, adicione-a antes em **Settings → Users Management** (até 5 contas).

## Ligar o PC pela rede (Wake-on-LAN, opcional)

Só faz sentido com o servidor em outro aparelho (o celular ou, no futuro, o Raspberry Pi): no modo notebook, o servidor desliga junto com o PC. Com o PC desligado e `pc_mac` preenchido, a assistente manda o "pacote mágico" pela rede e espera o agente responder por até 90 segundos antes de abrir o programa.

1. No PC, rode `getmac /v` e copie o **Endereço físico** da placa de rede com cabo para `"pc_mac"`, como `"AA:BB:CC:DD:EE:FF"`.
2. Em `"pc_broadcast"`, use o endereço de broadcast da rede, que é o IP do PC com o último número trocado por 255 (por exemplo `"192.168.0.255"`). Vazio, vale `255.255.255.255`, que nem sempre sai pela rede certa.
3. Prepare o PC:
   - use cabo de rede: pelo Wi-Fi, um PC desligado quase nunca liga;
   - no BIOS/UEFI, ative "Wake on LAN" (ou "Power On By PCI-E") e desative o "ErP";
   - no Gerenciador de Dispositivos, nas propriedades da placa de rede: em Gerenciamento de Energia, marque "Permitir que este dispositivo ative o computador" e a opção de ativar só com o pacote mágico ("Magic Packet"); em Avançado, ative "Wake on Magic Packet";
   - a Intel recomenda desativar a Inicialização Rápida do Windows. O Windows só garante o Wake-on-LAN a partir da suspensão ou da hibernação: a partir do PC desligado, depende da placa-mãe.
4. O agente precisa abrir sozinho quando você entra no Windows: aperte Win+R, digite `shell:startup` e crie ali um atalho para o `pc_agente\iniciar_agente.bat`. Mesmo assim, ele só responde depois do login: sem login automático, o PC liga e fica na tela de login, e a assistente avisa que ele não respondeu. Voltando da suspensão ou da hibernação, o agente que já estava aberto continua respondendo.

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
6. o agente aceita o `pc_token` (e mostra o que ele libera: programas, o que fecha e as ações);
7. a lâmpada responde, se houver uma configurada;
8. a previsão do tempo responde para a `cidade`, se houver uma;
9. o Spotify está conectado, se estiver configurado.

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

O `termux-instalar.sh` atualiza os pacotes do Termux, instala `python`, `python-pip`, `git` e o `python-cryptography` (que a lâmpada usa e o pip não compila no celular), instala as bibliotecas do `requirements.txt` e cria o `celular_servidor/config_servidor.json` a partir do exemplo, se ele ainda não existir. Pode levar alguns minutos.

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
- a lâmpada: conferência do bloco `lampada`, tradução dos pedidos para os comandos da Tuya e dos erros para o português, e o `tinytuya` de verdade falando com uma lâmpada falsa em `127.0.0.1` nos protocolos 3.3, 3.4 e 3.5, inclusive com chave ou versão erradas e com a lâmpada desligada;
- fechar programas, volume, bloquear e desligar o PC: os comandos exatos do Windows e do Linux (taskkill, tasklist, pkill, wpctl, pactl, shutdown), com as funções que mexem no PC trocadas por mocks. Nenhum teste muda o volume, bloqueia a tela ou desliga nada; no Windows, só uma leitura do volume real;
- a confirmação por voz antes de desligar: o que conta como "sim" e como "não", a pergunta que vence e que vale uma vez só;
- o Wake-on-LAN, a previsão do tempo (com um Open-Meteo falso) e o Spotify (com uma API falsa: renovação do token, busca, escolha do computador, erros de Premium e de cota, e o `spotify_conectar.py` de ponta a ponta);
- o diagnóstico `verificar.py`, os scripts do Termux e o `iniciar_agente.bat`.

Os testes copiam o código para uma pasta temporária com configs próprios, então nunca leem nem alteram os seus `config_*.json`. Passam no Windows e no Linux. Alguns são de uma plataforma só e aparecem como `skipped` nas outras: o `.bat` só roda no Windows; os scripts do Termux e os dois testes que sobem o agente em `0.0.0.0` (Ctrl+C e porta ocupada), só no Linux, para não acionar o firewall do Windows; o JavaScript da página, só com o Node instalado; os testes com a lâmpada falsa, só com o `tinytuya` instalado; a leitura real do volume e o Core Audio falso, só no Windows; a permissão do `spotify_token.json`, só no Linux. Sem Flask, os testes do servidor e do diagnóstico também aparecem como `skipped`: instale o `requirements.txt` para rodá-los.

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

### Lâmpada

- **"A lâmpada não respondeu: confira se o interruptor dela está ligado..."**: a lâmpada está desligada no interruptor, fora da rede ou com outro IP. Rode `python -m tinytuya scan`, compare o IP com o do config e reserve o IP no roteador.
- **"A lâmpada não aceitou a conexão..."**: além de desligada, acontece com a `versao` errada (uma lâmpada 3.5 configurada como 3.3, por exemplo). Use a `Version` que o scan mostra.
- **"A lâmpada recusou a conexão: confira a "chave_local" e a "versao"..."**: confira primeiro a `versao`, que dá essa mesma mensagem quando está errada (uma lâmpada 3.4 configurada como 3.3, ou o contrário): use a `Version` do scan. Se a versão estiver certa, a chave está errada ou mudou porque a lâmpada foi pareada de novo no app: rode o wizard outra vez e copie a `key` nova do `devices.json`.
- **"Falta preencher ... no bloco "lampada""**: complete os dados, como em [Preencha o bloco da lâmpada](#4-preencha-o-bloco-da-lâmpada), ou apague o bloco se não tiver lâmpada.
- **'"chave_local" da lâmpada precisa ter 16 caracteres'** (ou `"id"`, `"ip"`, `"versao"`): o valor veio com o formato errado. Copie de novo do `devices.json` ou do scan.
- **"Falta a biblioteca tinytuya"** ou **`No module named tinytuya`**: o comando rodou fora do ambiente virtual, ou o `requirements.txt` não foi instalado de novo depois de atualizar. Ative o `.venv` e rode `pip install -r celular_servidor/requirements.txt`. No Termux, rode `bash scripts/termux-instalar.sh`, que instala também o `python-cryptography`.
- **O scan não mostra a lâmpada**: confira se ela está ligada e se o PC está na mesma rede Wi-Fi. No Windows, a rede precisa estar como privada e o Python liberado no firewall em redes privadas; no Linux, o firewall precisa liberar as portas UDP 6666, 6667 e 7000, como em [Descubra o IP e a versão](#3-descubra-o-ip-e-a-versão-em-casa).
- **O wizard não lista a lâmpada ou dá erro de permissão**: a conta do app não está vinculada ao projeto, a região está errada (tente `us-e`, como explica o [passo do wizard](#2-pegue-o-id-e-a-chave-local-uma-vez)) ou o teste do IoT Core venceu (renove em **Cloud → Cloud Services** na plataforma).
- **"Este modelo de lâmpada usa outros comandos"**: a lâmpada não segue o padrão das lâmpadas Tuya mais comuns (os comandos 20 a 24), o único que o servidor sabe mandar por enquanto.
- **O branco sai amarelado e o "branco quente" sai azulado**: alguns modelos invertem a escala de temperatura. Troque os valores dos brancos em `BRANCOS`, no `celular_servidor/lampada.py` (0 e 1000 trocam de lugar).

### Controle do PC

- **"'x' não está na lista de programas que posso fechar"**: cadastre o programa em `fechar` no `config_agente.json` e abra o agente de novo.
- **"x não está aberto"**: o agente não achou o processo. Confira o nome no Gerenciador de Tarefas, na aba Detalhes, com o `.exe`.
- **"pedi para fechar x, mas ele continua aberto; talvez esteja esperando você salvar algo"**: o programa pediu para salvar, ou não aceita o pedido de fechar. Feche à mão. Os aplicativos da Microsoft Store, como a Calculadora, não têm janela própria (quem desenha a janela é o `ApplicationFrameHost`): o agente fecha a moldura deles, como o X.
- **"... precisa ter o .exe no fim"** ou **"... precisa ser só o nome do processo"**: em `fechar`, use só o nome do processo, como `"chrome.exe"`, sem pasta. O `ApplicationFrameHost.exe` é recusado (ele fecharia todos os aplicativos da Microsoft Store de uma vez), e o `explorer.exe` também: pedir para ele fechar abre a caixa "Desligar o Windows".
- **"o controle de volume está desligado no config_agente.json do PC"**, **"bloquear a tela está desligado..."** ou **"desligar o PC não está liberado..."**: acrescente a ação em `acoes` no `config_agente.json` e abra o agente de novo.
- **"o PC não tem uma saída de som ativa"**: nenhum alto-falante ou fone está ativo no Windows.
- **"não achei o wpctl nem o pactl para controlar o volume neste PC"** (Linux): instale o PipeWire (`wpctl`) ou o `pulseaudio-utils` (`pactl`).
- **A assistente diz que não consegue fechar programas ou mudar o volume**: o agente ainda está com o config antigo. Depois de mudar o `config_agente.json`, feche o agente e abra de novo.
- **O volume não muda**: a assistente muda o volume do Windows, não o controle de volume dentro do Spotify; com o Windows já em 100%, "aumenta o volume" não muda nada. Se nem isso funcionar, o agente precisa rodar na sessão do usuário, na janela aberta pelo `iniciar_agente.bat`, e não como serviço do Windows.
- **Quero cancelar o desligamento**: diga "cancela o desligamento" nos 30 segundos, ou rode `shutdown /a` no PC. **"já existe um desligamento agendado"**: cancele o anterior do mesmo jeito.
- **O Wake-on-LAN não liga o PC**: confira o `pc_mac` (da placa com cabo), o `pc_broadcast`, o BIOS/UEFI e a placa de rede, como em [Wake-on-LAN](#ligar-o-pc-pela-rede-wake-on-lan-opcional). **"mandei o sinal para ligar o PC, mas o agente não respondeu em 90 segundos"**: o PC pode ter ligado e ficado na tela de login; faça login e abra o agente.

### Previsão do tempo e Spotify

- **"não encontrei a cidade x"**: use o formato `"Cidade, UF"`, como `"Curitiba, PR"`.
- **"não consegui consultar a previsão do tempo agora"**: o aparelho está sem internet ou o Open-Meteo está fora do ar. Tente de novo em instantes.
- **"o Spotify ainda não foi conectado"** ou **"a autorização do Spotify venceu (ela dura 6 meses)"**: na pasta `celular_servidor`, rode `python spotify_conectar.py`.
- **"o Spotify recusou: a conta precisa ser Premium..."**: o dono do app precisa ter Premium ativo (depois de assinar, pode levar algumas horas para valer), e outras contas precisam estar em Settings → Users Management.
- **"o Spotify não está aberto no PC: abra o aplicativo e peça de novo"**: abra o Spotify no PC. Com `"spotify"` na lista de programas do agente, a assistente abre sozinha. Se você preencheu `dispositivo`, confira se o nome é o mesmo que aparece no Spotify, em "Conectar a um dispositivo".
- **"abri o Spotify no PC, mas ele ainda não apareceu para tocar"**: o aplicativo demorou mais de 15 segundos para abrir. Peça de novo em alguns segundos.
- **"INVALID_CLIENT: Invalid redirect URI"** na página do Spotify: a Redirect URI do app no painel precisa ser exatamente `http://127.0.0.1:8888/callback`. Corrija em Settings, salve e rode o `spotify_conectar.py` de novo.
- **A porta 8888 está ocupada** ao conectar: feche o programa que a usa ou rode `python spotify_conectar.py --colar`.
- **"a cota do app do Spotify acabou por enquanto"** ou **"o Spotify pediu para esperar um pouco"**: espere alguns minutos.

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
- **Erro ao instalar o `cryptography` no Termux**: o `tinytuya` precisa dele, e o pip não consegue compilá-lo no celular. Rode `pkg install python-cryptography` (o `termux-instalar.sh` já faz isso) e instale de novo.
- **`pip: command not found` no Termux**: o pip é um pacote separado; rode `pkg install python-pip` ou o `termux-instalar.sh`.
- **"WARNING: The C extension could not be compiled" ao instalar no Termux**: é o MarkupSafe (dependência do Flask) sem compilador. Ele instala a versão em Python puro e funciona normalmente.
- **"Não encontrei o Python neste PC"** (no `.bat`) ou **"Python não foi encontrado; executar sem argumentos para instalar do Microsoft Store"**: o Python não está instalado ou não está no PATH. Instale pelo python.org, como em [Requisitos](#requisitos).
- **Acentos aparecem estranhos no terminal do Windows (Git Bash)**: use o PowerShell ou defina a variável `PYTHONUTF8=1`.
- **Nos testes, tudo do servidor aparece como `skipped`**: o ambiente não tem Flask e requests. Ative o `.venv` e instale o `requirements.txt`.
