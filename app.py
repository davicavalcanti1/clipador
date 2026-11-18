"""Clipador: grava lives da Twitch (vários canais ao mesmo tempo) e gera clipes verticais 9:16.

Cada canal tem a própria gravação, o próprio detector de chat e de áudio e a própria série
de sinais (mensagens por segundo, risadas, volume em LUFS, espectadores), guardada em
sinais/<canal>/AAAAMMDD.csv e mostrada em gráfico no painel. A fila de corte é única.

Sinais de "momento bom": marcação manual (botão ou trecho arrastado no gráfico), pico no chat,
pico de áudio e clipes criados por espectadores. Os candidatos automáticos recebem uma nota;
a cada bloco de tempo os melhores de cada canal são cortados, legendados e aprovados sozinhos.

Uso:  .venv\\Scripts\\python.exe app.py [--sem-tunel]
"""
import copy
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
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

import edicao

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
DADOS_PATH = BASE / "dados.json"
REC = BASE / "gravacao"
CLIPES = BASE / "clipes"
APROVADOS = CLIPES / "aprovados"
TMP = BASE / "tmp"
SINAIS = BASE / "sinais"
SEG = 6                 # duração alvo de cada segmento gravado, em segundos
MEMORIA_H = 12          # horas de série de sinais mantidas em memória para o gráfico
NOME_RE = re.compile(r"^[a-z0-9_]{2,25}$")
# opções de qualidade com fallback (os nomes variam por canal: 720p60, 720p30, 480p30...)
QUALIDADES = ("best", "1080p60,1080p,best", "720p60,720p,best", "720p30,720p,480p30,best", "480p30,480p,best", "worst")

CANAL_PADRAO = {
    "ativo": True,
    "qualidade": "best",
    "antes": 45,
    "depois": 15,
    "modo": "desfocado",          # "desfocado" ou "dividido" (câmera em cima, jogo embaixo)
    "camera": None,               # {"x","y","w","h"} em frações do quadro (0..1)
    "camera_altura": 0.38,
    "jogo_largura": 1.0,
    "jogo": "",                   # vazio = usa o jogo informado pela Twitch
    "marca": "",                  # ex.: "@gilzeracortes" no canto do vídeo; vazio = sem marca
}

PADRAO = {
    "porta": 8787,
    "senha": None,
    "canais": {},
    "twitch_client_id": "",
    "twitch_client_secret": "",
    "anthropic_api_key": "",
    "cortes_simultaneos": 2,
    "retencao_h": 24,             # apaga gravação bruta mais velha que isso (0 = nunca)
    "pasta_drive": "",            # ex.: G:/Meu Drive/Clipador (Google Drive para computador); vazio = fica no PC
    "auto": {"por_hora": 6, "bloco_min": 30, "nota_minima": 2.0, "margem_s": 180},
    "chat": {"janela": 10, "historico": 300, "fator": 2.5, "minimo": 18, "intervalo": 90,
             "reacao": 3, "por_pessoa": 3},
    "audio": {"limiar": 8, "piso": -32, "intervalo": 60},
    "legenda": {"ativa": True, "modelo": "small", "versoes": "ambas"},   # versoes: "com", "sem" ou "ambas"
    "edicao": {"ativa": False, "zoom": True, "volume_lufs": -14, "aprovar_nota_minima": 4},
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
CFG_LOCK = threading.RLock()
CANAL_MIGRADO = None    # canal da versão antiga (um canal só), para migrar gravação e clipes


def carregar_cfg():
    global CANAL_MIGRADO
    cfg = copy.deepcopy(PADRAO)
    salvo = {}
    if CFG_PATH.exists():
        salvo = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))   # aceita BOM do Bloco de Notas
    if "canal" in salvo:                                                # config da versão de um canal só
        nome = str(salvo.pop("canal")).lower()
        antigo = {k: salvo.pop(k) for k in list(CANAL_PADRAO) if k in salvo}
        salvo.setdefault("canais", {}).setdefault(nome, antigo)
        CANAL_MIGRADO = nome
    for k, v in salvo.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k != "canais":
            cfg[k].update(v)
        else:
            cfg[k] = v
    cfg["chat"].pop("atraso", None)   # substituído por "reacao"
    cfg["canais"] = {n.lower(): {**copy.deepcopy(CANAL_PADRAO), **(c or {})} for n, c in cfg["canais"].items()}
    if not cfg["senha"]:
        cfg["senha"] = secrets.token_urlsafe(9)
    CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


CFG = carregar_cfg()
for p in (REC, CLIPES, APROVADOS, TMP, SINAIS):
    p.mkdir(parents=True, exist_ok=True)


def salvar_cfg():
    with CFG_LOCK:
        CFG_PATH.write_text(json.dumps(CFG, indent=2, ensure_ascii=False), encoding="utf-8")


def cfg_canal(nome):
    return CFG["canais"].get(nome) or CANAL_PADRAO


def migrar_versao_antiga():
    """Segmentos soltos em gravacao/ (versão de um canal só) vão para gravacao/<canal>/."""
    soltos = list(REC.glob("seg_*.ts"))
    if not soltos:
        return
    destino_nome = CANAL_MIGRADO or next(iter(CFG["canais"]), "antigo")
    destino = REC / destino_nome
    destino.mkdir(exist_ok=True)
    for p in soltos:
        p.replace(destino / p.name)
    log(f"migrados {len(soltos)} segmentos antigos para gravacao/{destino_nome}/")


# ---------------------------------------------------------------- dados
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.clipes = []
        if DADOS_PATH.exists():
            self.clipes = json.loads(DADOS_PATH.read_text(encoding="utf-8"))
        padrao = CANAL_MIGRADO or next(iter(CFG["canais"]), "antigo")
        for c in self.clipes:
            c.setdefault("canal", padrao)
            if c["status"] == "cortando":   # corte interrompido por queda do programa volta para a fila
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
    if s.get("riso", 0) >= 0.4:
        p += 1.5                                  # o pico do chat é de risada, não de spam qualquer
    if sum(1 for k in ("chat", "audio", "twitch") if s.get(k)) >= 2:
        p += 2.0                                  # sinais diferentes concordando
    return round(p, 1)


