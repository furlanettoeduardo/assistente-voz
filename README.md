# Assistente de voz caseira: fase 0.1a (abrir programas)

Você segura um botão no celular, fala "abre a calculadora" e o programa abre no PC.

```
Celular (Termux)                                    PC
  página com o botão de falar                         pc_agente/agente.py
  celular_servidor/servidor.py  ── rede local ──▶     abre só programas da lista
     │
     └─▶ Groq: transcrição (Whisper) + LLM Qwen com ferramentas
```

O projeto tem duas partes:

- `pc_agente/`: roda no computador e abre programas quando o celular pede. Usa só a biblioteca padrão do Python.
- `celular_servidor/`: roda no celular (Termux). Mostra a página com o botão de falar, transcreve o áudio no Groq, consulta o LLM e manda o PC agir.

O LLM nunca executa comandos: ele só escolhe um nome da lista de programas do agente, que roda o comando cadastrado sem shell, e todo pedido ao PC exige token. O plano das próximas fases está em [roadmap.md](roadmap.md).

## Requisitos

- Python 3.11 ou mais novo no PC e no celular. No Windows, instale pelo python.org: o instalador principal (Python install manager) já deixa os comandos `python` e `py` disponíveis; se usar o instalador tradicional, marque **Add python.exe to PATH**.
- PC com Windows ou Linux na mesma rede Wi-Fi do celular.
- Celular Android com Termux instalado pelo **F-Droid** (a versão da Play Store é experimental e tem recursos faltando).
- Uma chave gratuita da API do Groq.

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

Salve os arquivos em UTF-8 (o padrão do Bloco de Notas e do VS Code). Se um config faltar, estiver incompleto ou com o JSON quebrado, o programa encerra dizendo o que corrigir.

### Agente do PC: `pc_agente/config_agente.json`

| Chave | O que é |
|---|---|
| `token` | Segredo compartilhado com o servidor, sem acentos. O agente não inicia com o token vazio ou com o valor de exemplo. |
| `porta` | Porta em que o agente escuta. Padrão: `8765`. |
| `programas` | Nome falado → comando que o PC executa, como lista de strings. Maiúsculas e espaços extras no nome não importam. |

1. No roteador, reserve um IP fixo para o PC (reserva de DHCP). Anote o IP, por exemplo `192.168.0.10`.
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
| `pc_url` | IP e porta do agente, por exemplo `http://192.168.0.10:8765`. |
| `pc_token` | O mesmo valor de `token` do agente. |
| `host` e `porta` | Onde o servidor escuta. Padrão: `127.0.0.1:8000`, só o próprio aparelho acessa. |

Em setembro de 2026, o Qwen disponível no Groq é o `qwen/qwen3.8-27b`, na categoria Preview. Modelos Preview trocam de nome ou saem do ar com pouco aviso, então confira a lista antes de preencher.

Para usar Ollama no lugar do Groq, troque `llm_base_url` para `http://IP-DO-PC:11434/v1`, `llm_api_key` para `ollama` e `llm_model` para o nome de um modelo com suporte a ferramentas. Por padrão o Ollama só aceita conexões do próprio PC: defina a variável de ambiente `OLLAMA_HOST=0.0.0.0`, reinicie o Ollama e libere a porta 11434 no firewall, só em redes privadas. A transcrição continua no Groq.

## Como rodar

### 1. Agente no PC

```
cd pc_agente
python agente.py
```

No Windows, quando o firewall perguntar, permita o acesso **apenas em redes privadas**. O Windows 11 marca toda rede nova como pública, então confira em Configurações > Rede e Internet > Wi-Fi > propriedades da sua rede se o **Tipo de perfil de rede** está como **Rede privada**. Sem isso, o celular não alcança o agente.

Depois de mudar o `config_agente.json`, feche o agente (Ctrl+C) e rode de novo.

### 2. Servidor no próprio PC, para testar

