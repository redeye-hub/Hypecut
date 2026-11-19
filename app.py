import json, os, re, shutil, subprocess, threading, uuid, wave, webbrowser
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).parent
DATA, CLIPS = BASE / "data", BASE / "clips"
DATA.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)

app = Flask(__name__)
JOBS, MODELS = {}, {}
RENDER_LOCK = threading.Lock()
W, H = 1080, 1920

# ---------- caption styles ----------
STYLES = {
    "hormozi": dict(label="Hormozi", font="Arial Black", size=84, color="FFFFFF", hi="FFE600", outline="000000", ow=8, shadow=0, box=False, upper=True, words=3, pop=True),
    "neon": dict(label="Neon", font="Impact", size=96, color="FFFFFF", hi="00FFC8", outline="8A00FF", ow=7, shadow=0, box=False, upper=True, words=3, pop=True),
    "comic": dict(label="Comic", font="Comic Sans MS", size=80, color="FFFFFF", hi="FF3B3B", outline="000000", ow=7, shadow=2, box=False, upper=False, words=3, pop=True),
    "classic": dict(label="Classic", font="Arial", size=68, color="FFFFFF", hi="FFFFFF", outline="000000", ow=5, shadow=1, box=False, upper=False, words=6, pop=False),
    "boxed": dict(label="Boxed", font="Segoe UI", size=66, color="FFFFFF", hi="FFE600", outline="000000", ow=14, shadow=0, box=True, upper=False, words=4, pop=False),
    "minimal": dict(label="Minimal", font="Segoe UI", size=60, color="FFFFFF", hi="FFFFFF", outline="000000", ow=3, shadow=0, box=False, upper=False, words=5, pop=False),
}
POS = {"top": (8, 200), "center": (5, 0), "lower": (2, 430), "bottom": (2, 290)}  # (ASS alignment, marginV)


def ac(rgb, a="00"):
    return f"&H{a}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def ts(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def clean(s):
    return re.sub(r"[{}\\]", "", s).replace("\n", r"\N")


def make_ass(path, words, dur, cap, title, mark):
    st = STYLES.get(cap.get("style"), STYLES["hormozi"])
    scale = float(cap.get("size", 100)) / 100
    align, mv = POS.get(cap.get("position", "lower"), POS["lower"])
    back = ac("000000", "60") if st["box"] else ac("000000", "80")
    outc = back if st["box"] else ac(st["outline"])
    hdr = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{st['font']},{int(st['size'] * scale)},{ac(st['color'])},{ac(st['color'])},{outc},{back},1,0,0,0,100,100,0,0,{3 if st['box'] else 1},{st['ow']},{st['shadow']},{align},70,70,{mv},1",
        f"Style: Title,Arial Black,60,{ac('FFFFFF')},{ac('FFFFFF')},{ac('000000')},{ac('000000', '80')},1,0,0,0,100,100,0,0,1,7,0,8,70,70,150,1",
        f"Style: Mark,Segoe UI,40,{ac('FFFFFF', '80')},{ac('FFFFFF', '80')},{ac('000000', 'A0')},{ac('000000', 'A0')},1,0,0,0,100,100,0,0,1,2,0,3,50,50,120,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev, end = [], ts(dur)
    if title:
        ev.append(f"Dialogue: 1,{ts(0)},{end},Title,,0,0,0,,{clean(title)}")
    if mark:
        ev.append(f"Dialogue: 1,{ts(0)},{end},Mark,,0,0,0,,{clean(mark)}")
    n = int(cap.get("words") or st["words"])
    upper = cap.get("caps", st["upper"])
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= n or w[0] - cur[-1][1] > 0.9):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    for ci, ch in enumerate(chunks):
        texts = [clean(x[2].upper() if upper else x[2]) for x in ch]
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else None
        for i, w in enumerate(ch):
            s = w[0]
            if i + 1 < len(ch):
                e = ch[i + 1][0]
            else:
                e = min(w[1] + 0.25, nxt) if nxt else w[1] + 0.25
            e = max(e, s + 0.1)
            parts = []
            for j, t in enumerate(texts):
                if j == i and (st["hi"] != st["color"] or st["pop"]):
                    tag = "{\\c" + ac(st["hi"]) + ("\\fscx115\\fscy115" if st["pop"] else "") + "}"
                    parts.append(tag + t + "{\\r}")
                else:
                    parts.append(t)
            ev.append(f"Dialogue: 0,{ts(s)},{ts(e)},Cap,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text("\n".join(hdr + ev), encoding="utf-8")


# ---------- helpers ----------
def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-700:])
    return p.stdout


