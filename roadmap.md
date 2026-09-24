# Roadmap: assistente de voz caseira

## Arquitetura atual

O celular Android sobrando é o centro do sistema: ele roda o servidor (no Termux), serve a página com o botão de falar e usa o próprio microfone e alto-falante. O processamento pesado fica no Groq, que faz a transcrição (Whisper) e roda o LLM (Qwen). O PC roda um agente pequeno que só abre programas de uma lista permitida. A lâmpada Elgin Smart Color (plataforma Tuya) será controlada direto pelo celular, via Wi-Fi local.

```
Celular (Termux)
  página web + servidor Python (Flask)
  → Groq: transcrição + LLM com ferramentas
  → PC: agente que abre programas
  → lâmpada: tinytuya pela rede local
```

Tudo se comunica pela rede Wi-Fi de casa, através do roteador.

## Stack

| Camada | Ferramenta |
|---|---|
| Linguagem | Python 3.11+ |
| Servidor no celular | Flask + requests, rodando no Termux |
| Agente no PC | Python puro (biblioteca padrão) |
| Cliente | Página HTML + JavaScript aberta em `localhost` no celular |
| Transcrição | Whisper no Groq |
| LLM | Qwen no Groq (ou Ollama, trocando só a configuração) |
| Voz de resposta | `speechSynthesis` do navegador |
| Lâmpada | Elgin Smart Color + `tinytuya` |

## Fases

### v0.1a: abrir programas (em andamento)

Enquanto a lâmpada não chega, o MVP funciona só entre celular e PC.

- [ ] Reservar IP fixo para o PC no roteador
- [ ] Rodar o agente no PC com token e lista de programas
- [ ] Criar a chave do Groq e confirmar o nome do modelo Qwen com suporte a tool calling
- [ ] Testar o servidor no próprio PC, primeiro por texto e depois por voz
- [ ] Instalar o Termux no celular e levar o servidor para lá
- [ ] Testar o fluxo completo: falar no celular e ver o programa abrir no PC

### v0.1b: lâmpada (quando a Elgin chegar)

- [ ] Parear a lâmpada no app Smart Life (confirma que é Tuya)
- [ ] Criar o projeto na plataforma de desenvolvedor da Tuya e vincular a conta
- [ ] Obter ID, IP e local key com `python -m tinytuya wizard`
- [ ] Reservar IP fixo para a lâmpada no roteador
- [ ] Adicionar a ferramenta `controlar_lampada(ligar, brilho, cor)` ao servidor

Com a v0.1a e a v0.1b prontas, o MVP está completo.

### v0.2: qualidade de vida

- Arquivo de configuração único para cadastrar lâmpadas e programas
- Logs dos comandos com data e hora
- Mensagens de erro mais claras (lâmpada offline, PC desligado)
- Iniciar o servidor automaticamente quando o Termux abrir (Termux:Boot)

### v0.3: mais ferramentas

- Fechar programas e controlar o volume do PC
- Ligar o PC remotamente com Wake-on-LAN antes de abrir um programa
- Bloquear ou desligar o PC, com confirmação por voz
- Perguntar horário e previsão do tempo

### v0.4: voz melhor e memória

- Trocar a voz do navegador pelo Piper (vozes pt-BR mais naturais)
- Manter um histórico curto da conversa para entender "agora apaga ela"
- Reduzir a latência com respostas em streaming

### v0.5: palavra de ativação

- Substituir o botão pelo openWakeWord, com uma palavra escolhida por você
- Celular sempre ligado como aparelho dedicado (na tomada, com limite de carga se disponível)

### v0.6: casa inteligente completa

- Integrar com o Home Assistant para controlar dispositivos de qualquer marca
- Satélites baratos com ESP32 em outros cômodos
- Acesso de fora de casa com Tailscale

## Regras que valem para todas as fases

- O LLM nunca executa comandos livres: ele só escolhe itens de listas definidas no código.
- Todo pedido ao PC exige token.
- Os serviços não ficam expostos para a internet; o acesso externo só entra com Tailscale na v0.6.
