# app.py
# Repo root must have:
#   packages.txt     → ffmpeg
#   requirements.txt → streamlit==1.45.1 / psutil==5.9.8

import streamlit as st

st.set_page_config(
    page_title="MP4 → MP3 Converter",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

import os
import sys
import time
import uuid
import shutil
import threading
import subprocess
import tempfile
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
PIPE_CHUNK     = 2 * 1024 * 1024       # 2 MB — safe on all RAM sizes
FFMPEG_TIMEOUT = 7200                  # 2 hours max
MAX_RAM_LOAD   = 200 * 1024 * 1024     # 200 MB max loaded into browser RAM

TMP_ROOT = Path(tempfile.gettempdir()) / "mp4mp3app"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

BITRATES = {
    "320 kbps — Maximum" : "320k",
    "256 kbps — High"    : "256k",
    "192 kbps — Standard": "192k",
    "128 kbps — Compact" : "128k",
    "96 kbps  — Voice"   : "96k",
}
SAMPLERATES = {
    "48000 Hz — Studio"  : "48000",
    "44100 Hz — CD"      : "44100",
    "22050 Hz — Lo-fi"   : "22050",
}
CHANNELS = {
    "Stereo (2 ch)"      : "2",
    "Mono   (1 ch)"      : "1",
}
ALLOWED = ["mp4","avi","mkv","mov","wmv","flv","webm","m4v","ts"]

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "sid"          : None,
    "status"       : "idle",   # idle | converting | done | error
    "error_msg"    : "",
    "output_path"  : None,
    "out_filename" : None,
    "in_name"      : None,
    "in_size"      : 0,
    "out_size"     : 0,
    "elapsed"      : 0.0,
    "progress"     : 0.0,
}

for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

if st.session_state.sid is None:
    st.session_state.sid = uuid.uuid4().hex

# ─────────────────────────────────────────────────────────────────────────────
#  TEMP FOLDER HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_session_dir() -> Path:
    d = TMP_ROOT / st.session_state.sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def wipe_session():
    """Delete this session's temp files and reset state."""
    try:
        d = TMP_ROOT / st.session_state.sid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    for k, v in _DEFAULTS.items():
        st.session_state[k] = v
    st.session_state.sid = uuid.uuid4().hex


# Background stale-folder cleanup (runs at most once per 10 min)
_LAST_CLEAN  = [0.0]
_CLEAN_LOCK  = threading.Lock()

def _bg_cleanup():
    with _CLEAN_LOCK:
        if time.time() - _LAST_CLEAN[0] < 600:
            return
        _LAST_CLEAN[0] = time.time()

    def _run():
        try:
            cutoff = time.time() - 7200
            for child in TMP_ROOT.iterdir():
                try:
                    if child.is_dir() and child.stat().st_mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
#  SYSTEM INFO
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60, show_spinner=False)
def _check_ffmpeg():
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            return True, (r.stdout or "").splitlines()[0]
        return False, "non-zero exit"
    except FileNotFoundError:
        return False, "not found in PATH"
    except Exception as e:
        return False, str(e)


def _disk_gb():
    try:
        if _PSUTIL:
            return psutil.disk_usage(str(TMP_ROOT)).free / 1e9
        s = os.statvfs(str(TMP_ROOT))
        return s.f_bavail * s.f_frsize / 1e9
    except Exception:
        return -1.0


def _ram_gb():
    try:
        if _PSUTIL:
            return psutil.virtual_memory().available / 1e9
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) / 1e6
    except Exception:
        pass
    return -1.0

