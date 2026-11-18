# Clipador

Grava lives da Twitch neste PC, de vários canais ao mesmo tempo. Recebe marcações de quem estiver assistindo (de qualquer casa), detecta picos no chat e no áudio e corta clipes verticais 1080x1920 para TikTok e Reels. O painel mostra, para cada canal, o gráfico em tempo real do chat e do volume, com os clipes marcados em cima.

## Instalação (Windows)

Pré-requisitos no PATH: Python 3.11+, [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) (com ffprobe) e, para o link externo, [cloudflared](https://github.com/cloudflare/cloudflared/releases).

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Na primeira execução o programa cria o `config.json` com os valores padrão e uma senha nova. Esse arquivo não vai para o repositório porque guarda senha e chaves (`twitch_client_id`, `twitch_client_secret`, `anthropic_api_key`). As pastas `gravacao\`, `clipes\` e `tmp\` também ficam fora.

O corte usa Intel QSV quando existe GPU Intel e cai para x264 na CPU quando não existe.

## Como usar

1. Dois cliques em `INICIAR CLIPADOR.bat`. Não feche a janela preta.
2. A janela mostra dois links:
   - **Painel local**: o seu.
   - **Link para o seu amigo**: mande para ele. Muda a cada vez que o programa reinicia.
3. No painel, em **Adicionar canal**, coloque o nome ou o link do canal. Ele começa a gravar na hora se estiver ao vivo e volta a tentar sozinho se a live cair. Dá para gravar vários canais ao mesmo tempo, e cada um pode ser pausado ou removido no próprio card.
4. Coloque seu nome no topo. Para marcar um lance, use o botão **MARCAR** do card do canal, o botão grande do canal selecionado ou a tecla **M**. As teclas **1** a **9** trocam de canal.
5. Para cortar um trecho exato, arraste no gráfico e clique em **cortar este trecho** (de 3 s a 5 min).
6. Uns 25 s depois o clipe aparece em "Para revisar". Ali dá para assistir, ver o gráfico do momento, ajustar início e fim (±10 s) e aprovar ou descartar.
7. Os aprovados ficam em `clipesprovados\`, com o nome do canal no começo do arquivo.

### Com ou sem legenda

No topo do painel, o seletor escolhe o que cada corte gera:

| Opção | Arquivos |
|---|---|
| clipes com e sem legenda (padrão) | `..._vertical.mp4` legendado e `..._vertical_sem_legenda.mp4` limpo (sem legenda, gancho nem marca) |
| só com legenda | `..._vertical.mp4` legendado |
| só sem legenda | `..._vertical.mp4` limpo. O Whisper nem roda, então o corte sai mais rápido |

Vale para os próximos cortes. Um clipe já pronto pode ser refeito com o ajuste de tempo. Se o trecho não tiver fala, sai só a versão limpa.

### Ao vivo: um player por canal

Logo abaixo dos cards fica um player para cada canal, todos tocando ao mesmo tempo, cada um com o botão MARCAR. Só um tem som por vez: o botão "ouvir" escolhe qual. Clicar na barra do player seleciona o canal no gráfico. "esconder players" desliga todos, o que alivia o PC e a internet de quem está assistindo.

O seletor de fonte vale para todos os players e cada pessoa escolhe o seu:

- **gravação deste PC** (padrão): sem anúncio, uns 15 s atrás da live.
- **player da Twitch**: ao vivo, mas com a tela de intervalo comercial. Para quem assiste pelo link do amigo, pode ser mais leve, porque o vídeo vem da Twitch e não pelo túnel.

Detalhe da gravação sem anúncio: o player toca a gravação deste PC, não o player da Twitch. Como o streamlink já descarta os intervalos comerciais, não aparece a tela de anúncio, e o que se vê é exatamente o que vai para os clipes. Fica uns 15 s atrás da live. Pelo link embaixo do player dá para voltar ao player da Twitch, que é ao vivo mas mostra os anúncios.

### O gráfico

| Elemento | O que é |
|---|---|
| Barras | Mensagens no chat nos últimos 10 s. Risadas e emotes contam em dobro e cada pessoa conta no máximo 3, para que alguém spamando não vire pico. Barras roxas passaram do limite. |
| Linha amarela tracejada | Limite do pico: 2,5 vezes a mediana dos últimos 5 min. |
| Linha ciano | Volume da live em LUFS (escala da direita). A tracejada mais escura é o volume normal (mediana de 5 min). |
| Faixas e triângulos | Clipes: verde aprovado, azul para revisar, roxo na fila, amarelo candidato. |

Passando o mouse aparecem os números de cada momento: mensagens, risadas, volume e espectadores. As faixas vão de 5 min a 12 h. A série fica salva em `sinais\<canal>\AAAAMMDD.csv`, um registro por segundo.

### Quantos canais ao mesmo tempo

Cada canal em 1080p60 usa uns 6 a 8 Mbit/s de internet e grava de 3 a 4 GB por hora. Para muitos canais, baixe a qualidade no seletor do painel (720p60 já serve para vertical). Por padrão, a gravação bruta com mais de 24 h é apagada sozinha, menos os trechos de clipes ainda não resolvidos (`retencao_h` no `config.json`, 0 desliga). A fila de corte é única, com 2 cortes simultâneos (`cortes_simultaneos`).

## Modo automático (sem ninguém clicar)

Quatro sinais geram candidatos:

| Sinal | Como detecta |
|---|---|
| Chat | Mensagens em 10 s acima de 2,5x a mediana dos últimos 5 min (risada e emote contam em dobro, no máximo 3 por pessoa). O clipe é marcado no começo da subida do chat, não no pico, porque a reação vem depois do lance |
| Áudio | Volume (LUFS momentâneo) 8 dB acima da mediana dos últimos 5 min |
| Clipes da Twitch | Clipe criado por espectador (API Helix, consulta a cada 60 s; precisa do app em `config.json`). A mesma consulta traz espectadores e jogo de cada canal |
| Manual | Botão do painel. É sempre cortado, não passa pela seleção |

Sinais do mesmo canal a até 25 s de distância se juntam num candidato só, com uma nota (chat + áudio + 6 por clipe da Twitch + 1,5 se o pico do chat for pelo menos 40% de risada + 2 se houver sinais diferentes concordando). A cada bloco de 30 min, os 3 melhores de cada canal com nota ≥ 2 são cortados, legendados (Whisper, no próprio PC) e vão direto para `clipes\aprovados\`. O resto fica na aba "Candidatos" e pode ser cortado com um clique.

Ajustes em `config.json`: `auto.por_hora`, `auto.bloco_min`, `auto.nota_minima`, `legenda.ativa`.

## Edição automática (desligada por enquanto)

Código pronto em `edicao.py`, desligado em `config.json` com `"edicao": {"ativa": false}`. Ligada, ela faz:

- a Claude escolhe o trecho final (15 a 60 s) e escreve gancho, título, descrição e hashtags;
- zoom no pico;
- volume em -14 LUFS;
- @ da conta no canto;
- não aprova sozinho um clipe com nota da IA abaixo de 4.

Para ligar, precisa de `anthropic_api_key` e, se quiser a marca, `marca` dentro do canal em `canais`.

A legenda (Luckiest Guy, palavra falada em amarelo) independe da edição e continua ligada.

## Detalhes

- Cada marcação pega 45 s antes e 15 s depois. O padrão de cada canal muda em "layout vertical".
- Duas marcações no mesmo canal com até 25 s de diferença viram um clipe só.
- Layout: em "layout vertical", com a live no ar, arraste um retângulo em volta da facecam. No modo dividido a câmera fica em cima e o jogo embaixo. Cada canal tem o seu layout.
- A gravação fica em `gravacao\<canal>\`, em pedaços de ~6 s.
- Configurações em `config.json`: as globais na raiz e as de cada canal em `canais`. A `senha` é a chave do link. Se apagar, uma nova é gerada.
- Quem vinha da versão de um canal só não perde nada: na primeira execução, o `config.json`, os clipes e a gravação antiga passam para dentro do canal que estava configurado.