def probe(path):
    j = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-show_entries", "format=duration", "-of", "json", str(path)]))
    return int(j["streams"][0]["width"]), int(j["streams"][0]["height"]), float(j["format"]["duration"])


def slug(s):
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-")[:40] or "clip"


def transcribe(wav, size, lang):
    from faster_whisper import WhisperModel
    if size not in MODELS:
        dev = os.environ.get("WHISPER_DEVICE", "cpu")
        MODELS[size] = WhisperModel(size, device=dev, compute_type="float16" if dev == "cuda" else "int8")
    segs, _ = MODELS[size].transcribe(str(wav), word_timestamps=True, language=lang or None, vad_filter=True)
    return [(w.start, w.end, w.word.strip()) for sg in segs for w in (sg.words or []) if w.word.strip()]


def suggest(rms, L, count):
    L, n = max(10, int(L)), len(rms)
    if n <= L:
        return [dict(start=0, end=n, score=100)]
    avg = np.convolve(rms, np.ones(L) / L, "valid")
    pk = np.array([rms[i:i + L].max() for i in range(len(avg))])
    sc = avg / max(avg.max(), 1e-9) * 0.7 + pk / max(pk.max(), 1e-9) * 0.3
    out = []
    for i in np.argsort(-sc):
        if all(abs(int(i) - o["start"]) >= L for o in out):
            out.append(dict(start=int(i), end=int(i) + L, score=round(float(sc[i]) * 100)))
        if len(out) >= count:
            break
    return out


# ---------- layout / filter graph ----------
def panel(src, b, w, h, mode, xoff, p):
    c = f"{src}crop=iw*{b['w']:.5f}:ih*{b['h']:.5f}:iw*{b['x']:.5f}:ih*{b['y']:.5f}"
    if mode == "fill":
        return [f"{c},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}:(iw-{w})*{xoff:.3f}:(ih-{h})/2,setsar=1[{p}]"]
    bw, bh = w // 8 * 2, h // 8 * 2
    return [
        f"{c},split=2[{p}a][{p}b]",
        f"[{p}a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=6:2,scale={w}:{h},setsar=1[{p}bg]",
        f"[{p}b]scale={w}:{h}:force_original_aspect_ratio=decrease[{p}fg]",
        f"[{p}bg][{p}fg]overlay=(W-w)/2:(H-h)/2[{p}]",
    ]


def build_graph(s, subs=False, small=False):
    face, cont = s["face"], s["content"]
    layout = s.get("layout", "split_face_top")
    mode, xoff = s.get("content_mode", "fill"), float(s.get("xoff", 0.5))
    hf = int(H * float(s.get("split", 0.4))) // 2 * 2
    hc = H - hf
    if layout == "face_only":
        g, base = panel("[0:v]", face, W, H, "fill", 0.5, "f"), "[f]"
    elif layout == "content_only":
        g, base = panel("[0:v]", cont, W, H, mode, xoff, "c"), "[c]"
    else:
        g = ["[0:v]split=2[s1][s2]"]
        g += panel("[s1]", face, W, hf, "fill", 0.5, "f")
        g += panel("[s2]", cont, W, hc, mode, xoff, "c")
        g.append("[f][c]vstack=inputs=2[b]" if layout == "split_face_top" else "[c][f]vstack=inputs=2[b]")
        base = "[b]"
    tail = "subtitles=subs.ass,format=yuv420p" if subs else ("scale=540:960,format=yuv420p" if small else "format=yuv420p")
    g.append(f"{base}{tail}[v]")
    return ";".join(g)


