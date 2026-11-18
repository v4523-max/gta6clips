#!/usr/bin/env python3
"""
GTA 6 Twitch -> Dikey Shorts/Reels pipeline
1) Twitch API: GTA VI kategorisinde son X saatin en çok izlenen klipleri (önemli anlar)
2) yt-dlp ile indir
3) OpenCV ile facecam tespiti -> üstte yüz, altta oyun (1080x1920). Yüz yoksa blur arka plan.
4) faster-whisper ile kelime kelime altyazı + hook yazısı (opsiyonel Claude ile Türkçe hook)
5) ffmpeg ile render -> out/<tarih>/
Kullanım:
  python pipeline.py                 # tek tur
  python pipeline.py --watch 600     # her 10 dakikada bir yeni klipleri işle
  python pipeline.py --local x.mp4   # elindeki bir videoyu işle (test)
"""
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import requests

W, H = 1080, 1920
TOP_H = 700            # facecam paneli
BOT_H = H - TOP_H      # oyun paneli
STATE = Path("seen.json")


# ---------------- Twitch ----------------
def twitch_token():
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": os.environ["TWITCH_CLIENT_ID"],
        "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def helix(path, token, **params):
    r = requests.get(f"https://api.twitch.tv/helix/{path}", params=params, timeout=20, headers={
        "Client-Id": os.environ["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def get_game_id(token, name):
    data = helix("games", token, name=name)["data"]
    if not data:
        sys.exit(f"Twitch'te '{name}' kategorisi bulunamadı. --game ile tam adını ver.")
    return data[0]["id"]


def get_clips(token, game_id, hours, pages=3):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips, cursor = [], None
    for _ in range(pages):
        p = {"game_id": game_id, "started_at": since, "first": 100}
        if cursor:
            p["after"] = cursor
        res = helix("clips", token, **p)
        clips += res["data"]
        cursor = res.get("pagination", {}).get("cursor")
        if not cursor:
            break
    return sorted(clips, key=lambda c: c["view_count"], reverse=True)


def pick(clips, seen, top, min_views, per_streamer, langs):
    out, count = [], {}
    for c in clips:
        if c["id"] in seen or c["view_count"] < min_views or c["duration"] < 8:
            continue
        if langs and c.get("language") not in langs:
            continue
        b = c["broadcaster_name"]
        if count.get(b, 0) >= per_streamer:
            continue
        count[b] = count.get(b, 0) + 1
        out.append(c)
        if len(out) >= top:
            break
    return out


def download(url, dest):
    subprocess.run(["yt-dlp", "-q", "-f", "best[ext=mp4]/best", "-o", str(dest), url], check=True)
    return dest


# ---------------- Yüz / layout ----------------
def cascade_path():
    name = "haarcascade_frontalface_default.xml"
    p = Path(getattr(getattr(cv2, "data", None), "haarcascades", "") or ".") / name
    if p.exists():
        return str(p)
    local = Path(name)
    if not local.exists():  # OpenCV 5+ bu dosyayı içermiyor -> bir kez indir
        r = requests.get(f"https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/{name}", timeout=30)
        r.raise_for_status()
        local.write_bytes(r.content)
    return str(local)


def detect_facecam(path, samples=30):
    """Videoda SABİT duran yüzü bulur (facecam). Oyundaki NPC yüzleri tutarsız olduğu için elenir."""
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    vw, vh = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    casc = cv2.CascadeClassifier(cascade_path())
    boxes = []
    for i in np.linspace(0, n - 1, samples).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, fr = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        f = casc.detectMultiScale(g, 1.1, 6, minSize=(int(vh * 0.06), int(vh * 0.06)))
        if len(f):
            boxes.append(max(f, key=lambda b: b[2] * b[3]))
    cap.release()
    if len(boxes) < samples * 0.4:
        return None, (vw, vh)
    b = np.array(boxes, dtype=float)
    cx, cy = np.median(b[:, 0] + b[:, 2] / 2), np.median(b[:, 1] + b[:, 3] / 2)
    near = b[(abs(b[:, 0] + b[:, 2] / 2 - cx) < vw * 0.05) & (abs(b[:, 1] + b[:, 3] / 2 - cy) < vh * 0.05)]
    if len(near) < samples * 0.4:
        return None, (vw, vh)
    return (cx, cy, float(np.median(near[:, 2]))), (vw, vh)


def even(x):
    return int(x) // 2 * 2


def layout_filter(face, vw, vh, ass_name):
    if face:
        cx, cy, fw = face
        cw = min(fw * 3.2, vw)
        ch = cw * TOP_H / W
        if ch > vh:
            ch, cw = vh, vh * W / TOP_H
        x = min(max(cx - cw / 2, 0), vw - cw)
        y = min(max(cy - ch / 2, 0), vh - ch)
        gh = vh
        gw = gh * W / BOT_H
        gx = (vw - gw) / 2
        return (f"[0:v]crop={even(cw)}:{even(ch)}:{even(x)}:{even(y)},scale={W}:{TOP_H}[t];"
                f"[0:v]crop={even(gw)}:{even(gh)}:{even(gx)}:0,scale={W}:{BOT_H}[b];"
                f"[t][b]vstack=inputs=2,setsar=1,ass={ass_name}[v]")
    return (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:5[bg];"
            f"[0:v]scale={W}:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1,ass={ass_name}[v]")


# ---------------- Altyazı / hook ----------------
_model = None


def transcribe(path, model_name):
    global _model
    try:
        from faster_whisper import WhisperModel
        if _model is None:
            _model = WhisperModel(model_name, device="auto", compute_type="int8")
        segs, info = _model.transcribe(str(path), word_timestamps=True, vad_filter=True,
                                       language=os.environ.get("WHISPER_LANG") or None)
        words = [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]
        return words, info.language
    except Exception as e:
        print(f"  ! altyazı atlandı: {e}")
        return [], None


def make_hook(title, words):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return title
    text = " ".join(w[2] for w in words)[:1500]
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=30, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.environ.get("HOOK_MODEL", "claude-haiku-5-5"), "max_tokens": 60, "messages": [{
                "role": "user", "content":
                f"GTA 6 yayın klibi. Başlık: {title}\nKonuşma: {text}\n"
                "Shorts için en fazla 6 kelimelik, merak uyandıran TÜRKÇE bir hook yaz. Emoji yok, tırnak yok. Sadece hook."}]})
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip().strip('"')
    except Exception as e:
        print(f"  ! hook için klip başlığı kullanıldı: {e}")
        return title


def up(s, tr=True):
    return (s.replace("i", "İ").replace("ı", "I") if tr else s).upper()


def clean(s):
    return re.sub(r"[{}\\]", "", s)


def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(path, words, hook, credit, has_face, dur, lang=None):
    cap_y = TOP_H if has_face else 1450
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,Arial,92,&H0000F0FF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,7,2,5,40,40,0,1",
        "Style: Hook,Arial,70,&H00FFFFFF,&H000000FF,&H00000000,&HC0000000,1,0,0,0,100,100,0,0,3,18,0,5,60,60,0,1",
        "Style: Cr,Arial,34,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,0,9,30,30,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    # Hook: ilk 4 sn, alt kısımda
    lines.append(f"Dialogue: 2,{ts(0)},{ts(min(4, dur))},Hook,,0,0,0,,{{\\pos({W//2},{H - 330})\\fad(150,200)}}{clean(up(hook))}")
    # Kaynak/kredi
    lines.append(f"Dialogue: 1,{ts(0)},{ts(dur)},Cr,,0,0,0,,{{\\pos({W - 30},{H - 90})}}{clean(credit)}")
    # Kelime grupları (max 3 kelime / 0.6 sn boşlukta böl)
    chunk = []
    def flush():
        if chunk:
            txt = " ".join(c[2] for c in chunk)
            lines.append(f"Dialogue: 0,{ts(chunk[0][0])},{ts(chunk[-1][1] + 0.05)},Cap,,0,0,0,,"
                         f"{{\\pos({W//2},{cap_y})\\fscx80\\fscy80\\t(0,80,\\fscx100\\fscy100)}}{clean(up(txt, lang == 'tr'))}")
            chunk.clear()
    for w in words:
        if chunk and (len(chunk) >= 3 or w[0] - chunk[-1][1] > 0.6):
            flush()
        chunk.append(w)
    flush()
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------------- Render ----------------
def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def process(src, out_mp4, title, broadcaster, whisper_model, subs=True):
    src, out_mp4 = Path(src).resolve(), Path(out_mp4).resolve()
    try:
        face, (vw, vh) = detect_facecam(src)
    except Exception as e:  # yüz tespiti çökerse blur layout ile devam et
        print(f"  ! yüz tespiti çalışmadı: {e}")
        cap = cv2.VideoCapture(str(src))
        face, vw, vh = None, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
    print(f"  facecam: {'var' if face else 'yok -> blur layout'}")
    words, lang = transcribe(src, whisper_model) if subs else ([], None)
    hook = make_hook(title, words)
    dur = duration(src)
    work = out_mp4.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    write_ass(work / "subs.ass", words, hook, f"twitch.tv/{broadcaster}" if broadcaster else "", bool(face), dur, lang)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-filter_complex", layout_filter(face, vw, vh, "subs.ass"),
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_mp4)]
    subprocess.run(cmd, check=True, cwd=work)
    return hook