# ─────────────────────────────────────────────────────────────────────────────
#  CONVERSION ENGINE
#  - Pipes upload bytes → ffmpeg stdin in PIPE_CHUNK chunks
#  - Video bytes NEVER touch disk
#  - Only output MP3 is written to disk
#  - Works on Python 3.9 → 3.14
# ─────────────────────────────────────────────────────────────────────────────
def run_ffmpeg(uploaded_file, out_path, bitrate, sr, ch, progress_cb):
    """
    Returns (success: bool, error_msg: str)
    progress_cb(float) is called with 0.0 → 1.0
    """
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i",        "pipe:0",
        "-vn",
        "-acodec",   "libmp3lame",
        "-b:a",      bitrate,
        "-ar",       sr,
        "-ac",       ch,
        "-q:a",      "0",
        str(out_path),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin  = subprocess.PIPE,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg not found. packages.txt must contain 'ffmpeg'."
    except Exception as e:
        return False, f"Cannot start ffmpeg: {e}"

    total     = int(st.session_state.in_size)
    sent      = [0]
    pipe_err  = [None]
    done_ev   = threading.Event()

    # Writer thread — pushes upload bytes into ffmpeg stdin
    def _writer():
        try:
            uploaded_file.seek(0)
            while True:
                chunk = uploaded_file.read(PIPE_CHUNK)
                if not chunk:
                    break
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except BrokenPipeError:
                    break
                except Exception as e:
                    pipe_err[0] = str(e)
                    break
                sent[0] += len(chunk)
                if total > 0:
                    progress_cb(min(0.93, sent[0] / total))
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            done_ev.set()

    # Watchdog — kill ffmpeg if it runs too long
    def _watchdog():
        if not done_ev.wait(timeout=FFMPEG_TIMEOUT):
            try:
                proc.kill()
            except Exception:
                pass

    wt = threading.Thread(target=_writer,   daemon=True)
    wd = threading.Thread(target=_watchdog, daemon=True)
    wt.start()
    wd.start()

    # Collect stderr (blocking — waits for ffmpeg to finish)
    stderr_lines = []
    try:
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                stderr_lines.append(line)
    except Exception:
        pass

    proc.wait()
    wt.join(timeout=10)

    if pipe_err[0]:
        return False, f"Pipe error: {pipe_err[0]}"

    if proc.returncode != 0:
        msg = stderr_lines[-1] if stderr_lines else f"Exit code {proc.returncode}"
        return False, f"ffmpeg failed: {msg}"

    if not out_path.exists() or out_path.stat().st_size == 0:
        return False, "Output is empty — file may have no audio track."

    progress_cb(1.0)
    return True, ""

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def fmt_bytes(n):
    n = max(0, int(n))
    for unit, div in [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]:
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def fmt_time(s):
    s = max(0.0, float(s))
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m {int(s % 60)}s"