Antes de ir para o celular, rode tudo no computador para achar erros mais rápido. Com o ambiente virtual ativado e `"pc_url": "http://127.0.0.1:8765"`:

```
cd celular_servidor
python servidor.py
```

Abra `http://localhost:8000` no navegador do PC. Comece pelo campo de texto ("abre a calculadora"); depois teste o botão de voz.

### 3. Servidor no celular (Termux)

1. No Termux:
   ```
   pkg update && pkg install python python-pip
   termux-setup-storage
   ```
2. Copie a pasta `celular_servidor` inteira (com a subpasta `static` e o seu `config_servidor.json`) para o celular, por exemplo para Downloads, e depois:
   ```
   cp -r ~/storage/downloads/celular_servidor ~/
   cd ~/celular_servidor
   pip install -r requirements.txt
   ```
   A pasta Downloads é acessível por outros apps. Depois de copiar, apague de lá o `config_servidor.json`.
3. Volte o `pc_url` para o IP do PC na rede (`http://192.168.0.10:8765`), editando com `nano config_servidor.json`.
4. Rode `termux-wake-lock` e depois `python servidor.py`.
5. Abra o Chrome do celular em `http://localhost:8000` e permita o microfone.

Nas configurações do Android, desative a otimização de bateria para o Termux, senão o sistema encerra o servidor com a tela apagada.

## Testes

Na raiz do repositório, com o ambiente virtual ativado:

```
python -m unittest -v
```

Os testes não chamam nenhuma API real. O LLM e a transcrição são um servidor HTTP falso em `127.0.0.1`, e o agente roda de verdade numa porta livre, também em `127.0.0.1`, então o firewall não é acionado. Eles cobrem:

- token errado, ausente ou com acento recusado pelo agente, que também não inicia com token vazio ou de exemplo;
- programa fora da lista recusado, inclusive quando o LLM inventa um nome ou uma ferramenta;
- fluxo completo: texto ou voz → LLM com tool call → agente → programa aberto. O "programa" de teste é o próprio Python criando um arquivo temporário, e o teste confere que ele foi chamado sem shell;
- remoção dos blocos `<think>` da resposta, inclusive sem abertura ou sem fechamento;
- erros da API na transcrição e no LLM, argumentos inválidos vindos do LLM, PC desligado, pedidos malformados ao agente e configuração ausente, incompleta ou inválida.

Os testes copiam o código para uma pasta temporária com configs próprios, então nunca leem nem alteram os seus `config_*.json`. Passam no Windows e no Linux. Sem Flask instalado, os testes do servidor aparecem como `skipped`: o resultado só vale se terminar em `OK` sem `skipped`.

## Solução de problemas

### Configuração