def marcar(canal, t, origem, quem=None, nota="", sinais=None, antes=None, depois=None):
    """Cria uma marcação ou junta com uma vizinha do mesmo canal ainda não cortada (até 25 s)."""
    sinais = sinais or {}
    cc = cfg_canal(canal)
    antes = cc["antes"] if antes is None else antes
    depois = cc["depois"] if depois is None else depois
    with DB.lock:
        if origem != "trecho":
            for c in reversed(DB.clipes[-80:]):
                if c.get("canal") != canal or c["status"] not in ("candidato", "aguardando"):
                    continue
                t0 = de_iso(c["t"])
                if abs((t - t0).total_seconds()) > 25:
                    continue
                ini = min(t0, t)
                fim = max(t0 + timedelta(seconds=c["depois"]), t + timedelta(seconds=depois))
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
        manual = origem in ("manual", "trecho")
        c = {
            "id": t.strftime("%H%M%S") + "-" + secrets.token_hex(2), "canal": canal, "t": iso(t), "origem": origem,
            "quem": [quem] if quem else [], "nota": nota, "antes": antes, "depois": depois,
            "sinais": sinais, "nota_auto": pontuar(sinais), "auto": False,
            "status": "aguardando" if manual else "candidato",
            "arquivo": None, "cru": None, "erro": None, "v": 0,
        }
        DB.clipes.append(c)
        DB.salvar()
        log(f"[{canal}] novo {c['status']} ({origem}) {c['t'][11:]} sinais={sinais}")
        return c


