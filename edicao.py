"""Edição automática dos clipes: corte fino, gancho, título e legenda karaokê.

Fluxo (chamado pelo app.py para cada clipe):
  1. transcrever(): Whisper com tempo por palavra sobre a janela inteira do clipe.
  2. editar(): Claude lê a transcrição e decide o trecho final (início/fim), o
     gancho que aparece no topo, título, descrição, hashtags e uma nota de 1 a 10.
     Sem chave da API, devolve a janela inteira, sem gancho e com texto padrão.
  3. gerar_ass(): legenda palavra por palavra (palavra falada em amarelo) + gancho
     + marca do canal, no formato ASS que o ffmpeg queima no vídeo.
"""
import os
import re
from typing import List

from pydantic import BaseModel, Field

FONTE = "Luckiest Guy"
AMARELO = "&H0000E5FF&"   # BGR do ASS: #FFE500
BRANCO = "&H00FFFFFF&"


# ---------------------------------------------------------------- transcrição
class Palavra:
    __slots__ = ("texto", "ini", "fim")

    def __init__(self, texto, ini, fim):
        self.texto, self.ini, self.fim = texto, ini, fim


def transcrever(modelo, audio) -> List[Palavra]:
    segs, _ = modelo.transcribe(audio, language="pt", vad_filter=True, word_timestamps=True, beam_size=1)
    out = []
    for sg in segs:
        if sg.no_speech_prob > 0.6 or sg.avg_logprob < -1.0:
            continue
        out += [Palavra(w.word.strip(), w.start, w.end) for w in (sg.words or []) if w.word.strip()]
    return out


def transcricao_com_tempo(palavras: List[Palavra]) -> str:
    """Uma linha por frase, com o segundo de início: '[12.4] texto...'."""
    linhas, atual, ini = [], [], None
    for w in palavras:
        if atual and (w.ini - atual[-1].fim > 0.8 or len(atual) >= 14):
            linhas.append(f"[{ini:.1f}] " + " ".join(p.texto for p in atual))
            atual = []
        if not atual:
            ini = w.ini
        atual.append(w)
    if atual:
        linhas.append(f"[{ini:.1f}] " + " ".join(p.texto for p in atual))
    return "\n".join(linhas)


# ---------------------------------------------------------------- Claude
class Edicao(BaseModel):
    inicio: float = Field(description="segundo em que o clipe final começa, dentro da janela")
    fim: float = Field(description="segundo em que o clipe final termina, dentro da janela")
    gancho: str = Field(description="texto curto (até 6 palavras) que aparece no topo nos primeiros 3 segundos")
    titulo: str = Field(description="título do post, até 90 caracteres")
    descricao: str = Field(description="legenda do post, 1 ou 2 frases, sem hashtags")
    hashtags: List[str] = Field(description="de 3 a 6 hashtags, sem o #")
    nota: int = Field(description="1 a 10: quanto o trecho funciona sozinho como clipe para TikTok/Reels")


SISTEMA = """Você é editor de cortes de live da Twitch para TikTok, Reels e YouTube Shorts.
Recebe a transcrição (com o segundo de cada frase) de uma janela da live e o segundo do pico,
que é o momento em que o chat, o áudio ou um espectador indicou algo marcante.

Decida:
- inicio e fim: o trecho final, entre 15 e 60 segundos, contendo o pico. Comece um pouco antes
  do contexto necessário para entender o lance e termine logo depois da reação; corte o tempo morto.
  Os dois valores devem ficar dentro da janela informada.
- gancho: frase curta, em português do Brasil, que faça a pessoa parar de rolar a tela.
  Descreva o que acontece, sem inventar fatos que não estejam na transcrição. Sem emoji.
- titulo, descricao e hashtags para o post. Linguagem natural de internet brasileira, sem emoji,
  sem exagero, sem prometer o que o clipe não mostra.
- nota: seja exigente. Use 1 a 3 quando não há nada que se sustente sozinho
  (fala solta, sem contexto, silêncio), 4 a 6 para um momento razoável e 7 a 10 só para lances fortes.

Se a transcrição estiver vazia ou confusa (áudio do jogo, outra língua), baseie-se no pico,
mantenha o trecho em torno dele e dê uma nota baixa."""


