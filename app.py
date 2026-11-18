"""Clipador: grava a live da Twitch e gera clipes verticais (9:16) para TikTok/Reels.

Sinais de "momento bom": marcação manual, pico no chat, pico de áudio e clipes
criados por espectadores na Twitch. Os candidatos automáticos recebem uma nota;
a cada bloco de tempo os melhores são cortados, legendados e aprovados sozinhos.

Uso:  .venv\\Scripts\\python.exe app.py [--sem-tunel]
"""
import json
import random
import re
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

import edicao

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
DADOS_PATH = BASE / "dados.json"
SEG = 6  # duração alvo de cada segmento gravado, em segundos

PADRAO = {
    "canal": "gilzera",
    "porta": 8787,
    "senha": None,
    "qualidade": "best",
    "antes": 45,
    "depois": 15,
    "modo": "desfocado",          # "desfocado" ou "dividido" (câmera em cima, jogo embaixo)
    "camera": None,               # {"x","y","w","h"} em frações do quadro (0..1)
    "camera_altura": 0.38,
    "jogo_largura": 1.0,
    "twitch_client_id": "",
    "twitch_client_secret": "",
    "auto": {"por_hora": 6, "bloco_min": 30, "nota_minima": 2.0, "margem_s": 180},
    "chat": {"janela": 10, "historico": 300, "fator": 2.5, "minimo": 18, "intervalo": 90, "atraso": 10},
    "audio": {"limiar": 8, "piso": -32, "intervalo": 60},
    "legenda": {"ativa": True, "modelo": "small"},
    "edicao": {"ativa": False, "zoom": True, "volume_lufs": -14, "aprovar_nota_minima": 4},
    "anthropic_api_key": "",
    "jogo": "GTA VI",
    "marca": "",                  # ex.: "@gilzeracortes" no canto do vídeo; vazio = sem marca
}

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or str(Path(FFMPEG).with_name("ffprobe.exe"))
STREAMLINK = str(BASE / ".venv" / "Scripts" / "streamlink.exe")
if not Path(STREAMLINK).exists():
    STREAMLINK = shutil.which("streamlink") or "streamlink"
CLOUDFLARED = shutil.which("cloudflared") or r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(*a):
    print(datetime.now().strftime("[%H:%M:%S]"), *a, flush=True)


# ---------------------------------------------------------------- config
def carregar_cfg():
    cfg = json.loads(json.dumps(PADRAO))
    if CFG_PATH.exists():
        salvo = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))   # aceita BOM do Bloco de Notas
        for k, v in salvo.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg["senha"]:
        cfg["senha"] = secrets.token_urlsafe(9)
    CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


CFG = carregar_cfg()
REC = BASE / "gravacao"
CLIPES = BASE / "clipes"
APROVADOS = CLIPES / "aprovados"
TMP = BASE / "tmp"
for p in (REC, CLIPES, APROVADOS, TMP):
    p.mkdir(parents=True, exist_ok=True)