# ---------------------------------------------------------------- série de sinais (gráficos)
class Serie:
    """Um registro por segundo: mensagens, risadas, janela do detector, limite, volume, base do volume, espectadores."""
    CAMPOS = ("m", "r", "j", "l", "db", "dbb", "esp")
    IDX = {k: i for i, k in enumerate(CAMPOS)}

    def __init__(self, nome):
        self.lock = threading.Lock()
        self.dados = {}
        self.dir = SINAIS / nome
        self.dir.mkdir(parents=True, exist_ok=True)
        self.gravado_ate = 0
        self.carregar()

    def por(self, ts, **kw):
        ts = int(ts)
        with self.lock:
            row = self.dados.get(ts)
            if row is None:
                row = self.dados[ts] = [None] * len(self.CAMPOS)
            for k, v in kw.items():
                row[self.IDX[k]] = v

    def carregar(self):
        limite = time.time() - MEMORIA_H * 3600
        for arq in sorted(self.dir.glob("*.csv"))[-2:]:
            for linha in arq.read_text(encoding="utf-8").splitlines()[1:]:
                partes = linha.split(",")
                try:
                    ts = int(partes[0])
                except ValueError:
                    continue
                if ts < limite:
                    continue
                self.dados[ts] = [float(x) if x else None for x in partes[1:1 + len(self.CAMPOS)]]
                self.gravado_ate = max(self.gravado_ate, ts)

    def gravar(self):
        """Grava em disco os segundos já fechados (o áudio chega uns 15 s depois do chat)."""
        corte = int(time.time()) - 45
        with self.lock:
            novos = sorted(ts for ts in self.dados if self.gravado_ate < ts <= corte)
            linhas = defaultdict(list)
            for ts in novos:
                dia = datetime.fromtimestamp(ts).strftime("%Y%m%d")
                linhas[dia].append(f"{ts}," + ",".join("" if v is None else f"{v:g}" for v in self.dados[ts]))
            if novos:
                self.gravado_ate = novos[-1]
            velho = time.time() - MEMORIA_H * 3600
            for ts in [ts for ts in self.dados if ts < velho]:
                del self.dados[ts]
        for dia, ls in linhas.items():
            arq = self.dir / f"{dia}.csv"
            novo = not arq.exists()
            with arq.open("a", encoding="utf-8") as f:
                if novo:
                    f.write("ts," + ",".join(self.CAMPOS) + "\n")
                f.write("\n".join(ls) + "\n")

    def faixa(self, de, ate, passo):
        """Agrega [de, ate) em baldes de `passo` segundos."""
        de, ate, passo = int(de), int(ate), max(1, int(passo))
        n = max(1, (ate - de) // passo)
        out = {k: [None] * n for k in self.CAMPOS}
        soma = {"m", "r"}
        with self.lock:
            for i in range(n):
                b0 = de + i * passo
                for ts in range(b0, b0 + passo):
                    row = self.dados.get(ts)
                    if row is None:
                        continue
                    for k, v in zip(self.CAMPOS, row):
                        if v is None:
                            continue
                        atual = out[k][i]
                        if k in soma:
                            out[k][i] = (atual or 0) + v
                        elif k in ("j", "db"):
                            out[k][i] = v if atual is None else max(atual, v)
                        else:
                            out[k][i] = v
        return {"de": de, "passo": passo, **out}


# ---------------------------------------------------------------- gravação
SEG_RE = re.compile(r"seg_(\d{8}-\d{6})\.ts$")


def listar_segmentos(pasta):
    out = []
    for p in sorted(pasta.glob("seg_*.ts")):
        m = SEG_RE.search(p.name)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y%m%d-%H%M%S"), p))
    return out


RISO = re.compile(r"k{3,}|kek|lul|omegalul|icant|pog|monka|wtf|hah{2,}|rsrs|caralho|porra|meu deus|clip", re.I)


class Canal:
    def __init__(self, nome):
        self.nome = nome
        self.dir = REC / nome
        self.dir.mkdir(parents=True, exist_ok=True)
        self.parar_ev = threading.Event()
        self.procs = []
        self.estado = "parado"
        self.serie = Serie(nome)
        self._segs = (0.0, [])
        self.seq = {}                        # nome do segmento -> número de sequência estável (player HLS)
        # chat
        self.msgs = deque()                  # (ts, usuário, peso, riso)
        self.cont = defaultdict(lambda: [0, 0])   # segundo -> [mensagens, risadas]
        self.hist = deque(maxlen=CFG["chat"]["historico"])
        self.recentes = deque(maxlen=60)
        self.chat_atual = 0
        self.chat_base = 0
        self.chat_limite = 0
        self.ultimo_disparo = 0.0
        # áudio
        self.db_atual = None
        self.db_base = None
        # twitch
        self.bid = None
        self.espectadores = None
        self.jogo_live = ""
        self.titulo = ""
        self.online_twitch = None

    @property
    def cfg(self):
        return cfg_canal(self.nome)

    # -- ciclo de vida
    def iniciar(self):
        ev = self.parar_ev = threading.Event()   # um sinal novo por início: threads antigas não revivem
        self.estado = "iniciando"
        for alvo in (self.gravador, self.detector_chat, self.detector_audio):
            threading.Thread(target=alvo, args=(ev,), daemon=True, name=f"{alvo.__name__}-{self.nome}").start()
        IRC.entrar(self.nome)

    def parar(self):
        self.parar_ev.set()
        IRC.sair(self.nome)
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        self.estado = "parado"

    def gravador(self, ev):
        while not ev.is_set():
            self.estado = "procurando a live"
            sl = subprocess.Popen(
                [STREAMLINK, f"https://twitch.tv/{self.nome}", self.cfg["qualidade"],
                 "--twitch-disable-ads", "--loglevel", "error", "-O"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            ff = subprocess.Popen(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                 "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                 "-f", "segment", "-segment_time", str(SEG), "-strftime", "1",
                 str(self.dir / "seg_%Y%m%d-%H%M%S.ts")],
                stdin=sl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            sl.stdout.close()
            self.procs = [sl, ff]
            self.estado = "conectado"
            ff.wait()
            sl.wait()
            self.procs = []
            if ev.is_set():
                break
            self.estado = "offline"
            ev.wait(20)
        if ev is self.parar_ev:
            self.estado = "parado"

    def segmentos(self):
        quando, lista = self._segs
        if time.time() - quando > 2:
            lista = listar_segmentos(self.dir)
            self._segs = (time.time(), lista)
        return lista

    def status_gravacao(self):
        segs = self.segmentos()
        if not segs:
            return {"ao_vivo": False, "ultimo": None, "inicio": None}
        try:
            idade = time.time() - segs[-1][1].stat().st_mtime
        except FileNotFoundError:
            idade = 999
        inicio = segs[-1][0]
        for i in range(len(segs) - 1, 0, -1):  # início da sessão atual (sem buracos > 60 s)
            if (segs[i][0] - segs[i - 1][0]).total_seconds() > 60:
                break
            inicio = segs[i - 1][0]
        return {"ao_vivo": idade < 25, "ultimo": round(idade), "inicio": iso(inicio)}

    def ha_gravacao_em(self, t):
        g = self.status_gravacao()
        return g["ao_vivo"] and de_iso(g["inicio"]) <= t

    # -- chat
    def registrar_msg(self, usuario, texto):
        ts = time.time()
        riso = bool(RISO.search(texto))
        self.msgs.append((ts, usuario, 2 if riso else 1, riso))
        c = self.cont[int(ts)]
        c[0] += 1
        c[1] += riso

    def detector_chat(self, ev):
        while not ev.wait(1):
            try:
                cc = CFG["chat"]
                now = time.time()
                while self.msgs and self.msgs[0][0] < now - cc["janela"]:
                    self.msgs.popleft()
                # peso por pessoa limitado: uma pessoa spamando não vira pico
                por_pessoa = defaultdict(int)
                risos = 0
                for _, u, peso, riso in self.msgs:
                    por_pessoa[u] += peso
                    risos += riso
                self.chat_atual = sum(min(v, cc["por_pessoa"]) for v in por_pessoa.values())
                self.hist.append(self.chat_atual)
                self.recentes.append(self.chat_atual)
                self.chat_base = statistics.median(self.hist) if len(self.hist) >= 60 else 0
                b = self.chat_base
                self.chat_limite = max(cc["minimo"], b * cc["fator"]) if b else cc["minimo"] * 2
                seg = int(now) - 1
                m, r = self.cont.pop(seg, (0, 0))
                for velho in [k for k in self.cont if k < seg]:
                    del self.cont[velho]
                self.serie.por(seg, m=m, r=r, j=self.chat_atual, l=round(self.chat_limite, 1))
                if self.chat_atual >= self.chat_limite and now - self.ultimo_disparo > cc["intervalo"]:
                    # início da subida: último segundo em que o chat ainda estava perto do normal
                    ref = max(b * 1.3, cc["minimo"] / 2)
                    subida = 0
                    for i, v in enumerate(reversed(self.recentes)):
                        if v <= ref:
                            subida = i
                            break
                    else:
                        subida = len(self.recentes)
                    t = agora() - timedelta(seconds=subida + cc["reacao"])
                    if self.ha_gravacao_em(t):
                        self.ultimo_disparo = now
                        razao = self.chat_atual / max(b, cc["minimo"] / 2)
                        sinais = {"chat": round(razao, 1)}
                        n = len(self.msgs)
                        if n:
                            sinais["riso"] = round(risos / n, 2)
                        marcar(self.nome, t, "chat", sinais=sinais)
            except Exception:
                log(f"[{self.nome}] chat:", traceback.format_exc(limit=2))

    # -- áudio
    def detector_audio(self, ev):
        """Mede o volume (LUFS momentâneo) de cada segmento novo e marca picos acima do normal."""
        valores = deque(maxlen=300)   # um valor por segundo, últimos 5 min
        feitos = set()
        ultimo = 0.0
        while not ev.wait(2):
            try:
                cfg = CFG["audio"]
                for s, p in self.segmentos()[-6:-1]:   # só segmentos já fechados
                    if p.name in feitos:
                        continue
                    feitos.add(p.name)
                    if (agora() - s).total_seconds() > 90:
                        continue
                    r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", p.name, "-vn", "-af", "ebur128",
                                        "-f", "null", "-"], cwd=self.dir, capture_output=True, text=True,
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
                        quando = s + timedelta(seconds=sec)
                        if v < -70:
                            self.serie.por(quando.timestamp(), db=-70)
                            continue
                        self.db_atual = round(v, 1)
                        if len(valores) >= 60:
                            base = statistics.median(valores)
                            self.db_base = round(base, 1)
                            excesso = v - base
                            if excesso >= cfg["limiar"] and v >= cfg["piso"] and time.time() - ultimo > cfg["intervalo"]:
                                ultimo = time.time()
                                marcar(self.nome, quando, "audio", sinais={"audio": round(excesso, 1)})
                        self.serie.por(quando.timestamp(), db=round(v, 1), dbb=self.db_base)
                        valores.append(v)
                if len(feitos) > 5000:
                    feitos = set(list(feitos)[-500:])
            except Exception:
                log(f"[{self.nome}] áudio:", traceback.format_exc(limit=2))

    def resumo(self):
        g = self.status_gravacao()
        agora_s = int(time.time())
        spark = self.serie.faixa(agora_s - 300, agora_s, 5)
        return {
            "nome": self.nome, "ativo": self.cfg["ativo"], "estado": self.estado, "gravacao": g,
            "chat": {"atual": self.chat_atual, "base": self.chat_base, "limite": round(self.chat_limite, 1)},
            "audio": {"atual": self.db_atual, "base": self.db_base},
            "espectadores": self.espectadores, "jogo": self.cfg["jogo"] or self.jogo_live, "titulo": self.titulo,
            "online_twitch": self.online_twitch, "spark": spark["j"], "spark_l": spark["l"],
            "modo": self.cfg["modo"], "qualidade": self.cfg["qualidade"],
        }


LUFS_RE = re.compile(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+)")
CANAIS = {}
CANAIS_LOCK = threading.Lock()


# ---------------------------------------------------------------- chat (uma conexão IRC para todos os canais)
class ConexaoIRC:
    PRIV = re.compile(r"^:([^!]+)![^ ]+ PRIVMSG #([^ ]+) :(.*)$")

    def __init__(self):
        self.sock = None
        self.lock = threading.Lock()
        self.canais = set()
        self.estado = "desconectado"

    def _enviar(self, txt):
        if self.sock:
            try:
                self.sock.sendall((txt + "\r\n").encode())
            except OSError:
                pass

    def entrar(self, nome):
        with self.lock:
            self.canais.add(nome)
            self._enviar(f"JOIN #{nome}")

    def sair(self, nome):
        with self.lock:
            self.canais.discard(nome)
            self._enviar(f"PART #{nome}")

    def loop(self):
        while True:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=360)
                with self.lock:
                    self.sock = s
                    self._enviar(f"NICK justinfan{random.randint(10000, 99999)}")
                    for nome in self.canais:
                        self._enviar(f"JOIN #{nome}")
                self.estado = "conectado"
                buf = b""
                while True:
                    dado = s.recv(16384)
                    if not dado:
                        break
                    buf += dado
                    while b"\r\n" in buf:
                        linha, buf = buf.split(b"\r\n", 1)
                        linha = linha.decode("utf-8", "ignore")
                        if linha.startswith("PING"):
                            self._enviar(linha.replace("PING", "PONG", 1))
                            continue
                        m = self.PRIV.match(linha)
                        if m:
                            canal = CANAIS.get(m.group(2).lower())
                            if canal:
                                canal.registrar_msg(m.group(1), m.group(3))
            except Exception:
                pass
            with self.lock:
                self.sock = None
            self.estado = "reconectando"
            time.sleep(5)


IRC = ConexaoIRC()


# ---------------------------------------------------------------- legenda (Whisper)
_WHISPER = None
WHISPER_LOCK = threading.Lock()
ESTADO = {"tunel": None, "encoder": None, "twitch": "desligado", "legenda": "desligada", "drive": "desligado"}


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


VERSOES = ("com", "sem", "ambas")


def versoes_legenda():
    """Quais versões verticais gerar: com legenda, sem legenda ou as duas."""
    if not CFG["legenda"]["ativa"]:
        return "sem"
    v = CFG["legenda"].get("versoes", "ambas")
    return v if v in VERSOES else "ambas"


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


def dividido(cc):
    return cc["modo"] == "dividido" and bool(cc.get("camera"))


def filtro_vertical(cc, fim="format=yuv420p"):
    cam = cc.get("camera")
    if dividido(cc):
        ht = int(1920 * cc["camera_altura"]) // 2 * 2
        hb = 1920 - ht
        r = 1080 / hb * cc["jogo_largura"]
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


def posicoes_texto(cc):
    """(y da legenda, y do gancho) no quadro 1080x1920."""
    if dividido(cc):
        ht = int(1920 * cc["camera_altura"])
        return ht + 200, ht       # legenda sobre o jogo, gancho na divisa câmera/jogo
    return 1430, 420              # legenda abaixo do vídeo, gancho no fundo desfocado de cima


def nome_arquivo(c):
    nota = re.sub(r"[^\w\- ]", "", c.get("nota") or "", flags=re.U).strip().replace(" ", "-")[:40]
    t = de_iso(c["t"]).strftime("%Y%m%d-%H%M%S")
    return f"{c['canal']}_{t}_{c['id'][-4:]}" + (f"_{nota}" if nota else "")


def cortar(c, encoder):
    canal = c["canal"]
    cc = cfg_canal(canal)
    pasta = REC / canal
    obj = CANAIS.get(canal)
    t = de_iso(c["t"])
    a = t - timedelta(seconds=c["antes"])
    b = t + timedelta(seconds=c["depois"])
    segs = listar_segmentos(pasta)
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
    if c.get("origem") == "trecho":
        pico = dur / 2
    entrada = "concat:" + "|".join(p.name for _, p in sel)
    nome = nome_arquivo(c)
    vert = CLIPES / f"{nome}_vertical.mp4"
    limpo = CLIPES / f"{nome}_vertical_sem_legenda.mp4"
    cru = CLIPES / f"{nome}_horizontal.mp4"
    txt = CLIPES / f"{nome}_post.txt"
    run = lambda args: subprocess.run(args, cwd=pasta, capture_output=True, text=True, creationflags=NO_WINDOW)
    janela = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", entrada, "-t", f"{dur:.2f}"]

    # 1. transcrição da janela inteira (pulada quando só a versão sem legenda é pedida)
    versoes = versoes_legenda()
    editar = CFG["edicao"]["ativa"]
    palavras = []
    if CFG["legenda"]["ativa"] and (versoes != "sem" or editar):
        wav = TMP / f"{c['id']}.wav"
        try:
            run(janela + ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
            audio = ler_wav(wav)
            with WHISPER_LOCK:
                palavras = edicao.transcrever(whisper(), audio)
        except Exception as e:
            log("transcrição falhou:", e)
        finally:
            wav.unlink(missing_ok=True)

    # 2. edição (Claude): trecho final, gancho, título
    sinais = ", ".join(f"{k}={v}" for k, v in (c.get("sinais") or {}).items()) or "marcação manual"
    jogo = cc.get("jogo") or (obj.jogo_live if obj else "") or "jogo não informado"
    contexto = f"Live de twitch.tv/{canal} jogando {jogo}. Sinais do momento: {sinais}."
    if c.get("nota"):
        contexto += f" Nota de quem marcou: {c['nota']}."
    if editar:
        ed = edicao.editar(palavras, dur, pico, contexto, CFG.get("anthropic_api_key", ""))
    else:   # edição desligada: janela inteira, sem IA, sem gancho
        ed = {"inicio": 0.0, "fim": dur, "gancho": "", "titulo": "", "descricao": "",
              "hashtags": [], "nota": None, "ia": False}
    ini, fim = ed["inicio"], ed["fim"]

    # 3. legenda karaokê + gancho + marca
    y_leg, y_gancho = posicoes_texto(cc)
    ass = pasta / f"_leg_{c['id']}.ass"
    marca = cc.get("marca", "") if editar else ""
    tem_texto = versoes != "sem" and edicao.gerar_ass(ass, palavras, ini, fim, y_leg, ed["gancho"], y_gancho, marca)

    # 4. zoom rápido no pico (vale para as duas versões)
    zoom = ""
    p = pico - ini
    if editar and CFG["edicao"]["zoom"] and 0.5 < p < (fim - ini) - 1:
        fps = fps_da_fonte(sel[0][1])
        z = f"1+0.12*min(1\\,max(0\\,(it-{p:.2f})/0.12))*min(1\\,max(0\\,({p + 0.9:.2f}-it)/0.25))"
        zoom = f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps={fps},"

    final = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{offset + ini:.2f}", "-i", entrada,
             "-t", f"{fim - ini:.2f}"]
    audio_f = ["-af", f"loudnorm=I={CFG['edicao']['volume_lufs']}:TP=-1.5:LRA=11"] if editar else []

    def render(com_texto, saida):
        fim_filtro = zoom + (f"ass={ass.name}:fontsdir='{FONTES_FF}'," if com_texto else "") + "format=yuv420p"
        r = run(final + ["-filter_complex", filtro_vertical(cc, fim_filtro), "-map", "[v]", "-map", "0:a:0?",
                         *audio_f, *encoder, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(saida)])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-400:] or "ffmpeg falhou")

    try:
        render(tem_texto, vert)          # principal: com legenda (ou sem, se não houver fala ou se pediram "sem")
        if tem_texto and versoes == "ambas":
            render(False, limpo)         # cópia limpa: sem legenda, gancho nem marca
    finally:
        ass.unlink(missing_ok=True)
    # versão horizontal da janela inteira, sem edição, para quem quiser editar na mão
    run(janela + ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(cru)])

    if editar:
        hashtags = " ".join("#" + h for h in ed["hashtags"])
        txt.write_text(f"{ed['titulo']}\n\n{ed['descricao']}\n\n{hashtags}\n".strip() + "\n", encoding="utf-8")
    else:
        txt = None
    info = {"legenda": bool(tem_texto), "limpo": limpo.name if limpo.exists() else None, "titulo": ed["titulo"], "descricao": ed["descricao"],
            "hashtags": ed["hashtags"], "gancho": ed["gancho"], "nota_ia": ed["nota"], "ia": ed["ia"],
            "erro_ia": ed.get("erro_ia"), "corte": [round(ini, 1), round(fim, 1)] if editar else None, "post": txt.name if txt else None}
    return vert.name, (cru.name if cru.exists() else None), info