# ---------- jobs ----------
def load_job(jid, url, q):
    j = JOBS[jid]
    try:
        import yt_dlp
        d = DATA / jid
        d.mkdir(exist_ok=True)

        def hook(h):
            if h["status"] == "downloading":
                tot = h.get("total_bytes") or h.get("total_bytes_estimate") or 0
                if tot:
                    j["progress"] = int(h["downloaded_bytes"] / tot * 70)
                j["message"] = "Downloading video"
            elif h["status"] == "finished":
                j["message"] = "Merging streams"

        opts = {
            "format": f"bv*[height<={q}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={q}]+ba/b[height<={q}]/b",
            "merge_output_format": "mp4", "outtmpl": str(d / "source.%(ext)s"),
            "progress_hooks": [hook], "quiet": True, "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
        src = d / "source.mp4"
        if not src.exists():
            src = next(d.glob("source.*"))
        w, h, dur = probe(src)
        j.update(message="Scanning for hype moments", progress=80)
        wav = d / "audio.wav"
        run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", str(wav)])
        rms = []
        with wave.open(str(wav)) as wf:
            sr = wf.getframerate()
            while True:
                raw = wf.readframes(sr)
                if not raw:
                    break
                a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
                rms.append(float(np.sqrt((a ** 2).mean())))
        wav.unlink(missing_ok=True)
        rms = np.array(rms)
        np.save(d / "rms.npy", rms)
        j.update(status="done", progress=100, message="Ready", src=str(src), title=info.get("title", "video"),
                 duration=dur, width=w, height=h, suggestions=suggest(rms, 45, 8))
    except Exception as e:
        j.update(status="error", message=str(e)[:400])


def render_job(rid, s):
    j = JOBS[rid]
    try:
        with RENDER_LOCK:
            vj = JOBS[s["vid"]]
            src = str(Path(vj["src"]).resolve())
            start, dur = float(s["start"]), float(s["end"]) - float(s["start"])
            d = DATA / rid
            d.mkdir(exist_ok=True)
            cap = s.get("captions") or {}
            words = []
            j.update(status="running", message="Preparing", progress=2)
            if cap.get("enabled"):
                j.update(message="Transcribing (first run downloads the model)", progress=5)
                wav = d / "clip.wav"
                run(["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", src, "-vn", "-ac", "1", "-ar", "16000", str(wav)])
                words = transcribe(wav, cap.get("model", "base"), cap.get("lang"))
            title, mark = (s.get("title") or "").strip(), (s.get("watermark") or "").strip()
            use_ass = bool(words or title or mark)
            if use_ass:
                make_ass(d / "subs.ass", words, dur, cap, title, mark)
            name = f"{slug(vj['title'])}_{int(start)}s_{rid[:4]}.mp4"
            out = str((CLIPS / name).resolve())
            cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", src,
                   "-filter_complex", build_graph(s, subs=use_ass), "-map", "[v]", "-map", "0:a?",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(s.get("crf", 20))), "-r", "30",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
            j.update(message="Rendering", progress=10)
            with open(d / "ff.log", "w") as log:
                p = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=log, text=True)
                for line in p.stdout:
                    if line.startswith("out_time_ms="):
                        try:
                            j["progress"] = min(99, 10 + int(int(line.split("=")[1]) / 1e6 / dur * 89))
                        except ValueError:
                            pass
                p.wait()
            if p.returncode:
                raise RuntimeError((d / "ff.log").read_text()[-500:])
            j.update(status="done", progress=100, message="Done", file=name)
            shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        j.update(status="error", message=str(e)[:400])