def salvar_cfg():
    CFG_PATH.write_text(json.dumps(CFG, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- dados
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.clipes = []
        if DADOS_PATH.exists():
            self.clipes = json.loads(DADOS_PATH.read_text(encoding="utf-8"))
            for c in self.clipes:  # corte interrompido por queda do programa volta para a fila
                if c["status"] == "cortando":
                    c["status"] = "aguardando"

    def salvar(self):
        tmp = DADOS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.clipes, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(DADOS_PATH)

    def get(self, cid):
        for c in self.clipes:
            if c["id"] == cid:
                return c
        raise HTTPException(404, "clipe não encontrado")

    def mudar(self, c, **kw):
        with self.lock:
            c.update(kw)
            c["v"] = c.get("v", 0) + 1
            self.salvar()


DB = Store()


def agora():
    return datetime.now()


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def de_iso(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def pontuar(s):
    """Nota de um candidato a partir dos sinais que ele juntou."""
    p = 0.0
    if s.get("chat"):
        p += min(8.0, (s["chat"] - 1) * 2.5)      # quantas vezes acima do ritmo normal do chat
    if s.get("audio"):
        p += min(8.0, (s["audio"] - 4) * 0.8)     # dB acima do volume normal
    if s.get("twitch"):
        p += 6.0 * s["twitch"]                    # cada clipe criado por espectador
    if sum(1 for k in ("chat", "audio", "twitch") if s.get(k)) >= 2:
        p += 2.0                                  # sinais diferentes concordando
    return round(p, 1)


def marcar(t, origem, quem=None, nota="", sinais=None):
    """Cria uma marcação ou junta com uma vizinha ainda não cortada (até 25 s de distância)."""
    sinais = sinais or {}
    with DB.lock:
        for c in reversed(DB.clipes[-40:]):
            if c["status"] not in ("candidato", "aguardando"):
                continue
            t0 = de_iso(c["t"])
            if abs((t - t0).total_seconds()) > 25:
                continue
            ini = min(t0, t)
            fim = max(t0 + timedelta(seconds=c["depois"]), t + timedelta(seconds=CFG["depois"]))
            s = dict(c.get("sinais") or {})
            for k, v in sinais.items():
                s[k] = s.get(k, 0) + v if k == "twitch" else max(s.get(k, 0), v)
            upd = {"t": iso(ini), "depois": int((fim - ini).total_seconds()), "sinais": s, "nota_auto": pontuar(s)}
            if quem:
                upd["quem"] = sorted(set(c["quem"]) | {quem})
            if nota:
                upd["nota"] = " / ".join(x for x in (c.get("nota"), nota) if x)
            if origem == "manual":
                upd["origem"] = "manual"
                upd["status"] = "aguardando"
            DB.mudar(c, **upd)
            return c
        c = {
            "id": t.strftime("%H%M%S") + "-" + secrets.token_hex(2), "t": iso(t), "origem": origem,
            "quem": [quem] if quem else [], "nota": nota, "antes": CFG["antes"], "depois": CFG["depois"],
            "sinais": sinais, "nota_auto": pontuar(sinais), "auto": False,
            "status": "aguardando" if origem == "manual" else "candidato",
            "arquivo": None, "cru": None, "erro": None, "v": 0,
        }
        DB.clipes.append(c)
        DB.salvar()
        log(f"novo {c['status']} ({origem}) {c['t'][11:]} sinais={sinais}")
        return c


# ---------------------------------------------------------------- gravação
ESTADO = {"gravador": "iniciando", "chat": "desconectado", "tunel": None, "encoder": None,
          "twitch": "desligado", "legenda": "desligada", "audio_base": None}
SEG_RE = re.compile(r"seg_(\d{8}-\d{6})\.ts$")


def gravador():
    while True:
        ESTADO["gravador"] = "procurando a live"
        sl = subprocess.Popen(
            [STREAMLINK, f"https://twitch.tv/{CFG['canal']}", CFG["qualidade"],
             "--twitch-disable-ads", "--loglevel", "error", "-O"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        ff = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
             "-f", "segment", "-segment_time", str(SEG), "-strftime", "1",
             str(REC / "seg_%Y%m%d-%H%M%S.ts")],
            stdin=sl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        sl.stdout.close()
        ESTADO["gravador"] = "conectado"
        ff.wait()
        sl.wait()
        ESTADO["gravador"] = "offline"
        time.sleep(20)


def segmentos():
    out = []
    for p in sorted(REC.glob("seg_*.ts")):
        m = SEG_RE.search(p.name)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y%m%d-%H%M%S"), p))
    return out


def status_gravacao():
    segs = segmentos()
    if not segs:
        return {"ao_vivo": False, "ultimo": None, "inicio": None}
    idade = time.time() - segs[-1][1].stat().st_mtime
    inicio = segs[-1][0]
    for i in range(len(segs) - 1, 0, -1):  # início da sessão atual (sem buracos > 60 s)
        if (segs[i][0] - segs[i - 1][0]).total_seconds() > 60:
            break
        inicio = segs[i - 1][0]
    return {"ao_vivo": idade < 25, "ultimo": round(idade), "inicio": iso(inicio)}


def ha_gravacao_em(t):
    g = status_gravacao()
    return g["ao_vivo"] and de_iso(g["inicio"]) <= t


# ---------------------------------------------------------------- legenda (Whisper)
_WHISPER = None


def whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        ESTADO["legenda"] = "carregando modelo"
        _WHISPER = WhisperModel(CFG["legenda"]["modelo"], device="cpu", compute_type="int8")
        ESTADO["legenda"] = f"Whisper {CFG['legenda']['modelo']}"
    return _WHISPER


def ler_wav(wav):
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as w:   # lê o PCM direto, sem depender do PyAV
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


FONTES = BASE / "fontes"
FONTES_FF = str(FONTES).replace("\\", "/").replace(":", "\\:")   # formato que o filtro ass do ffmpeg aceita


def fps_da_fonte(seg):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(seg)],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        n, d = r.stdout.strip().split("/")
        return max(24, min(60, round(int(n) / int(d))))
    except Exception:
        return 60


# ---------------------------------------------------------------- corte
def detectar_encoder():
    teste = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=black:s=1080x1920:d=1", "-c:v", "h264_qsv", "-f", "null", "-"]
    try:
        ok = subprocess.run(teste, capture_output=True, timeout=30, creationflags=NO_WINDOW).returncode == 0
    except Exception:
        ok = False
    if ok:
        ESTADO["encoder"] = "Intel QSV"
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21", "-b:v", "0"]
    ESTADO["encoder"] = "CPU (x264)"
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def dividido():
    return CFG["modo"] == "dividido" and bool(CFG.get("camera"))


def filtro_vertical(fim="format=yuv420p"):
    cam = CFG.get("camera")
    if dividido():
        ht = int(1920 * CFG["camera_altura"]) // 2 * 2
        hb = 1920 - ht
        r = 1080 / hb * CFG["jogo_largura"]
        jogo = (f"[b]crop=min(iw\\,ih*{r:.4f}):ih:(iw-min(iw\\,ih*{r:.4f}))/2:0,"
                f"scale=1080:{hb}:force_original_aspect_ratio=decrease,"
                f"pad=1080:{hb}:(ow-iw)/2:(oh-ih)/2,setsar=1[game]")
        return (f"[0:v]split=2[a][b];"
                f"[a]crop=iw*{cam['w']:.4f}:ih*{cam['h']:.4f}:iw*{cam['x']:.4f}:ih*{cam['y']:.4f},"
                f"scale=1080:{ht}:force_original_aspect_ratio=increase,crop=1080:{ht},setsar=1[cam];"
                f"{jogo};[cam][game]vstack=inputs=2,{fim}[v]")
    return ("[0:v]split=2[a][b];"
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,setsar=1[bg];[b]scale=1080:-2,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{fim}[v]")


def posicoes_texto():
    """(y da legenda, y do gancho) no quadro 1080x1920."""
    if dividido():
        ht = int(1920 * CFG["camera_altura"])
        return ht + 200, ht       # legenda sobre o jogo, gancho na divisa câmera/jogo
    return 1430, 420              # legenda abaixo do vídeo, gancho no fundo desfocado de cima


def nome_arquivo(c):
    nota = re.sub(r"[^\w\- ]", "", c.get("nota") or "", flags=re.U).strip().replace(" ", "-")[:40]
    t = de_iso(c["t"]).strftime("%Y%m%d-%H%M%S")
    return f"{t}_{c['id'][-4:]}" + (f"_{nota}" if nota else "")


def cortar(c, encoder):
    t = de_iso(c["t"])
    a = t - timedelta(seconds=c["antes"])
    b = t + timedelta(seconds=c["depois"])
    segs = segmentos()
    sel = []
    for i, (s, p) in enumerate(segs):
        e = segs[i + 1][0] if i + 1 < len(segs) else s + timedelta(seconds=SEG + 3)
        e = min(e, s + timedelta(seconds=SEG * 3))
        if s < b and e > a:
            sel.append((s, p))
    if not sel:
        raise RuntimeError("não há gravação nesse horário")
    inicio_janela = max(a, sel[0][0])
    offset = max(0.0, (a - sel[0][0]).total_seconds())
    dur = (b - inicio_janela).total_seconds()
    pico = max(0.0, min(dur, (t - inicio_janela).total_seconds()))
    entrada = "concat:" + "|".join(p.name for _, p in sel)
    nome = nome_arquivo(c)
    vert = CLIPES / f"{nome}_vertical.mp4"
    cru = CLIPES / f"{nome}_horizontal.mp4"
    txt = CLIPES / f"{nome}_post.txt"
    run = lambda args: subprocess.run(args, cwd=REC, capture_output=True, text=True, creationflags=NO_WINDOW)
    janela = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", entrada, "-t", f"{dur:.2f}"]

    # 1. transcrição da janela inteira
    palavras = []
    if CFG["legenda"]["ativa"]:
        wav = TMP / f"{c['id']}.wav"
        try:
            run(janela + ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
            palavras = edicao.transcrever(whisper(), ler_wav(wav))
        except Exception as e:
            log("transcrição falhou:", e)
        finally:
            wav.unlink(missing_ok=True)

    # 2. edição (Claude): trecho final, gancho, título
    sinais = ", ".join(f"{k}={v}" for k, v in (c.get("sinais") or {}).items()) or "marcação manual"
    contexto = f"Live de twitch.tv/{CFG['canal']} jogando {CFG['jogo']}. Sinais do momento: {sinais}."
    if c.get("nota"):
        contexto += f" Nota de quem marcou: {c['nota']}."
    editar = CFG["edicao"]["ativa"]
    if editar:
        ed = edicao.editar(palavras, dur, pico, contexto, CFG.get("anthropic_api_key", ""))
    else:   # edição desligada: janela inteira, sem IA, sem gancho
        ed = {"inicio": 0.0, "fim": dur, "gancho": "", "titulo": "", "descricao": "",
              "hashtags": [], "nota": None, "ia": False}
    ini, fim = ed["inicio"], ed["fim"]

    # 3. legenda karaokê + gancho + marca
    y_leg, y_gancho = posicoes_texto()
    ass = REC / f"_leg_{c['id']}.ass"
    marca = CFG.get("marca", "") if editar else ""
    tem_texto = edicao.gerar_ass(ass, palavras, ini, fim, y_leg, ed["gancho"], y_gancho, marca)
    fim_filtro = "format=yuv420p"
    if tem_texto:
        fim_filtro = f"ass={ass.name}:fontsdir='{FONTES_FF}',format=yuv420p"

    # 4. zoom rápido no pico
    p = pico - ini
    if editar and CFG["edicao"]["zoom"] and 0.5 < p < (fim - ini) - 1:
        fps = fps_da_fonte(sel[0][1])
        z = f"1+0.12*min(1\\,max(0\\,(it-{p:.2f})/0.12))*min(1\\,max(0\\,({p + 0.9:.2f}-it)/0.25))"
        fim_filtro = (f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps={fps},"
                      + fim_filtro)

    final = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{offset + ini:.2f}", "-i", entrada,
             "-t", f"{fim - ini:.2f}"]
    audio = ["-af", f"loudnorm=I={CFG['edicao']['volume_lufs']}:TP=-1.5:LRA=11"] if editar else []
    r = run(final + ["-filter_complex", filtro_vertical(fim_filtro), "-map", "[v]", "-map", "0:a:0?",
                     *audio, *encoder, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(vert)])
    ass.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-400:] or "ffmpeg falhou")
    # versão horizontal da janela inteira, sem edição, para quem quiser editar na mão
    run(janela + ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(cru)])

    if editar:
        hashtags = " ".join("#" + h for h in ed["hashtags"])
        txt.write_text(f"{ed['titulo']}\n\n{ed['descricao']}\n\n{hashtags}\n".strip() + "\n", encoding="utf-8")
    else:
        txt = None
    info = {"legenda": bool(palavras), "titulo": ed["titulo"], "descricao": ed["descricao"],
            "hashtags": ed["hashtags"], "gancho": ed["gancho"], "nota_ia": ed["nota"], "ia": ed["ia"],
            "erro_ia": ed.get("erro_ia"), "corte": [round(ini, 1), round(fim, 1)] if editar else None, "post": txt.name if txt else None}
    return vert.name, (cru.name if cru.exists() else None), info


def aprovar_arquivos(vert, post):
    shutil.copy2(CLIPES / vert, APROVADOS / vert)
    if post and (CLIPES / post).exists():
        shutil.copy2(CLIPES / post, APROVADOS / post)


def desaprovar_arquivos(c):
    for k in ("arquivo", "post"):
        if c.get(k):
            (APROVADOS / c[k]).unlink(missing_ok=True)


def cortador():
    encoder = detectar_encoder()
    if CFG["legenda"]["ativa"]:
        try:
            whisper()
        except Exception as e:
            ESTADO["legenda"] = "erro ao carregar"
            log("Whisper não carregou:", e)
            CFG["legenda"]["ativa"] = False
    while True:
        pendente = None
        with DB.lock:
            fila = [c for c in DB.clipes if c["status"] == "aguardando"
                    and agora() >= de_iso(c["t"]) + timedelta(seconds=c["depois"] + SEG + 4)]
            fila.sort(key=lambda c: (c["origem"] != "manual", c["t"]))   # manuais primeiro
            if fila:
                pendente = fila[0]
                DB.mudar(pendente, status="cortando", erro=None)
        if not pendente:
            time.sleep(1.5)
            continue
        for chave in ("arquivo", "cru", "post"):
            if pendente.get(chave):
                (CLIPES / pendente[chave]).unlink(missing_ok=True)
        inicio = time.time()
        try:
            vert, cru, info = cortar(pendente, encoder)
            upd = {"arquivo": vert, "cru": cru, "tempo_corte": round(time.time() - inicio), **info}
            nota_ia = info.get("nota_ia")
            fraco = nota_ia is not None and nota_ia < CFG["edicao"]["aprovar_nota_minima"]
            if pendente.get("auto") and not fraco:
                aprovar_arquivos(vert, info.get("post"))
                DB.mudar(pendente, status="aprovado", aprovado_por="automático", **upd)
            else:
                if fraco:
                    upd["obs"] = f"a IA deu nota {nota_ia}; ficou para revisão em vez de aprovar sozinho"
                DB.mudar(pendente, status="pronto", **upd)
            log(f"cortado {vert} em {upd['tempo_corte']} s (ia={info['ia']} nota={nota_ia} {info.get('erro_ia') or ''})")
        except Exception as e:
            DB.mudar(pendente, status="erro", erro=str(e))
            log("erro no corte:", e)


# ---------------------------------------------------------------- seleção automática
def seletor():
    """Fecha blocos de tempo e manda os melhores candidatos de cada um para o corte."""
    while True:
        time.sleep(20)
        try:
            a = CFG["auto"]
            bloco = timedelta(minutes=a["bloco_min"])
            vagas = max(1, round(a["por_hora"] * a["bloco_min"] / 60))
            g = status_gravacao()
            live_parada = not g["ao_vivo"] and (g["ultimo"] or 0) > a["margem_s"]
            with DB.lock:
                blocos = {}
                for c in DB.clipes:
                    t = de_iso(c["t"])
                    chave = t.replace(minute=t.minute - t.minute % a["bloco_min"], second=0)
                    blocos.setdefault(chave, []).append(c)
                for chave, lista in blocos.items():
                    fechado = agora() >= chave + bloco + timedelta(seconds=a["margem_s"])
                    cand = [c for c in lista if c["status"] == "candidato"]
                    if not cand or not (fechado or live_parada):
                        continue
                    usadas = sum(1 for c in lista if c.get("auto"))
                    cand.sort(key=lambda c: c.get("nota_auto", 0), reverse=True)
                    for c in cand:
                        if usadas < vagas and c.get("nota_auto", 0) >= a["nota_minima"]:
                            DB.mudar(c, status="aguardando", auto=True)
                            usadas += 1
                        else:
                            DB.mudar(c, status="sobra")
        except Exception:
            log("seletor:", traceback.format_exc(limit=2))


# ---------------------------------------------------------------- sinais: chat
RISO = re.compile(r"k{3,}|kek|lul|omegalul|icant|pog|monka|wtf|hah{2,}|rsrs|caralho|porra|meu deus|clip", re.I)


class Chat:
    def __init__(self):
        self.msgs = deque()
        self.hist = deque()
        self.ultimo_disparo = 0
        self.serie = deque(maxlen=60)
        self.atual = 0
        self.base = 0

    def registrar(self, texto):
        self.msgs.append((time.time(), 2 if RISO.search(texto) else 1))

    def detector(self):
        cc = CFG["chat"]
        passo = 0
        while True:
            time.sleep(1)
            try:
                now = time.time()
                while self.msgs and self.msgs[0][0] < now - cc["janela"]:
                    self.msgs.popleft()
                self.atual = sum(p for _, p in self.msgs)
                passo += 1
                if passo % 5 == 0:
                    self.serie.append(self.atual)
                if passo % cc["janela"] == 0:
                    self.hist.append(self.atual)
                    while len(self.hist) > cc["historico"] // cc["janela"]:
                        self.hist.popleft()
                self.base = statistics.median(self.hist) if len(self.hist) >= 6 else 0
                limite = max(cc["minimo"], self.base * cc["fator"]) if self.base else cc["minimo"] * 2
                if self.atual >= limite and now - self.ultimo_disparo > cc["intervalo"]:
                    t = agora() - timedelta(seconds=cc["atraso"])
                    if ha_gravacao_em(t):
                        self.ultimo_disparo = now
                        razao = self.atual / max(self.base, cc["minimo"] / 2)
                        marcar(t, "chat", sinais={"chat": round(razao, 1)})
            except Exception:
                log("chat:", traceback.format_exc(limit=2))

    def conexao(self):
        while True:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=360)
                s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                s.sendall(f"JOIN #{CFG['canal'].lower()}\r\n".encode())
                ESTADO["chat"] = "conectado"
                buf = b""
                while True:
                    dado = s.recv(8192)
                    if not dado:
                        break
                    buf += dado
                    while b"\r\n" in buf:
                        linha, buf = buf.split(b"\r\n", 1)
                        linha = linha.decode("utf-8", "ignore")
                        if linha.startswith("PING"):
                            s.sendall(linha.replace("PING", "PONG", 1).encode() + b"\r\n")
                        elif " PRIVMSG " in linha:
                            partes = linha.split(" PRIVMSG ", 1)[1].split(" :", 1)
                            if len(partes) == 2:
                                self.registrar(partes[1])
            except Exception:
                pass
            ESTADO["chat"] = "reconectando"
            time.sleep(5)