ARQUIVOS = ("arquivo", "limpo", "cru", "post")


# ---------------------------------------------------------------- Google Drive (pasta sincronizada)
def pasta_drive():
    """Pasta do Google Drive para computador (ex.: G:\\Meu Drive\\Clipador). Vazia = desligado."""
    p = (CFG.get("pasta_drive") or "").strip()
    return Path(p) if p else None


def achar_arquivo(nome):
    """Arquivo de um clipe, esteja na pasta local ou já movido para o Drive."""
    if not nome or "/" in nome or "\\" in nome or ".." in nome:
        return None
    p = CLIPES / nome
    if p.exists():
        return p
    d = pasta_drive()
    if d and d.exists():
        for sub in d.iterdir():
            q = sub / nome
            if sub.is_dir() and q.exists():
                return q
    return None


def apagar_arquivos(c):
    for k in ARQUIVOS:
        p = achar_arquivo(c.get(k))
        if p:
            p.unlink(missing_ok=True)


def mover_no_drive(c, sub):
    """Move os arquivos do clipe para <pasta_drive>/<sub>/ (canal ou _descartados). True se moveu tudo."""
    d = pasta_drive()
    if not d:
        return False
    if not d.exists():
        if not d.parent.exists():        # o Drive para computador não está montado
            ESTADO["drive"] = f"pasta não encontrada: {d}"
            return False
        d.mkdir(parents=True, exist_ok=True)
    destino = d / sub
    destino.mkdir(parents=True, exist_ok=True)
    for k in ARQUIVOS:
        origem = achar_arquivo(c.get(k))
        if origem and origem.parent != destino:
            shutil.move(str(origem), str(destino / origem.name))
    return True