# ---------- routes ----------
@app.get("/")
def index():
    return send_file(BASE / "index.html")


@app.get("/api/styles")
def styles():
    return jsonify(STYLES)


@app.post("/api/load")
def load():
    b, jid = request.json, uuid.uuid4().hex[:10]
    JOBS[jid] = dict(kind="video", status="running", progress=0, message="Starting")
    threading.Thread(target=load_job, args=(jid, b["url"].strip(), int(b.get("quality", 1080))), daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/job/<jid>")
def job(jid):
    return jsonify({k: v for k, v in JOBS.get(jid, {"status": "error", "message": "Unknown job"}).items() if k != "src"})


@app.get("/api/suggest/<vid>")
def sug(vid):
    rms = np.load(DATA / vid / "rms.npy")
    return jsonify(suggest(rms, float(request.args.get("length", 45)), int(request.args.get("count", 8))))


@app.get("/api/video/<vid>")
def video(vid):
    return send_file(JOBS[vid]["src"], conditional=True)


def grab(src, t, scale=None):
    cmd = ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(src), "-frames:v", "1"]
    if scale:
        cmd += ["-vf", scale]
    p = subprocess.run(cmd + ["-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"], capture_output=True)
    return p.stdout


@app.get("/api/frame/<vid>")
def frame(vid):
    return Response(grab(JOBS[vid]["src"], request.args.get("t", 0), "scale=960:-2"), mimetype="image/jpeg")


@app.post("/api/preview")
def preview():
    s = request.json
    cmd = ["ffmpeg", "-v", "error", "-ss", str(s["t"]), "-i", JOBS[s["vid"]]["src"], "-filter_complex", build_graph(s, small=True),
           "-map", "[v]", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode:
        return p.stderr[-300:], 500
    return Response(p.stdout, mimetype="image/jpeg")


@app.post("/api/detect")
def detect():
    import cv2
    s = request.json
    img = cv2.imdecode(np.frombuffer(grab(JOBS[s["vid"]]["src"], s["t"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return jsonify(found=False)
    h, w = img.shape
    cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    f = cas.detectMultiScale(img, 1.1, 6, minSize=(int(w * 0.03), int(w * 0.03)))
    if not len(f):
        return jsonify(found=False)
    x, y, fw, fh = max(f, key=lambda r: r[2] * r[3])
    asp = float(s.get("aspect", 0.9))
    bw = fw * 2.6
    bh = bw / asp
    if bh > h:
        bh, bw = h, h * asp
    if bw > w:
        bw, bh = w, w / asp
    bx = min(max(x + fw / 2 - bw / 2, 0), w - bw)
    by = min(max(y + fh * 0.6 - bh / 2, 0), h - bh)
    return jsonify(found=True, box=dict(x=bx / w, y=by / h, w=bw / w, h=bh / h))


@app.post("/api/render")
def render():
    s, rid = request.json, uuid.uuid4().hex[:10]
    JOBS[rid] = dict(kind="render", status="queued", progress=0, message="Queued", label=f"{int(s['start'])}s to {int(s['end'])}s")
    threading.Thread(target=render_job, args=(rid, s), daemon=True).start()
    return jsonify(id=rid)


@app.get("/api/clips")
def clips():
    fs = sorted(CLIPS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([dict(name=f.name, size=f.stat().st_size) for f in fs])


@app.get("/clips/<name>")
def clip(name):
    return send_from_directory(CLIPS, name, as_attachment=request.args.get("dl") == "1")


@app.delete("/api/clips/<name>")
def delclip(name):
    (CLIPS / Path(name).name).unlink(missing_ok=True)
    return jsonify(ok=True)


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found. Install it with:  winget install Gyan.FFmpeg   then reopen the terminal.")
        raise SystemExit(1)
    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run("127.0.0.1", 5000, threaded=True)
