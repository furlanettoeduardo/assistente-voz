# Roadmap: assistente de voz caseira

## Arquitetura

### Protótipo atual: tudo no notebook

O celular de testes não funcionou, então o notebook faz os dois papéis por enquanto: roda o servidor (o cérebro) e o agente, e a página com o botão de falar abre em `http://localhost:8000` no próprio notebook. O servidor fala com o agente em `http://127.0.0.1:8765`. O processamento pesado continua no Groq, que faz a transcrição (Whisper) e roda o LLM (Qwen).

```
Notebook
  página em http://localhost:8000 (botão de falar)
  celular_servidor/servidor.py (Flask)
    → Groq: transcrição (Whisper) + LLM Qwen com ferramentas
    → pc_agente/agente.py em http://127.0.0.1:8765: abre programas da lista
```

O suporte ao celular com Termux continua no projeto (`scripts/termux-instalar.sh` e `scripts/termux-iniciar.sh`), como alternativa opcional.

### Arquitetura final

- **Cérebro:** um Raspberry Pi sempre ligado dentro de casa, rodando o `servidor.py`. É o mesmo código de hoje, que já é compatível com Linux.
- **Satélites:** um ESP32-S3 com PSRAM por cômodo, com microfone e alto-falante. A palavra de ativação "hey Jarvis" é detectada no próprio chip, com o microWakeWord.
- **PC:** apenas o agente, instalado para rodar em segundo plano.

```
Satélite ESP32-S3 (um por cômodo)                    Raspberry Pi (cérebro)
  microfone e alto-falante                             servidor.py
  "hey Jarvis" no próprio chip    ── Wi-Fi, /voz ──▶     → Groq: transcrição + LLM
                                                         → PC: agente em segundo plano
                                                         → lâmpada: tinytuya na rede local
                                                         → Wake-on-LAN para ligar o PC
```

Os aparelhos se comunicam pela rede Wi-Fi de casa, através do roteador; só as chamadas ao Groq (transcrição e LLM) saem para a internet.

## Decisões

### Sem servidor na nuvem

- A lâmpada, o agente do PC e o Wake-on-LAN estão na rede local, que um servidor na nuvem não alcança.
- Um servidor exposto na internet, capaz de abrir programas no PC, seria um risco de segurança permanente.
- O acesso de fora de casa, quando existir, será via Tailscale.

### Descartados

- **STM32:** a maioria dos modelos não tem Wi-Fi; seria trabalho demais para pouco ganho.
- **Palavra de ativação no navegador:** fazia sentido só com o celular como aparelho dedicado, e o celular era provisório.

### Contrato entre satélite e cérebro: `/voz`

Os satélites vão falar com o cérebro pelo endpoint que a página já usa hoje:

- `POST /voz` com o áudio cru no corpo e o formato no `Content-Type` (`audio/webm`, `audio/ogg`, `audio/mp4`, `audio/wav` ou `audio/mpeg`);
- resposta em JSON com `transcricao`, `resposta` (o texto a ser falado) e `acoes` (o que foi executado); em caso de erro, `{"erro": "..."}` em português, com status 400, 500 ou 502.

Mudanças previstas no contrato:

- gerar a voz de resposta no servidor, com o Piper, e devolver o áudio junto com o texto;
- aceitar WAV 16 kHz, o formato natural do ESP32 (o servidor já repassa `audio/wav` ao Groq, mas ainda não foi testado com o áudio de um satélite);
- exigir token dos satélites quando o servidor aceitar conexões da rede (hoje o servidor não pede token em nenhum caso; por isso, por padrão, escuta só em `127.0.0.1`).

### Instalador do agente

- Empacotado com PyInstaller e instalado com Inno Setup.
- Inicia junto com o Windows.
- Ícone na bandeja do sistema com status, pausar e sair.
- Tela de primeira configuração que gera o token e mostra um QR code.

## Stack

| Camada | Protótipo atual | Arquitetura final |
|---|---|---|
| Cérebro | `servidor.py` (Flask + requests) no notebook; opcional no celular com Termux | O mesmo `servidor.py` num Raspberry Pi |
| Entrada de voz | Página HTML + JavaScript em `localhost`, com botão de falar | Satélites ESP32-S3 com "hey Jarvis" (microWakeWord) |
| Agente no PC | `agente.py`, Python puro (biblioteca padrão), aberto pelo `iniciar_agente.bat` | Instalado (PyInstaller + Inno Setup), em segundo plano, com ícone na bandeja |
| Transcrição | Whisper no Groq | Whisper no Groq |
| LLM | Qwen no Groq (ou Ollama, trocando só a configuração) | Qwen no Groq (ou Ollama) |
| Voz de resposta | `speechSynthesis` do navegador | Piper no servidor, com o áudio devolvido pelo `/voz` |
| Lâmpada | Elgin Smart Color + `tinytuya` (v0.1b) | Elgin Smart Color + `tinytuya` |
| Acesso de fora de casa | Nenhum | Tailscale |