- **"Arquivo de configuração não encontrado"**: copie o `.example.json` da mesma pasta, como em [Configuração](#configuração).
- **"Erro de JSON em ..., linha X, coluna Y"**: tem vírgula sobrando ou faltando, aspas erradas, ou um caminho do Windows com barra simples. Em JSON, `C:\Pasta` se escreve `C:\\Pasta`.
- **"Faltam chaves em ..."**: o config não tem todas as chaves do `.example.json`. Compare os dois e copie as que faltam.
- **"... não está em UTF-8"**: o arquivo foi salvo em ANSI ou UTF-16. Abra no Bloco de Notas, vá em Salvar como e escolha a codificação UTF-8.
- **"Defina um token próprio"**: o `token` do agente está vazio, com acento ou ainda com o valor de exemplo. Gere um novo com o comando da [configuração do agente](#agente-do-pc-pc_agenteconfig_agentejson).

### Celular e PC

- **A assistente diz que o computador está desligado ou inacessível** (o texto exato varia, porque é o LLM que escreve): o servidor não conseguiu a lista de programas, seja por rede, seja por token errado. Olhe o console do agente no PC:
  - se aparece `[agente] pedido recusado de ...: token inválido`, a rede está certa e o `pc_token` do servidor está diferente do `token` do agente;
  - se não aparece nada, o pedido nem chegou ao PC. Confira se o agente está rodando, se o IP em `pc_url` está certo, se a rede do Windows está como privada e se o firewall liberou o Python. No Linux com firewall ativo, libere a porta: `sudo ufw allow 8765/tcp`.

  Para testar só a rede, abra `http://IP-DO-PC:8765/programas` no Chrome do celular. A resposta `{"erro": "token inválido"}` prova que a rede chega até o agente (o navegador não manda token).
- **Cancelei o aviso do firewall do Windows**: procure "Permitir um aplicativo pelo Firewall do Windows" no menu Iniciar e marque o Python na coluna Privada.
- **A página diz "Abriu", mas nada abriu no PC**: com `cmd /c start`, "Abriu" só quer dizer que o comando foi disparado. Rode o mesmo comando no terminal do PC (por exemplo `cmd /c start "" chrome`) para ver o erro.
- **"Não abriu: nome (programa 'nome' não está na lista)"**: o LLM escolheu um nome que não existe em `programas`. Fale o nome como está no config.
- **"Não consegui escutar na porta 8765"**: outro agente (ou outro programa) já usa a porta. Feche a outra janela do agente ou troque `porta` no config e em `pc_url`.

### API do Groq

- **"Erro na API: 401"**: a chave do Groq está errada ou foi revogada.
- **"Erro na API: 404" dizendo que o modelo "does not exist"**: o nome em `llm_model` está diferente do que o Groq lista.
- **"Erro na API: 404 404 page not found"**: falta o `/v1` no fim de `llm_base_url` (acontece ao configurar o Ollama).
- **"Erro na API: 400" dizendo que o modelo "has been decommissioned"**: o modelo saiu do ar. Escolha outro Qwen na lista de modelos do Groq.
- **"Erro na API: 400" com "Failed to call a function"**: o modelo gerou uma chamada de ferramenta inválida. Repita o pedido; se acontecer sempre, troque de modelo.
- **"Erro na API: 429"**: estourou o limite de pedidos do plano gratuito. Espere um pouco e tente de novo.
- **A transcrição mostra frases que você não disse** (por exemplo "Legendas pela comunidade Amara.org"): o Whisper inventa texto quando o áudio sai quase mudo. Segure o botão durante toda a fala e fale mais perto do microfone.
- **Aparece texto de raciocínio na resposta**: o servidor remove os blocos `<think>`. Se ainda aparecer, o backend usa outro formato; com Ollama, atualize para uma versão recente.
- **Usando Ollama, "Sem conexão com a API"**: confira se o Ollama está rodando com `OLLAMA_HOST=0.0.0.0` e se o firewall liberou a porta 11434.

### Celular e instalação

- **A página abre em branco ou dá 404**: a pasta `static` com o `index.html` precisa estar dentro de `celular_servidor`.
- **Microfone não liga**: a página precisa ser aberta como `localhost` no próprio aparelho que roda o servidor; o navegador só libera o microfone em `localhost` ou HTTPS.
- **O servidor para com a tela apagada**: rode `termux-wake-lock` e desative a otimização de bateria do Termux.
- **`pip: command not found` no Termux**: o pip é um pacote separado; rode `pkg install python-pip`.
- **"WARNING: The C extension could not be compiled" ao instalar no Termux**: é o MarkupSafe (dependência do Flask) sem compilador. Ele instala a versão em Python puro e funciona normalmente.
- **"Python não foi encontrado; executar sem argumentos para instalar do Microsoft Store"**: o `python` do Windows é só o atalho da Loja, porque o Python não está instalado ou não está no PATH. Instale pelo python.org, como em [Requisitos](#requisitos).
- **Acentos aparecem estranhos no terminal do Windows (Git Bash)**: use o PowerShell ou defina a variável `PYTHONUTF8=1`.
- **Nos testes, tudo do servidor aparece como `skipped`**: o ambiente não tem Flask e requests. Ative o `.venv` e instale o `requirements.txt`.