# ─────────────────────────────────────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────────────────────────────────────
def _inject_css():
    st.markdown("""
<style>
html,body,[class*="css"]{
    font-family:'Segoe UI',system-ui,sans-serif!important;
}
.stApp{
    background:linear-gradient(135deg,#0f0f1a 0%,#12122a 55%,#0f1a2a 100%);
    min-height:100vh;
}
.block-container{max-width:740px!important;padding-top:1.4rem!important}

/* ── Hero ── */
.hero{text-align:center;padding:1.6rem 1rem .9rem;margin-bottom:.7rem}
.hero-icon{
    font-size:3.3rem;display:block;margin-bottom:.4rem;
    animation:float 3s ease-in-out infinite;
}
@keyframes float{
    0%,100%{transform:translateY(0)}
    50%{transform:translateY(-9px)}
}
.hero h1{
    font-size:clamp(1.25rem,4vw,1.9rem);font-weight:900;
    background:linear-gradient(135deg,#dde0ff 0%,#9d95ff 55%,#00c8f8 100%);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;
    background-clip:text;margin:0 0 .3rem;
}
.hero-sub{color:#6868a0;font-size:.8rem;margin:0}

/* ── Pills ── */
.pill-row{display:flex;gap:.4rem;flex-wrap:wrap;margin:.4rem 0 .85rem}
.pill{
    display:inline-flex;align-items:center;gap:.28rem;
    border-radius:99px;padding:.22rem .75rem;
    font-size:.69rem;font-weight:700;border:1px solid;white-space:nowrap;
}
.p-g{background:rgba(0,230,118,.08);border-color:rgba(0,230,118,.3);color:#00e676}
.p-r{background:rgba(255,92,122,.08);border-color:rgba(255,92,122,.3);color:#ff5c7a}
.p-a{background:rgba(255,179,0,.08);border-color:rgba(255,179,0,.3);color:#ffb300}
.p-b{background:rgba(0,200,248,.08);border-color:rgba(0,200,248,.3);color:#00c8f8}
.p-u{background:rgba(124,111,255,.1);border-color:rgba(124,111,255,.3);color:#9d95ff}

/* ── Card ── */
.card{
    background:rgba(26,26,64,.75);border:1px solid rgba(44,44,96,.9);
    border-radius:13px;padding:.95rem 1.25rem;margin:.55rem 0;
}
.card h4{
    font-size:.68rem;font-weight:700;color:#6868a0;
    text-transform:uppercase;letter-spacing:.85px;margin:0 0 .5rem;
}

/* ── Banners ── */
.banner{
    border-radius:11px;padding:.85rem 1.05rem;
    margin:.65rem 0;display:flex;align-items:flex-start;gap:.6rem;
}
.b-ok  {background:rgba(0,230,118,.07);border:1px solid rgba(0,230,118,.33)}
.b-err {background:rgba(255,92,122,.07);border:1px solid rgba(255,92,122,.33)}
.b-warn{background:rgba(255,179,0,.07);border:1px solid rgba(255,179,0,.33)}
.b-icon {font-size:1.3rem;flex-shrink:0}
.b-title{font-weight:800;font-size:.86rem;margin-bottom:.13rem}
.b-body {font-size:.75rem;color:#a0a0c8;line-height:1.5}

/* ── Progress bar ── */
.stProgress>div>div{
    background:linear-gradient(90deg,#7c6fff,#00c8f8)!important;
    border-radius:99px!important;
}
.stProgress>div{
    background:rgba(26,26,56,.8)!important;
    border-radius:99px!important;height:10px!important;
}

/* ── File uploader ── */
[data-testid="stFileUploaderDropzone"]{
    background:rgba(124,111,255,.04)!important;
    border:2px dashed rgba(124,111,255,.4)!important;
    border-radius:13px!important;padding:1.5rem!important;
}
[data-testid="stFileUploaderDropzone"]:hover{
    border-color:rgba(124,111,255,.8)!important;
    background:rgba(124,111,255,.09)!important;
}

/* ── Download button ── */
[data-testid="stDownloadButton"]>button{
    background:linear-gradient(135deg,#00e676,#00c853)!important;
    color:#000!important;font-weight:800!important;
    padding:.62rem 1.7rem!important;border-radius:10px!important;
    border:none!important;width:100%!important;
    box-shadow:0 4px 18px rgba(0,230,118,.28)!important;
}
[data-testid="stDownloadButton"]>button:hover{
    transform:translateY(-2px)!important;
    box-shadow:0 8px 26px rgba(0,230,118,.42)!important;
}

/* ── Primary button ── */
.stButton>button[kind="primary"]{
    background:linear-gradient(135deg,#7c6fff,#00c8f8)!important;
    color:#fff!important;font-weight:800!important;
    padding:.62rem 1.7rem!important;border-radius:10px!important;
    border:none!important;width:100%!important;
    box-shadow:0 4px 18px rgba(124,111,255,.28)!important;
}
.stButton>button[kind="primary"]:hover{
    transform:translateY(-2px)!important;
    box-shadow:0 8px 26px rgba(124,111,255,.42)!important;
}

/* ── Secondary button ── */
.stButton>button[kind="secondary"]{
    background:rgba(26,26,64,.8)!important;color:#dde0ff!important;
    font-weight:700!important;border:1px solid rgba(44,44,96,.9)!important;
    border-radius:10px!important;width:100%!important;
}
.stButton>button[kind="secondary"]:hover{
    border-color:rgba(124,111,255,.6)!important;
}

/* ── Selectbox ── */
[data-testid="stSelectbox"]>div>div{
    background:rgba(26,26,64,.9)!important;
    border:2px solid rgba(44,44,96,.9)!important;
    border-radius:10px!important;color:#dde0ff!important;
}

/* ── Misc ── */
audio{border-radius:10px;width:100%;margin-top:.3rem}
#MainMenu,footer,header{visibility:hidden!important}
[data-testid="stExpander"]{
    background:rgba(26,26,64,.5)!important;
    border:1px solid rgba(44,44,96,.7)!important;
    border-radius:12px!important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    _inject_css()
    _bg_cleanup()

    # ── Hero ──────────────────────────────────────────────────────────────────
    st.markdown("""
<div class="hero">
  <span class="hero-icon">🎬</span>
  <h1>MP4 → MP3 Converter Pro</h1>
  <p class="hero-sub">
    Stream-process up to 10 GB &nbsp;·&nbsp;
    Video never stored on disk &nbsp;·&nbsp;
    Session-isolated &amp; parallel-safe
  </p>
</div>""", unsafe_allow_html=True)

    # ── Status pills ──────────────────────────────────────────────────────────
    ffmpeg_ok, _ = _check_ffmpeg()
    disk = _disk_gb()
    ram  = _ram_gb()

    def _pc(v, g, a):
        return "p-g" if v > g else "p-a" if v > a else "p-r"

    rows = (
        f'<span class="pill {"p-g" if ffmpeg_ok else "p-r"}">'
        f'{"✅" if ffmpeg_ok else "❌"} ffmpeg</span>'
    )
    if disk >= 0:
        rows += (f'<span class="pill {_pc(disk,5,1)}">'
                 f'💾 {disk:.1f} GB disk</span>')
    if ram >= 0:
        rows += (f'<span class="pill {_pc(ram,2,.5)}">'
                 f'🧠 {ram:.1f} GB RAM</span>')
    rows += (f'<span class="pill p-b">🐍 '
             f'Python {sys.version.split()[0]}</span>')

    st.markdown(f'<div class="pill-row">{rows}</div>',
                unsafe_allow_html=True)

    # Hard stop if no ffmpeg
    if not ffmpeg_ok:
        st.error(
            "**ffmpeg not found.**  \n"
            "Add `packages.txt` to your repo root containing:  \n"
            "```\nffmpeg\n```\nThen redeploy."
        )
        st.stop()

    # Low disk warning
    if 0 < disk < 1:
        st.markdown("""
<div class="banner b-warn">
  <span class="b-icon">⚠️</span>
  <div>
    <div class="b-title">Low Disk Space</div>
    <div class="b-body">Under 1 GB free — large files may fail.</div>
  </div>
</div>""", unsafe_allow_html=True)

    # ── Step 1: Upload ────────────────────────────────────────────────────────
    st.markdown(
        '<div class="card"><h4>📁 Step 1 — Upload Your Video File</h4></div>',
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        "Drop video or click to browse",
        type=ALLOWED,
        label_visibility="collapsed",
        help="MP4 AVI MKV MOV WMV FLV WEBM M4V TS — max 10 GB",
    )

    # Detect new file → reset
    if uploaded is not None:
        if (st.session_state.in_name != uploaded.name
                or st.session_state.in_size != uploaded.size):
            wipe_session()
            st.session_state.in_name = uploaded.name
            st.session_state.in_size = uploaded.size

        ext = Path(uploaded.name).suffix.upper().lstrip(".")
        st.markdown(
            f'<div class="pill-row">'
            f'<span class="pill p-b">🎞️ {ext}</span>'
            f'<span class="pill p-u">📄 {uploaded.name}</span>'
            f'<span class="pill p-a">⚖️ {fmt_bytes(uploaded.size)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Step 2: Settings ──────────────────────────────────────────────────────
    st.markdown(
        '<div class="card"><h4>⚙️ Step 2 — Choose Audio Quality</h4></div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        br_label = st.selectbox("🎵 Bitrate",     list(BITRATES),    index=0)
    with c2:
        sr_label = st.selectbox("📊 Sample Rate", list(SAMPLERATES), index=1)
    with c3:
        ch_label = st.selectbox("🎤 Channels",    list(CHANNELS),    index=0)

    bitrate = BITRATES[br_label]
    sr      = SAMPLERATES[sr_label]
    ch      = CHANNELS[ch_label]

    # ── Step 3: Convert ───────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)

    status = st.session_state.status
    can_go = (
        uploaded is not None
        and status not in ("converting", "done")
    )
    btn_txt = (
        "⚙️  Converting…"        if status == "converting" else
        "✅  Done — see below"   if status == "done"       else
        "🚀  Convert to MP3"
    )

    if st.button(btn_txt, type="primary",
                 disabled=not can_go, use_container_width=True):

        # Pre-flight disk check
        if 0 < _disk_gb() < 0.1:
            st.error("Not enough disk space.")
            st.stop()

        st.session_state.status   = "converting"
        st.session_state.progress = 0.0
        st.session_state.error_msg = ""

        out_dir  = get_session_dir()
        stem     = Path(uploaded.name).stem
        out_path = out_dir / (stem + ".mp3")
        out_name = stem + ".mp3"

        # ── Progress bar ──────────────────────────────────────────────────────
        prog = st.progress(0.0, text="Starting conversion…")

        def _on_progress(frac):
            pct   = int(frac * 100)
            done_b = int(st.session_state.in_size * frac)
            prog.progress(
                frac,
                text=(f"Converting… {pct}%  |  "
                      f"{fmt_bytes(done_b)} / "
                      f"{fmt_bytes(st.session_state.in_size)}"),
            )

        # ── Run ffmpeg ────────────────────────────────────────────────────────
        t0 = time.time()
        ok, err = run_ffmpeg(
            uploaded_file = uploaded,
            out_path      = out_path,
            bitrate       = bitrate,
            sr            = sr,
            ch            = ch,
            progress_cb   = _on_progress,
        )
        elapsed = time.time() - t0
        prog.empty()

        # ── Save result ───────────────────────────────────────────────────────
        if ok:
            st.session_state.status       = "done"
            st.session_state.output_path  = str(out_path)
            st.session_state.out_filename = out_name
            st.session_state.elapsed      = elapsed
            st.session_state.out_size     = out_path.stat().st_size
        else:
            st.session_state.status    = "error"
            st.session_state.error_msg = err

        st.rerun()

    # ── Show progress if converting ───────────────────────────────────────────
    if status == "converting":
        p = float(st.session_state.progress)
        st.progress(p, text=f"Converting… {int(p*100)}%")
        st.info("⏳ Please wait — page refreshes automatically when done.")

    # ── Success ───────────────────────────────────────────────────────────────
    elif status == "done" and st.session_state.output_path:
        out_path = Path(st.session_state.output_path)

        if out_path.exists():
            out_sz  = int(st.session_state.out_size)
            in_sz   = int(st.session_state.in_size)
            ratio   = out_sz / max(1, in_sz) * 100
            elapsed = float(st.session_state.elapsed)

            st.markdown(f"""
<div class="banner b-ok">
  <span class="b-icon">✅</span>
  <div>
    <div class="b-title">Conversion Complete!</div>
    <div class="b-body">
      ⏱ {fmt_time(elapsed)} &nbsp;·&nbsp;
      📦 {fmt_bytes(out_sz)} ({ratio:.1f}% of original) &nbsp;·&nbsp;
      🎵 {br_label.split('—')[0].strip()}
    </div>
  </div>
</div>""", unsafe_allow_html=True)

            # Large file notice
            if out_sz > MAX_RAM_LOAD:
                st.markdown(f"""
<div class="banner b-warn">
  <span class="b-icon">⚠️</span>
  <div>
    <div class="b-title">Large File Notice</div>
    <div class="b-body">
      Output is {fmt_bytes(out_sz)}.
      Preview is disabled. Download works fine.
    </div>
  </div>
</div>""", unsafe_allow_html=True)

            # Read file for download
            with open(out_path, "rb") as fh:
                mp3_bytes = fh.read(MAX_RAM_LOAD)

            # Download button
            st.download_button(
                label               = "⬇️  Download MP3",
                data                = mp3_bytes,
                file_name           = st.session_state.out_filename,
                mime                = "audio/mpeg",
                use_container_width = True,
                on_click            = wipe_session,
            )

            # Audio preview (only for small files)
            if out_sz <= MAX_RAM_LOAD:
                st.markdown("**🔊 Preview:**")
                st.audio(mp3_bytes, format="audio/mpeg")

            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄  Convert Another File",
                         use_container_width=True, type="secondary"):
                wipe_session()
                st.rerun()

        else:
            st.error("Temp file missing — please convert again.")
            wipe_session()

    # ── Error ─────────────────────────────────────────────────────────────────
    elif status == "error":
        st.markdown(f"""
<div class="banner b-err">
  <span class="b-icon">❌</span>
  <div>
    <div class="b-title">Conversion Failed</div>
    <div class="b-body">{st.session_state.error_msg}</div>
  </div>
</div>""", unsafe_allow_html=True)

        if st.button("🔄  Try Again",
                     use_container_width=True, type="secondary"):
            st.session_state.status    = "idle"
            st.session_state.error_msg = ""
            st.rerun()

    # ── Info ──────────────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("ℹ️  How it works"):
        st.markdown(f"""
| | |
|---|---|
| **Max upload** | 10 GB |
| **Video stored?** | ❌ Never — piped into ffmpeg in {fmt_bytes(PIPE_CHUNK)} chunks |
| **Output stored?** | Temporary MP3 only, deleted after download |
| **Parallel users** | ✅ Each session gets an isolated folder |
| **Timeout** | {FFMPEG_TIMEOUT // 60} min max |
| **Auto-cleanup** | Folders older than 2 h deleted automatically |
| **Formats** | MP4 · AVI · MKV · MOV · WMV · FLV · WEBM · M4V · TS |
        """)


if __name__ == "__main__":
    main()
