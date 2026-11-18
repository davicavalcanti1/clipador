# Clipador

Grava a live de twitch.tv/gilzera neste PC, recebe marcações de quem estiver assistindo (de qualquer casa), detecta picos no chat e corta clipes verticais 1080x1920 para TikTok e Reels.

## Instalação (Windows)

Pré-requisitos no PATH: Python 3.11+, [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) (com ffprobe) e, para o link externo, [cloudflared](https://github.com/cloudflare/cloudflared/releases).

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Na primeira execução o programa cria o `config.json` com os valores padrão e uma senha nova. Esse arquivo não vai para o repositório porque guarda senha e chaves (`twitch_client_id`, `twitch_client_secret`, `anthropic_api_key`). As pastas `gravacao\`, `clipes\` e `tmp\` também ficam fora.

O corte usa Intel QSV quando existe GPU Intel e cai para x264 na CPU quando não existe.

## Como usar no dia

1. Dois cliques em `INICIAR CLIPADOR.bat`. Não feche a janela preta.
2. A janela mostra dois links:
   - **Painel local**: o seu.
   - **Link para o seu amigo**: mande para ele. Muda a cada vez que o programa reinicia.
3. Cada um coloca o nome no painel e aperta **MARCAR CLIPE** (ou a tecla M) quando acontecer algo.
4. Uns 25 s depois o clipe aparece em "Para revisar". Lá dá para assistir, ajustar início e fim (±10 s), aprovar ou descartar.
5. Os aprovados ficam em `clipes\aprovados\`, prontos para postar. Pelo painel, o amigo baixa a versão vertical ou a horizontal.

O programa começa a gravar sozinho quando a live abre e volta a tentar se ela cair.

## Modo automático (sem ninguém clicar)

Quatro sinais geram candidatos:

| Sinal | Como detecta |
|---|---|
| Chat | Mensagens em 10 s acima de 2,5x a mediana dos últimos 5 min (risada e emote contam em dobro) |
| Áudio | Volume (LUFS momentâneo) 8 dB acima da mediana dos últimos 5 min |
| Clipes da Twitch | Clipe criado por espectador (API Helix, consulta a cada 60 s; precisa do app em `config.json`) |
| Manual | Botão do painel. É sempre cortado, não passa pela seleção |

Sinais a até 25 s de distância se juntam num candidato só, com uma nota (chat + áudio + 6 por clipe da Twitch + 2 se houver sinais diferentes concordando). A cada bloco de 30 min, os 3 melhores com nota ≥ 3,5 são cortados, legendados (Whisper, no próprio PC) e vão direto para `clipes\aprovados\`. O resto fica na aba "Candidatos" e pode ser cortado com um clique.

Ajustes em `config.json`: `auto.por_hora`, `auto.bloco_min`, `auto.nota_minima`, `legenda.ativa`.

## Edição automática (desligada por enquanto)

Código pronto em `edicao.py`, desligado em `config.json` com `"edicao": {"ativa": false}`. Ligada, ela faz:

- a Claude escolhe o trecho final (15 a 60 s) e escreve gancho, título, descrição e hashtags;
- zoom no pico;
- volume em -14 LUFS;
- @ da conta no canto;
- não aprova sozinho um clipe com nota da IA abaixo de 4.

Para ligar, precisa de `anthropic_api_key` e, se quiser a marca, `marca`.

A legenda (Luckiest Guy, palavra falada em amarelo) independe da edição e continua ligada.

## Detalhes

- Cada marcação pega 45 s antes e 15 s depois. O padrão muda em "ajustar layout vertical".
- Duas marcações com até 20 s de diferença viram um clipe só.
- Um pico no chat (risada, "KKKK", KEKW, etc. acima de 2,5x a média) cria uma sugestão marcada como "chat".
- Layout: em "ajustar layout vertical", com a live no ar, arraste um retângulo em volta da facecam. No modo dividido a câmera fica em cima e o jogo embaixo.
- A gravação completa fica em `gravacao\`, em pedaços de ~6 s (cerca de 3 a 4 GB por hora em 1080p60). Dá para apagar depois da live.
- Configurações em `config.json`. A `senha` é a chave do link. Se apagar, uma nova é gerada.