def enviar_drive():
    """Todo clipe cortado vai sozinho para o Drive, sem precisar aprovar. Os descartados vão para _descartados."""
    while True:
        time.sleep(10)
        d = pasta_drive()
        if not d:
            ESTADO["drive"] = "desligado"
            continue
        if not d.exists() and not d.parent.exists():   # Drive para computador fechado ou sem login
            ESTADO["drive"] = f"pasta não encontrada: {d}"
            continue
        try:
            with DB.lock:
                pendentes = [c for c in DB.clipes if c.get("arquivo") and not c.get("drive")
                             and c["status"] in ("pronto", "aprovado", "descartado")]
            for c in pendentes:
                sub = "_descartados" if c["status"] == "descartado" else c["canal"]
                if not mover_no_drive(c, sub):
                    break
                DB.mudar(c, drive=True)
                log(f"[{c['canal']}] no Drive: {c['arquivo']}")
            else:
                ESTADO["drive"] = "conectado"
        except Exception as e:
            ESTADO["drive"] = f"erro: {str(e)[:60]}"
            log("drive:", traceback.format_exc(limit=2))


def aprovar_arquivos(c):
    if pasta_drive():      # com o Drive ligado tudo já vai para lá; aprovar só marca o status
        return
    for k in ("arquivo", "limpo", "post"):
        if c.get(k) and (CLIPES / c[k]).exists():
            shutil.copy2(CLIPES / c[k], APROVADOS / c[k])


def desaprovar_arquivos(c):
    for k in ("arquivo", "limpo", "post"):
        if c.get(k):
            (APROVADOS / c[k]).unlink(missing_ok=True)