## Fases

### v0.1a: abrir programas (concluída no notebook; falta confirmar a voz)

- [x] Rodar o agente no PC com token e lista de programas
- [x] Criar a chave do Groq e confirmar o nome do modelo Qwen com suporte a tool calling
- [x] Testar o servidor no notebook por texto, com o agente abrindo programas
- [ ] Testar o servidor no notebook por voz (botão da página)

### Correções e diagnóstico (concluído)

- [x] Configs reais fora do Git, com `*.example.json` e mensagem clara quando faltam ou estão errados
- [x] Testes sem API real (LLM falso local), passando no Windows e no Linux
- [x] Issues #1 a #5 corrigidas: config com tipo errado, chave com aspas curvas, corpo malformado em `/texto`, erros técnicos em inglês na página e terceira rodada de ferramenta
- [x] Todas as mensagens ao usuário em português, inclusive com o servidor fora do ar ou a rede caída
- [x] Diagnóstico `celular_servidor/verificar.py`
- [x] Scripts do Termux (`termux-instalar.sh` e `termux-iniciar.sh`) e `pc_agente/iniciar_agente.bat`

### v0.1b: lâmpada Elgin (Tuya/tinytuya)

- [ ] Parear a lâmpada no app Smart Life (confirma que é Tuya)
- [ ] Criar o projeto na plataforma de desenvolvedor da Tuya e vincular a conta
- [ ] Obter ID, IP e local key com `python -m tinytuya wizard`
- [ ] Reservar IP fixo para a lâmpada no roteador
- [ ] Adicionar a ferramenta `controlar_lampada(ligar, brilho, cor)` ao servidor
- [ ] Mensagem clara em português quando a lâmpada estiver offline

Com a v0.1a e a v0.1b prontas, o MVP está completo.

### v0.2: mais ferramentas e Wake-on-LAN

- [ ] Fechar programas e controlar o volume do PC
- [ ] Ligar o PC com Wake-on-LAN antes de abrir um programa
- [ ] Bloquear ou desligar o PC, com confirmação por voz
- [ ] Perguntar horário e previsão do tempo

### v0.3: voz gerada no servidor, memória curta e streaming

- [ ] Gerar a voz de resposta no servidor com o Piper (vozes pt-BR mais naturais) e devolver o áudio junto com o texto no `/voz`
- [ ] Manter um histórico curto da conversa para entender "agora apaga ela"
- [ ] Reduzir a latência com respostas em streaming

### v0.4: instalador do agente

- [ ] Empacotar o agente com PyInstaller e criar o instalador com Inno Setup
- [ ] Iniciar o agente junto com o Windows, em segundo plano
- [ ] Ícone na bandeja com status, pausar e sair
- [ ] Tela de primeira configuração que gera o token e mostra um QR code

### v0.5: Raspberry Pi como cérebro

- [ ] Rodar o `servidor.py` no Raspberry Pi, com IP fixo reservado no roteador
- [ ] Reservar IP fixo para o PC no roteador e apontar o `pc_url` do Raspberry Pi para ele
- [ ] Subir o servidor sozinho quando o Raspberry Pi ligar
- [ ] Aceitar conexões da rede de casa, exigindo token dos satélites (e também no `/texto`, que abre programas do mesmo jeito)

### v0.6: primeiro satélite ESP32 com "hey Jarvis"

- [ ] Montar um ESP32-S3 com PSRAM, microfone e alto-falante
- [ ] Detectar "hey Jarvis" no próprio chip com o microWakeWord
- [ ] Enviar o áudio ao `/voz` em WAV 16 kHz, com token, e tocar a resposta gerada pelo Piper

### v0.7: satélites em outros cômodos e acesso externo via Tailscale

- [ ] Um satélite por cômodo
- [ ] Acesso de fora de casa via Tailscale

### Modo celular (opcional)

O servidor continua podendo rodar num celular Android com Termux, com os scripts de instalação e início e o diagnóstico.

- [ ] Testar o fluxo completo num celular que funcione: falar no celular e ver o programa abrir no PC

### Ideias ainda sem fase

- Logs dos comandos com data e hora
- Arquivo de configuração único para cadastrar lâmpadas e programas
- Iniciar o servidor automaticamente quando o Termux abrir (Termux:Boot), no modo celular
- Integração com o Home Assistant para controlar dispositivos de qualquer marca

## Regras que valem para todas as fases

- O LLM nunca executa comandos livres: ele só escolhe itens de listas definidas no código.
- Todo pedido ao PC exige token; quando o servidor aceitar conexões da rede, os satélites também vão precisar de token.
- Os serviços não ficam expostos para a internet; o acesso externo só entra via Tailscale, na v0.7.