def editar(palavras: List[Palavra], duracao: float, pico: float, contexto: str, api_key: str = "") -> dict:
    padrao = {"inicio": 0.0, "fim": duracao, "gancho": "", "titulo": contexto,
              "descricao": "", "hashtags": [], "nota": None, "ia": False}
    chave = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not chave:
        return padrao
    import anthropic

    cliente = anthropic.Anthropic(api_key=chave, timeout=120, max_retries=2)
    texto = transcricao_com_tempo(palavras) or "(sem fala detectada)"
    pedido = (f"Contexto: {contexto}\n"
              f"Janela: de 0.0 a {duracao:.1f} segundos. Pico em {pico:.1f} s.\n\n"
              f"Transcrição:\n{texto}")
    try:
        resp = cliente.beta.messages.parse(
            model="claude-opus-5-5",
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=SISTEMA,
            messages=[{"role": "user", "content": pedido}],
            output_format=Edicao,
        )
    except anthropic.AuthenticationError:
        padrao["erro_ia"] = "chave da API inválida"
        return padrao
    except anthropic.APIError as e:
        padrao["erro_ia"] = f"API: {str(e)[:120]}"
        return padrao
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        padrao["erro_ia"] = f"sem edição ({resp.stop_reason})"
        return padrao
    e = resp.parsed_output
    ini = max(0.0, min(e.inicio, duracao - 5))
    fim = min(duracao, max(e.fim, ini + 5))
    if fim - ini > 62:
        fim = ini + 60
    return {"inicio": round(ini, 2), "fim": round(fim, 2), "gancho": e.gancho.strip()[:60],
            "titulo": e.titulo.strip()[:100], "descricao": e.descricao.strip()[:300],
            "hashtags": [re.sub(r"[^\w]", "", h, flags=re.U) for h in e.hashtags][:6],
            "nota": max(1, min(10, e.nota)), "ia": True}


# ---------------------------------------------------------------- legenda ASS
def _t(s):
    s = max(0.0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def _limpa(s):
    return re.sub(r"[{}\\]", "", s)


def gerar_ass(caminho, palavras: List[Palavra], inicio: float, fim: float, y_legenda: int,
              gancho: str = "", y_gancho: int = 300, marca: str = ""):
    """Escreve o .ass com tempos relativos ao trecho final [inicio, fim]."""
    ws = [Palavra(w.texto, w.ini - inicio, w.fim - inicio) for w in palavras
          if w.fim > inicio and w.ini < fim]
    blocos, atual = [], []
    for w in ws:
        if atual and (len(atual) >= 3 or w.ini - atual[-1].fim > 0.6):
            blocos.append(atual)
            atual = []
        atual.append(w)
    if atual:
        blocos.append(atual)

    eventos = []
    for bi, b in enumerate(blocos):
        fim_bloco = b[-1].fim + 0.15
        if bi + 1 < len(blocos):
            fim_bloco = min(fim_bloco, blocos[bi + 1][0].ini)
        for wi, w in enumerate(b):
            ini_w = w.ini if wi else b[0].ini
            fim_w = b[wi + 1].ini if wi + 1 < len(b) else fim_bloco
            if fim_w <= ini_w:
                continue
            partes = []
            for wj, x in enumerate(b):
                cor = AMARELO if wj == wi else BRANCO
                partes.append(f"{{\\c{cor}}}{_limpa(x.texto).upper()}")
            eventos.append(f"Dialogue: 0,{_t(ini_w)},{_t(fim_w)},L,,0,0,0,,{{\\pos(540,{y_legenda})}}" + " ".join(partes))

    if gancho:
        eventos.append(f"Dialogue: 1,{_t(0)},{_t(3.2)},G,,0,0,0,,"
                       f"{{\\pos(540,{y_gancho})\\fad(0,250)}}{_limpa(gancho).upper()}")
    if marca:
        eventos.append(f"Dialogue: 2,{_t(0)},{_t(fim - inicio + 1)},M,,0,0,0,,{_limpa(marca)}")

    estilos = (
        f"Style: L,{FONTE},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,8,4,5,60,60,0,1\n"
        f"Style: G,{FONTE},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,3,22,0,5,60,60,0,1\n"
        f"Style: M,{FONTE},44,&H64FFFFFF,&H00FFFFFF,&H96000000,&H00000000,0,0,0,0,100,100,1,0,1,3,0,9,40,40,40,1\n")
    caminho.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n" + estilos +
        "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(eventos) + "\n", encoding="utf-8")
    return bool(eventos)