def preparar_cortes():
    encoder = detectar_encoder()
    if CFG["legenda"]["ativa"] and (versoes_legenda() != "sem" or CFG["edicao"]["ativa"]):
        try:
            whisper()
        except Exception as e:
            ESTADO["legenda"] = "erro ao carregar"
            log("Whisper não carregou:", e)
            CFG["legenda"]["ativa"] = False
    for i in range(max(1, int(CFG["cortes_simultaneos"]))):
        threading.Thread(target=cortador, args=(encoder,), daemon=True, name=f"cortador-{i}").start()


def cortador(encoder):
    while True:
        pendente = None
        with DB.lock:
            fila = [c for c in DB.clipes if c["status"] == "aguardando"
                    and agora() >= de_iso(c["t"]) + timedelta(seconds=c["depois"] + SEG + 4)]
            fila.sort(key=lambda c: (c["origem"] not in ("manual", "trecho"), c["t"]))   # manuais primeiro
            if fila:
                pendente = fila[0]
                DB.mudar(pendente, status="cortando", erro=None)
        if not pendente:
            time.sleep(1.5)
            continue
        apagar_arquivos(pendente)      # recorte: tira a versão anterior (no PC ou no Drive)
        inicio = time.time()
        try:
            vert, cru, info = cortar(pendente, encoder)
            upd = {"arquivo": vert, "cru": cru, "tempo_corte": round(time.time() - inicio), "drive": False, **info}
            nota_ia = info.get("nota_ia")
            fraco = nota_ia is not None and nota_ia < CFG["edicao"]["aprovar_nota_minima"]
            if pendente.get("auto") and not fraco:
                DB.mudar(pendente, status="aprovado", aprovado_por="automático", **upd)
                aprovar_arquivos(pendente)
            else:
                if fraco:
                    upd["obs"] = f"a IA deu nota {nota_ia}; ficou para revisão em vez de aprovar sozinho"
                DB.mudar(pendente, status="pronto", **upd)
            log(f"[{pendente['canal']}] cortado {vert} em {upd['tempo_corte']} s (ia={info['ia']} nota={nota_ia})")
        except Exception as e:
            DB.mudar(pendente, status="erro", erro=str(e))
            log("erro no corte:", e)


# ---------------------------------------------------------------- seleção automática
def seletor():
    """Fecha blocos de tempo e manda os melhores candidatos de cada canal para o corte."""
    while True:
        time.sleep(20)
        try:
            a = CFG["auto"]
            bloco = timedelta(minutes=a["bloco_min"])
            vagas = max(1, round(a["por_hora"] * a["bloco_min"] / 60))
            parada = {}
            for nome, cn in list(CANAIS.items()):
                g = cn.status_gravacao()
                parada[nome] = not g["ao_vivo"] and (g["ultimo"] or 0) > a["margem_s"]
            with DB.lock:
                blocos = {}
                for c in DB.clipes:
                    t = de_iso(c["t"])
                    chave = t.replace(minute=t.minute - t.minute % a["bloco_min"], second=0)
                    blocos.setdefault((c["canal"], chave), []).append(c)
                for (canal, chave), lista in blocos.items():
                    fechado = agora() >= chave + bloco + timedelta(seconds=a["margem_s"])
                    cand = [c for c in lista if c["status"] == "candidato"]
                    if not cand or not (fechado or parada.get(canal, True)):
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


# ---------------------------------------------------------------- manutenção
def gravar_series():
    while True:
        time.sleep(15)
        for cn in list(CANAIS.values()):
            try:
                cn.serie.gravar()
            except Exception:
                log(f"[{cn.nome}] série:", traceback.format_exc(limit=1))


def limpeza():
    """Apaga gravação bruta mais velha que retencao_h, preservando o trecho de clipes ainda em aberto."""
    while True:
        time.sleep(300)
        horas = CFG.get("retencao_h") or 0
        if horas <= 0:
            continue
        try:
            limite = agora() - timedelta(hours=horas)
            with DB.lock:
                protegidos = [(c["canal"], de_iso(c["t"]) - timedelta(seconds=c["antes"] + 180),
                               de_iso(c["t"]) + timedelta(seconds=c["depois"] + 180))
                              for c in DB.clipes if c["status"] in ("candidato", "aguardando", "cortando", "pronto", "erro")]
            apagados = 0
            for pasta in REC.iterdir():
                if not pasta.is_dir():
                    continue
                for s, p in listar_segmentos(pasta):
                    if s >= limite:
                        break
                    if any(cn == pasta.name and a <= s <= b for cn, a, b in protegidos):
                        continue
                    p.unlink(missing_ok=True)
                    apagados += 1
            if apagados:
                log(f"limpeza: {apagados} segmentos com mais de {horas} h apagados")
        except Exception:
            log("limpeza:", traceback.format_exc(limit=2))