def telegram(video, caption):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        return
    api = f"https://api.telegram.org/bot{tok}"
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:  # bota bir kez mesaj attıysan chat id'yi kendisi bulur
        upd = requests.get(f"{api}/getUpdates", timeout=20).json().get("result", [])
        msgs = [u["message"]["chat"]["id"] for u in upd if "message" in u]
        if not msgs:
            print("  ! Telegram: önce bota /start yaz")
            return
        chat = msgs[-1]
    try:
        with open(video, "rb") as f:
            r = requests.post(f"{api}/sendVideo", timeout=300, data={
                "chat_id": chat, "caption": caption[:1000], "supports_streaming": "true"}, files={"video": f})
        r.raise_for_status()
        print("  -> Telegram'a gönderildi")
    except Exception as e:
        print(f"  ! Telegram hatası: {e}")


def run_once(a, seen):
    token = twitch_token()
    gid = get_game_id(token, a.game)
    clips = pick(get_clips(token, gid, a.hours, a.pages), seen, a.top, a.min_views, a.per_streamer,
                 set(a.lang.split(",")) if a.lang and a.lang != "all" else None)
    print(f"{datetime.now():%H:%M} -> {len(clips)} yeni klip")
    day = Path("out") / datetime.now().strftime("%Y%m%d")
    (day / "raw").mkdir(parents=True, exist_ok=True)
    for c in clips:
        name = f"{c['view_count']:06d}_{re.sub(r'\W+', '', c['broadcaster_name'])}_{c['id'][:12]}"
        print(f"* {c['broadcaster_name']} | {c['view_count']} izlenme | {c['title']}")
        try:
            raw = download(c["url"], day / "raw" / f"{name}.mp4")
            hook = process(raw, day / f"{name}.mp4", c["title"], c["broadcaster_name"], a.whisper)
            post = f"{hook}\n\n#GTA6 #GTA6Türkiye #GTAVI #{c['broadcaster_name']}\nKaynak: {c['url']}"
            (day / f"{name}.txt").write_text(post + "\n", encoding="utf-8")
            telegram(day / f"{name}.mp4", f"{post}\n({c['view_count']} izlenme)")
            seen.add(c["id"])
            STATE.write_text(json.dumps(sorted(seen)))
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ! hata: {e}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--game", default="Grand Theft Auto VI")
    p.add_argument("--hours", type=float, default=6, help="son kaç saatin klipleri")
    p.add_argument("--top", type=int, default=10, help="tur başına kaç video")
    p.add_argument("--min-views", type=int, default=20)
    p.add_argument("--pages", type=int, default=10, help="Twitch'ten kaç sayfa (x100) klip taransın")
    p.add_argument("--per-streamer", type=int, default=3)
    p.add_argument("--lang", default="tr", help="örn: tr  veya  en,tr (all = hepsi)")
    p.add_argument("--whisper", default="small")
    p.add_argument("--watch", type=int, default=0, help="saniye; 0 = tek tur")
    p.add_argument("--local", help="yerel video işle (test)")
    p.add_argument("--no-subs", action="store_true")
    a = p.parse_args()

    if a.local:
        out = Path("out") / "local" / (Path(a.local).stem + "_vertical.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        process(a.local, out, "GTA 6", "", a.whisper, subs=not a.no_subs)
        print(f"OK -> {out}")
        return

    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    while True:
        run_once(a, seen)
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