CHAT = Chat()


# ---------------------------------------------------------------- sinais: áudio
LUFS_RE = re.compile(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+)")


def audio_detector():
    """Mede o volume (LUFS momentâneo) de cada segmento novo e marca picos acima do normal."""
    cfg = CFG["audio"]
    valores = deque(maxlen=300)   # um valor por segundo, últimos 5 min
    feitos = set()
    ultimo = 0.0
    while True:
        time.sleep(2)
        try:
            segs = segmentos()
            for s, p in segs[-6:-1]:   # só segmentos já fechados
                if p.name in feitos:
                    continue
                feitos.add(p.name)
                if (agora() - s).total_seconds() > 90:
                    continue
                r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", p.name, "-vn", "-af", "ebur128",
                                    "-f", "null", "-"], cwd=REC, capture_output=True, text=True,
                                   creationflags=NO_WINDOW)
                pontos = [(float(m[1]), float(m[2])) for m in LUFS_RE.finditer(r.stderr)]
                if not pontos:
                    continue
                t0 = pontos[0][0]
                por_seg = {}
                for t, m in pontos:
                    por_seg.setdefault(int(t - t0), []).append(m)
                for sec in sorted(por_seg):
                    v = max(por_seg[sec])
                    if v < -70:
                        continue
                    if len(valores) >= 60:
                        base = statistics.median(valores)
                        ESTADO["audio_base"] = round(base, 1)
                        excesso = v - base
                        quando = s + timedelta(seconds=sec)
                        if excesso >= cfg["limiar"] and v >= cfg["piso"] and time.time() - ultimo > cfg["intervalo"]:
                            ultimo = time.time()
                            marcar(quando, "audio", sinais={"audio": round(excesso, 1)})
                    valores.append(v)
            if len(feitos) > 5000:
                feitos = set(list(feitos)[-500:])
        except Exception:
            log("áudio:", traceback.format_exc(limit=2))