# ---------------------------------------------------------------- Twitch Helix: espectadores, jogo e clipes
def twitch_api():
    token, vistos = None, set()

    def api(caminho):
        req = urllib.request.Request("https://api.twitch.tv/helix/" + caminho,
                                     headers={"Client-Id": CFG["twitch_client_id"], "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    while True:
        cid, segredo = CFG.get("twitch_client_id"), CFG.get("twitch_client_secret")
        if not cid or not segredo:
            ESTADO["twitch"] = "sem app configurado"
            time.sleep(30)
            continue
        try:
            if not token:
                corpo = urllib.parse.urlencode({"client_id": cid, "client_secret": segredo,
                                                "grant_type": "client_credentials"}).encode()
                with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", data=corpo, timeout=20) as r:
                    token = json.load(r)["access_token"]
            canais = [c for c in CANAIS.values() if c.cfg["ativo"]]
            sem_id = [c for c in canais if not c.bid]
            if sem_id:
                q = "&".join("login=" + urllib.parse.quote(c.nome) for c in sem_id[:100])
                ids = {u["login"].lower(): u["id"] for u in api("users?" + q)["data"]}
                for c in sem_id:
                    c.bid = ids.get(c.nome)
            if canais:
                q = "&".join("user_login=" + urllib.parse.quote(c.nome) for c in canais[:100])
                vivos = {s["user_login"].lower(): s for s in api("streams?" + q)["data"]}
                ts = time.time()
                for c in canais:
                    s = vivos.get(c.nome)
                    c.online_twitch = bool(s)
                    c.espectadores = s["viewer_count"] if s else None
                    if s:
                        c.jogo_live, c.titulo = s.get("game_name") or "", s.get("title") or ""
                        c.serie.por(ts, esp=s["viewer_count"])
            desde = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for c in canais:
                if not c.bid or not c.online_twitch:
                    continue
                for cl in api(f"clips?broadcaster_id={c.bid}&started_at={desde}&first=50")["data"]:
                    if cl["id"] in vistos:
                        continue
                    vistos.add(cl["id"])
                    criado = datetime.fromisoformat(cl["created_at"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                    t = criado - timedelta(seconds=12)
                    if c.ha_gravacao_em(t - timedelta(seconds=c.cfg["antes"] // 2)):
                        marcar(c.nome, t, "twitch", sinais={"twitch": 1})
            ESTADO["twitch"] = "conectado"
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


# ---------------------------------------------------------------- canais
def nome_valido(bruto):
    nome = (bruto or "").strip().lower()
    m = re.search(r"twitch\.tv/([^/?#\s]+)", nome)
    if m:
        nome = m.group(1)
    nome = nome.lstrip("@#")
    if not NOME_RE.match(nome):
        raise HTTPException(400, "nome de canal inválido")
    return nome


def ligar_canal(nome):
    with CANAIS_LOCK:
        cn = CANAIS.get(nome)
        if cn is None:
            cn = CANAIS[nome] = Canal(nome)
        if cn.estado == "parado":
            cn.iniciar()
        return cn


def obter_canal(nome):
    cn = CANAIS.get(nome)
    if not cn:
        raise HTTPException(404, "canal não encontrado")
    return cn


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


def com_ts(c):
    return {**c, "ts": int(de_iso(c["t"]).timestamp())}


@app.get("/api/estado")
def estado():
    livre = shutil.disk_usage(BASE).free / 1e9
    with DB.lock:
        clipes = [com_ts(c) for c in DB.clipes[-600:]]
    return {
        "canais": [cn.resumo() for cn in CANAIS.values()],
        "chat": IRC.estado, "encoder": ESTADO["encoder"], "livre_gb": round(livre, 1),
        "twitch": ESTADO["twitch"], "legenda": ESTADO["legenda"] if CFG["legenda"]["ativa"] else "desligada",
        "versoes": versoes_legenda(), "drive": ESTADO["drive"],
        "auto": CFG["auto"], "retencao_h": CFG["retencao_h"], "fator": CFG["chat"]["fator"],
        "link_amigo": f"{ESTADO['tunel']}/?k={CFG['senha']}" if ESTADO["tunel"] else None,
        "agora": iso(agora()), "agora_ts": int(time.time()), "clipes": clipes,
    }


@app.get("/api/serie")
def api_serie(canal: str, seg: int = 900, de: int = 0, ate: int = 0, pontos: int = 300):
    cn = obter_canal(canal)
    if not ate:
        ate = int(time.time())
    if not de:
        de = ate - max(30, min(MEMORIA_H * 3600, seg))
    pontos = max(20, min(1200, pontos))
    passo = max(1, -(-(ate - de) // pontos))
    out = cn.serie.faixa(de, ate, passo)
    with DB.lock:
        eventos = [{"id": c["id"], "ts": int(de_iso(c["t"]).timestamp()), "antes": c["antes"], "depois": c["depois"],
                    "status": c["status"], "origem": c["origem"], "nota": c.get("nota_auto")}
                   for c in DB.clipes if c.get("canal") == canal]
    eventos = [e for e in eventos if e["ts"] + e["depois"] >= de and e["ts"] - e["antes"] <= ate]
    return {**out, "ate": ate, "eventos": eventos}


@app.post("/api/config")
async def api_config(request: Request):
    d = await request.json()
    if d.get("versoes") in VERSOES:
        CFG["legenda"]["versoes"] = d["versoes"]
    salvar_cfg()
    return {"versoes": versoes_legenda()}


@app.post("/api/canais")
async def api_add_canal(request: Request):
    d = await request.json()
    nome = nome_valido(d.get("nome"))
    with CFG_LOCK:
        CFG["canais"].setdefault(nome, copy.deepcopy(CANAL_PADRAO))
        CFG["canais"][nome]["ativo"] = True
        salvar_cfg()
    ligar_canal(nome)
    log(f"canal adicionado: {nome}")
    return {"ok": True, "nome": nome}


@app.post("/api/canais/{nome}/{acao}")
async def api_canal(nome: str, acao: str, request: Request):
    cn = obter_canal(nome)
    if acao == "pausar":
        cn.parar()
        CFG["canais"][nome]["ativo"] = False
    elif acao == "retomar":
        CFG["canais"][nome]["ativo"] = True
        ligar_canal(nome)
    elif acao == "remover":
        cn.parar()
        with CANAIS_LOCK:
            CANAIS.pop(nome, None)
        CFG["canais"].pop(nome, None)
    elif acao == "config":
        d = await request.json()
        if d.get("qualidade") in QUALIDADES:
            CFG["canais"][nome]["qualidade"] = d["qualidade"]
        if "jogo" in d:
            CFG["canais"][nome]["jogo"] = str(d["jogo"])[:60]
    else:
        raise HTTPException(400, "ação inválida")
    salvar_cfg()
    return {"ok": True}


@app.post("/api/marcar")
async def api_marcar(request: Request):
    d = await request.json()
    canal = obter_canal(d.get("canal") or "").nome
    quem = (d.get("quem") or "").strip()[:20] or "anônimo"
    nota = (d.get("nota") or "").strip()[:80]
    if d.get("ini") and d.get("fim"):   # trecho escolhido no gráfico
        ini, fim = float(d["ini"]), float(d["fim"])
        if fim - ini < 3 or fim - ini > 300:
            raise HTTPException(400, "o trecho precisa ter entre 3 s e 5 min")
        return marcar(canal, datetime.fromtimestamp(int(ini)), "trecho", quem, nota, antes=0, depois=int(round(fim - ini)))
    voltar = max(0, min(120, int(d.get("voltar") or 0)))
    return marcar(canal, agora() - timedelta(seconds=voltar), "manual", quem, nota)


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
        aprovar_arquivos(c)
        DB.mudar(c, status="aprovado", aprovado_por=(d.get("quem") or "")[:20])
    elif acao == "descartar":
        desaprovar_arquivos(c)
        DB.mudar(c, status="descartado", drive=False)    # com o Drive ligado, vai para _descartados
    elif acao == "restaurar":
        desaprovar_arquivos(c)
        DB.mudar(c, status="pronto" if c.get("arquivo") else "candidato", auto=False, drive=False)
    elif acao == "cortar":
        DB.mudar(c, status="aguardando", auto=False)
    elif acao == "ajustar":
        antes = max(0, min(180, int(d.get("antes", c["antes"]))))
        depois = max(3, min(300, int(d.get("depois", c["depois"]))))
        nota = (d.get("nota", c.get("nota")) or "")[:80]
        desaprovar_arquivos(c)
        DB.mudar(c, antes=antes, depois=depois, nota=nota, status="aguardando", auto=False)
    elif acao == "nota":
        DB.mudar(c, nota=(d.get("nota") or "")[:80])
    else:
        raise HTTPException(400, "ação inválida")
    return com_ts(c)


@app.get("/arquivo/{nome}")
def arquivo(nome: str, baixar: int = 0):
    p = achar_arquivo(nome)
    if not p:
        raise HTTPException(404)
    tipo = "video/mp4" if p.suffix == ".mp4" else "text/plain; charset=utf-8"
    return FileResponse(p, media_type=tipo, filename=nome if baixar else None)


# ---------------------------------------------------------------- player da gravação (sem anúncios)
@app.get("/hls/{canal}/live.m3u8")
def hls_playlist(canal: str):
    """Playlist HLS ao vivo com os últimos segmentos fechados da gravação.

    O streamlink já descarta os anúncios da Twitch (--twitch-disable-ads), então este player
    não mostra a tela de intervalo comercial. Fica uns 10 a 15 s atrás da live."""
    cn = obter_canal(canal)
    segs = cn.segmentos()[:-1]                       # o último ainda está sendo escrito
    if not segs or (agora() - segs[-1][0]).total_seconds() > 90:
        raise HTTPException(404, "sem gravação recente")
    for _, p in segs[-40:]:                          # numera na ordem em que aparecem; nunca renumera
        if p.name not in cn.seq:
            cn.seq[p.name] = len(cn.seq)
    janela = segs[-6:]
    linhas = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEG * 3}",
              f"#EXT-X-MEDIA-SEQUENCE:{cn.seq[janela[0][1].name]}"]
    for i, (s, p) in enumerate(janela):
        if i and (s - janela[i - 1][0]).total_seconds() > SEG * 3:
            linhas.append("#EXT-X-DISCONTINUITY")    # buraco na gravação (queda ou intervalo)
        prox = janela[i + 1][0] if i + 1 < len(janela) else segs[-1][0] if len(segs) > len(janela) else None
        dur = (prox - s).total_seconds() if prox else SEG
        linhas += [f"#EXTINF:{max(1.0, min(SEG * 3, dur)):.3f},", f"{p.name}"]
    return HTMLResponse("\n".join(linhas) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})


@app.get("/hls/{canal}/{nome}")
def hls_segmento(canal: str, nome: str):
    cn = obter_canal(canal)
    if not SEG_RE.match(nome):
        raise HTTPException(404)
    p = cn.dir / nome
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp2t")


def quadro_atual(cn):
    segs = cn.segmentos()
    if len(segs) < 2:
        raise HTTPException(409, "ainda não há gravação para tirar um quadro")
    out = TMP / f"quadro_{cn.nome}.png"
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(segs[-2][1]),
                    "-frames:v", "1", str(out)], capture_output=True, creationflags=NO_WINDOW)
    if not out.exists():
        raise HTTPException(500, "não consegui extrair o quadro")
    return out


@app.get("/api/quadro.png")
def api_quadro(canal: str, novo: int = 0):
    cn = obter_canal(canal)
    out = TMP / f"quadro_{cn.nome}.png"
    if novo or not out.exists():
        out = quadro_atual(cn)
    return FileResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/previa.png")
def api_previa(canal: str):
    cn = obter_canal(canal)
    q = TMP / f"quadro_{cn.nome}.png"
    if not q.exists():
        q = quadro_atual(cn)
    out = TMP / f"previa_{cn.nome}.png"
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(q),
                        "-filter_complex", filtro_vertical(cn.cfg, "format=rgb24"), "-map", "[v]", "-frames:v", "1", str(out)],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise HTTPException(500, r.stderr[-300:])
    return FileResponse(out, headers={"Cache-Control": "no-store"})


@app.get("/api/layout")
def get_layout(canal: str):
    cc = obter_canal(canal).cfg
    return {"canal": canal, **{k: cc[k] for k in ("modo", "camera", "camera_altura", "jogo_largura", "antes", "depois")}}


@app.post("/api/layout")
async def set_layout(canal: str, request: Request):
    cc = obter_canal(canal).cfg
    d = await request.json()
    if d.get("modo") in ("desfocado", "dividido"):
        cc["modo"] = d["modo"]
    if isinstance(d.get("camera"), dict):
        cc["camera"] = {k: max(0.0, min(1.0, float(d["camera"][k]))) for k in ("x", "y", "w", "h")}
    for k, lo, hi in (("camera_altura", 0.2, 0.6), ("jogo_largura", 1.0, 2.0)):
        if k in d:
            cc[k] = max(lo, min(hi, float(d[k])))
    for k, lo, hi in (("antes", 5, 180), ("depois", 0, 120)):
        if k in d:
            cc[k] = max(lo, min(hi, int(d[k])))
    salvar_cfg()
    return get_layout(canal)


# ---------------------------------------------------------------- main
def main():
    migrar_versao_antiga()
    for nome, cc in CFG["canais"].items():
        if cc["ativo"]:
            ligar_canal(nome)
        else:
            CANAIS[nome] = Canal(nome)
    threading.Thread(target=IRC.loop, daemon=True).start()
    threading.Thread(target=preparar_cortes, daemon=True).start()
    for alvo in (seletor, gravar_series, limpeza, twitch_api, enviar_drive):
        threading.Thread(target=alvo, daemon=True).start()
    if "--sem-tunel" not in sys.argv:
        threading.Thread(target=tunel, daemon=True).start()
    nomes = ", ".join(CFG["canais"]) or "nenhum (adicione pelo painel)"
    print(f"\n  Clipador  |  canais: {nomes}")
    print(f"  Painel local:  http://localhost:{CFG['porta']}/?k={CFG['senha']}")
    print("  (o link para o amigo aparece aqui em alguns segundos)\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=CFG["porta"], log_level="warning")


if __name__ == "__main__":
    main()