# ---------------------------------------------------------------- sinais: clipes da Twitch
def twitch_clipes():
    cid, segredo = CFG.get("twitch_client_id"), CFG.get("twitch_client_secret")
    if not cid or not segredo:
        ESTADO["twitch"] = "sem app configurado"
        return
    token, bid, vistos = None, None, set()

    def api(caminho):
        req = urllib.request.Request("https://api.twitch.tv/helix/" + caminho,
                                     headers={"Client-Id": cid, "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    while True:
        try:
            if not token:
                corpo = urllib.parse.urlencode({"client_id": cid, "client_secret": segredo,
                                                "grant_type": "client_credentials"}).encode()
                with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", data=corpo, timeout=20) as r:
                    token = json.load(r)["access_token"]
            if not bid:
                dados = api("users?login=" + CFG["canal"].lower())["data"]
                if not dados:
                    ESTADO["twitch"] = "canal não encontrado"
                    time.sleep(300)
                    continue
                bid = dados[0]["id"]
            desde = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            dados = api(f"clips?broadcaster_id={bid}&started_at={desde}&first=50")["data"]
            ESTADO["twitch"] = "conectado"
            for cl in dados:
                if cl["id"] in vistos:
                    continue
                vistos.add(cl["id"])
                criado = datetime.fromisoformat(cl["created_at"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                t = criado - timedelta(seconds=12)
                if ha_gravacao_em(t - timedelta(seconds=CFG["antes"] // 2)):
                    marcar(t, "twitch", sinais={"twitch": 1})
        except urllib.error.HTTPError as e:
            ESTADO["twitch"] = f"erro HTTP {e.code}"
            if e.code == 401:
                token = None
        except Exception as e:
            ESTADO["twitch"] = f"erro: {str(e)[:50]}"
        time.sleep(60)


# ---------------------------------------------------------------- túnel
def tunel():
    try:
        p = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{CFG['porta']}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return
    for linha in p.stderr:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", linha)
        if m and not ESTADO["tunel"]:
            ESTADO["tunel"] = m.group(0)
            print(f"\n  Link para o seu amigo:  {m.group(0)}/?k={CFG['senha']}\n", flush=True)


# ---------------------------------------------------------------- web
app = FastAPI()


@app.middleware("http")
async def autenticar(request: Request, call_next):
    k = request.query_params.get("k") or request.cookies.get("k")
    if k != CFG["senha"]:
        return HTMLResponse("<h3 style='font-family:sans-serif'>Acesso negado. Use o link com a chave.</h3>", 401)
    resp = await call_next(request)
    if request.query_params.get("k"):
        resp.set_cookie("k", k, max_age=14 * 86400, httponly=True, samesite="lax")
    return resp


@app.get("/")
def pagina():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/camera")
def pagina_camera():
    return FileResponse(BASE / "static" / "camera.html", headers={"Cache-Control": "no-store"})


@app.get("/api/estado")
def estado():
    livre = shutil.disk_usage(BASE).free / 1e9
    with DB.lock:
        clipes = DB.clipes[-400:]
    return {
        "canal": CFG["canal"], "gravacao": status_gravacao(), "gravador": ESTADO["gravador"],
        "chat": {"estado": ESTADO["chat"], "atual": CHAT.atual, "base": CHAT.base, "serie": list(CHAT.serie)},
        "encoder": ESTADO["encoder"], "livre_gb": round(livre, 1), "modo": CFG["modo"],
        "twitch": ESTADO["twitch"], "legenda": ESTADO["legenda"] if CFG["legenda"]["ativa"] else "desligada",
        "audio_base": ESTADO["audio_base"], "auto": CFG["auto"],
        "link_amigo": f"{ESTADO['tunel']}/?k={CFG['senha']}" if ESTADO["tunel"] else None,
        "agora": iso(agora()), "clipes": clipes,
    }


@app.post("/api/marcar")
async def api_marcar(request: Request):
    d = await request.json()
    quem = (d.get("quem") or "").strip()[:20] or "anônimo"
    nota = (d.get("nota") or "").strip()[:80]
    voltar = max(0, min(120, int(d.get("voltar") or 0)))
    return marcar(agora() - timedelta(seconds=voltar), "manual", quem, nota)


@app.post("/api/clipe/{cid}/{acao}")
async def api_acao(cid: str, acao: str, request: Request):
    c = DB.get(cid)
    d = {}
    try:
        d = await request.json()
    except Exception:
        pass
    if acao == "aprovar":
        if c["status"] != "pronto":
            raise HTTPException(400, "clipe ainda não está pronto")
        aprovar_arquivos(c["arquivo"], c.get("post"))
        DB.mudar(c, status="aprovado", aprovado_por=(d.get("quem") or "")[:20])
    elif acao == "descartar":
        desaprovar_arquivos(c)
        DB.mudar(c, status="descartado")
    elif acao == "restaurar":
        desaprovar_arquivos(c)
        DB.mudar(c, status="pronto" if c.get("arquivo") else "candidato", auto=False)
    elif acao == "cortar":
        DB.mudar(c, status="aguardando", auto=False)
    elif acao == "ajustar":
        antes = max(5, min(180, int(d.get("antes", c["antes"]))))
        depois = max(0, min(120, int(d.get("depois", c["depois"]))))
        nota = (d.get("nota", c.get("nota")) or "")[:80]
        desaprovar_arquivos(c)
        DB.mudar(c, antes=antes, depois=depois, nota=nota, status="aguardando", auto=False)
    elif acao == "nota":
        DB.mudar(c, nota=(d.get("nota") or "")[:80])
    else:
        raise HTTPException(400, "ação inválida")
    return c


@app.get("/arquivo/{nome}")
def arquivo(nome: str, baixar: int = 0):
    p = (CLIPES / nome).resolve()
    if p.parent != CLIPES.resolve() or not p.exists():
        raise HTTPException(404)
    tipo = "video/mp4" if p.suffix == ".mp4" else "text/plain; charset=utf-8"
    return FileResponse(p, media_type=tipo, filename=nome if baixar else None)


def quadro_atual():
    segs = segmentos()
    if len(segs) < 2:
        raise HTTPException(409, "ainda não há gravação para tirar um quadro")
    out = TMP / "quadro.png"
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(segs[-2][1]),
                    "-frames:v", "1", str(out)], capture_output=True, creationflags=NO_WINDOW)
    if not out.exists():
        raise HTTPException(500, "não consegui extrair o quadro")
    return out


@app.get("/api/quadro.png")
def api_quadro(novo: int = 0):
    out = TMP / "quadro.png"
    if novo or not out.exists():
        out = quadro_atual()
    return FileResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/previa.png")
def api_previa():
    q = TMP / "quadro.png"
    if not q.exists():
        q = quadro_atual()
    out = TMP / "previa.png"
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(q),
                        "-filter_complex", filtro_vertical("format=rgb24"), "-map", "[v]", "-frames:v", "1", str(out)],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise HTTPException(500, r.stderr[-300:])
    return FileResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/layout")
def get_layout():
    return {k: CFG[k] for k in ("modo", "camera", "camera_altura", "jogo_largura", "antes", "depois")}


@app.post("/api/layout")
async def set_layout(request: Request):
    d = await request.json()
    if d.get("modo") in ("desfocado", "dividido"):
        CFG["modo"] = d["modo"]
    if isinstance(d.get("camera"), dict):
        CFG["camera"] = {k: max(0.0, min(1.0, float(d["camera"][k]))) for k in ("x", "y", "w", "h")}
    for k, lo, hi in (("camera_altura", 0.2, 0.6), ("jogo_largura", 1.0, 2.0)):
        if k in d:
            CFG[k] = max(lo, min(hi, float(d[k])))
    for k, lo, hi in (("antes", 5, 180), ("depois", 0, 120)):
        if k in d:
            CFG[k] = max(lo, min(hi, int(d[k])))
    salvar_cfg()
    return get_layout()


# ---------------------------------------------------------------- main
def main():
    for alvo in (gravador, cortador, seletor, CHAT.conexao, CHAT.detector, audio_detector, twitch_clipes):
        threading.Thread(target=alvo, daemon=True).start()
    if "--sem-tunel" not in sys.argv:
        threading.Thread(target=tunel, daemon=True).start()
    print(f"\n  Clipador  |  canal: twitch.tv/{CFG['canal']}")
    print(f"  Painel local:  http://localhost:{CFG['porta']}/?k={CFG['senha']}")
    print("  (o link para o amigo aparece aqui em alguns segundos)\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=CFG["porta"], log_level="warning")


if __name__ == "__main__":
    main()
