# app.py
# Requirements: streamlit==1.45.1, psutil==5.9.8
# packages.txt: ffmpeg
# .streamlit/config.toml: maxUploadSize = 10240

import streamlit as st

# ── MUST be first Streamlit call ─────────────────────────────────────────────
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
import traceback
from pathlib import Path

# ── Optional psutil ──────────────────────────────────────────────────────────
try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
PIPE_CHUNK      = 2 * 1024 * 1024      # 2 MB per pipe chunk (safe for all RAM)
READ_CHUNK      = 8 * 1024 * 1024      # 8 MB read chunk from download
FFMPEG_TIMEOUT  = 7200                 # 2 hours max per file
MAX_OUTPUT_LOAD = 256 * 1024 * 1024    # 256 MB — load into RAM for preview only

TMP_ROOT = Path(tempfile.gettempdir()) / "mp4mp3conv"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Thread lock for any shared-state writes
_LOCK = threading.Lock()

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
#  SESSION STATE  — thread-safe bootstrap
# ─────────────────────────────────────────────────────────────────────────────
_STATE_DEFAULTS = {
    "sid"           : None,   # unique session folder name
    "status"        : "idle", # idle | converting | done | error
    "error_msg"     : "",
    "output_path"   : None,   # Path str
    "out_filename"  : None,
    "in_name"       : None,
    "in_size"       : 0,
    "out_size"      : 0,
    "elapsed"       : 0.0,
    "progress"      : 0.0,   # 0.0 – 1.0
    "cancel_flag"   : False,
}

def _boot_state():
    for k, v in _STATE_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v
    if st.session_state.sid is None:
        st.session_state.sid = uuid.uuid4().hex

_boot_state()

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION DIRECTORY  — one per browser tab, isolated
# ─────────────────────────────────────────────────────────────────────────────
def session_dir() -> Path:
    d = TMP_ROOT / st.session_state.sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def wipe_session(new_sid: bool = True):
    """Delete temp files, reset state. Thread-safe."""
    with _LOCK:
        try:
            d = TMP_ROOT / st.session_state.sid
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        for k, v in _STATE_DEFAULTS.items():
            st.session_state[k] = v
        if new_sid:
            st.session_state.sid = uuid.uuid4().hex

# ─────────────────────────────────────────────────────────────────────────────
#  STALE TEMP CLEANUP  — removes folders older than 2 hours
#  Safe to call from any session; uses its own lock
# ─────────────────────────────────────────────────────────────────────────────
_CLEANUP_LOCK   = threading.Lock()
_last_cleanup   = [0.0]

def maybe_cleanup_old_temps():
    """Run at most once every 10 minutes across all sessions."""
    now = time.time()
    with _CLEANUP_LOCK:
        if now - _last_cleanup[0] < 600:
            return
        _last_cleanup[0] = now

    def _do():
        try:
            cutoff = time.time() - 7200      # 2 hours
            for child in TMP_ROOT.iterdir():
                if child.is_dir():
                    try:
                        if child.stat().st_mtime < cutoff:
                            shutil.rmtree(child, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
#  SYSTEM INFO
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=30, show_spinner=False)
def _ffmpeg_check() -> tuple:
    """Cached for 30 s so every user rerun doesn't shell out."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            ver = (r.stdout or "").splitlines()[0]
            return True, ver
        return False, "non-zero exit"
    except FileNotFoundError:
        return False, "not in PATH"
    except Exception as e:
        return False, str(e)


def disk_free_gb() -> float:
    try:
        if _PSUTIL:
            return psutil.disk_usage(str(TMP_ROOT)).free / 1e9
        s = os.statvfs(str(TMP_ROOT))
        return s.f_bavail * s.f_frsize / 1e9
    except Exception:
        return -1.0


def ram_free_gb() -> float:
    try:
        if _PSUTIL:
            return psutil.virtual_memory().available / 1e9
        # Read /proc/meminfo on Linux
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) / 1e6
    except Exception:
        pass
    return -1.0

# ─────────────────────────────────────────────────────────────────────────────
#  CORE CONVERTER
#  Architecture:
#    Writer thread  → pushes upload chunks into ffmpeg stdin (non-blocking)
#    Main thread    → reads ffmpeg stderr for progress / errors
#    Watchdog timer → kills ffmpeg if it hangs > FFMPEG_TIMEOUT
#
#  Source video bytes are NEVER written to disk.
#  Only the output MP3 touches the filesystem.
# ─────────────────────────────────────────────────────────────────────────────
def convert(uploaded_file, out_path: Path,
            bitrate: str, sr: str, ch: str,
            progress_cb) -> tuple:
    """
    Returns (success: bool, error_msg: str).
    progress_cb(float 0..1) is called from the writer thread — 
    it only updates session_state.progress (no Streamlit calls).
    """

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-loglevel",  "error",          # only real errors to stderr
        "-i",         "pipe:0",         # video comes from stdin
        "-vn",                          # strip video
        "-acodec",    "libmp3lame",
        "-b:a",       bitrate,
        "-ar",        sr,
        "-ac",        ch,
        "-q:a",       "0",
        str(out_path),
    ]

    # ── Launch ────────────────────────────────────────────────────────────────
    try:
        proc = subprocess.Popen(
            cmd,
            stdin  = subprocess.PIPE,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg binary not found. See packages.txt."
    except Exception as e:
        return False, f"Cannot launch ffmpeg: {e}"

    total     = int(st.session_state.in_size)
    sent      = [0]
    pipe_err  = [None]
    write_done = threading.Event()

    # ── Writer thread ─────────────────────────────────────────────────────────
    def _writer():
        try:
            uploaded_file.seek(0)
            while True:
                # Respect cancellation
                if st.session_state.cancel_flag:
                    break
                chunk = uploaded_file.read(PIPE_CHUNK)
                if not chunk:
                    break
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except BrokenPipeError:
                    break   # ffmpeg closed early — returncode tells why
                except Exception as e:
                    pipe_err[0] = str(e)
                    break
                sent[0] += len(chunk)
                if total > 0:
                    progress_cb(min(0.94, sent[0] / total))
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            write_done.set()

    wt = threading.Thread(target=_writer, daemon=True)
    wt.start()

    # ── Watchdog ──────────────────────────────────────────────────────────────
    def _watchdog():
        if not write_done.wait(timeout=FFMPEG_TIMEOUT):
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=_watchdog, daemon=True).start()

    # ── Collect stderr (non-blocking reader) ──────────────────────────────────
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

    # ── Cancellation ─────────────────────────────────────────────────────────
    if st.session_state.cancel_flag:
        try:
            proc.kill()
        except Exception:
            pass
        return False, "Cancelled by user."

    # ── Pipe error ────────────────────────────────────────────────────────────
    if pipe_err[0]:
        return False, f"Pipe write error: {pipe_err[0]}"

    # ── ffmpeg failed ─────────────────────────────────────────────────────────
    if proc.returncode != 0:
        msg = stderr_lines[-1] if stderr_lines else f"Exit code {proc.returncode}"
        return False, f"ffmpeg error: {msg}"

    # ── Empty output ──────────────────────────────────────────────────────────
    if not out_path.exists() or out_path.stat().st_size == 0:
        return False, "Output is empty — file may have no audio track."

    progress_cb(1.0)
    return True, ""

# ─────────────────────────────────────────────────────────────────────────────
#  FORMATTING
# ─────────────────────────────────────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    for unit, div in [("GB", 1<<30), ("MB", 1<<20), ("KB", 1<<10)]:
        if n >= div:
            return f"{n/div:.2f} {unit}"
    return f"{n} B"


def fmt_time(s: float) -> str:
    s = max(0.0, float(s))
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s//60)}m {int(s%60)}s"

# ─────────────────────────────────────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────────────────────────────────────
def _css():
    st.markdown("""
<style>
/* Base */
html,body,[class*="css"]{font-family:'Segoe UI',system-ui,sans-serif!important}
.stApp{
  background:linear-gradient(135deg,#0f0f1a 0%,#12122a 55%,#0f1a2a 100%);
  min-height:100vh;
}
.block-container{max-width:740px!important;padding-top:1.4rem!important}

/* Hero */
.hero{text-align:center;padding:1.6rem 1rem .9rem;margin-bottom:.7rem}
.hero-icon{
  font-size:3.3rem;display:block;margin-bottom:.4rem;
  animation:float 3s ease-in-out infinite;
}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-9px)}}
.hero h1{
  font-size:clamp(1.25rem,4vw,1.9rem);font-weight:900;
  background:linear-gradient(135deg,#dde0ff 0%,#9d95ff 55%,#00c8f8 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;margin:0 0 .3rem;
}
.hero-sub{color:#6868a0;font-size:.8rem;margin:0}

/* Pills */
.pr{display:flex;gap:.4rem;flex-wrap:wrap;margin:.4rem 0 .9rem}
.pill{
  display:inline-flex;align-items:center;gap:.28rem;
  border-radius:99px;padding:.22rem .75rem;
  font-size:.69rem;font-weight:700;border:1px solid;white-space:nowrap;
}
.pu{background:rgba(124,111,255,.1);border-color:rgba(124,111,255,.3);color:#9d95ff}
.pg{background:rgba(0,230,118,.08);border-color:rgba(0,230,118,.3);color:#00e676}
.pr2{background:rgba(255,92,122,.08);border-color:rgba(255,92,122,.3);color:#ff5c7a}
.pa{background:rgba(255,179,0,.08);border-color:rgba(255,179,0,.3);color:#ffb300}
.pb{background:rgba(0,200,248,.08);border-color:rgba(0,200,248,.3);color:#00c8f8}

/* Card */
.card{
  background:rgba(26,26,64,.75);border:1px solid rgba(44,44,96,.9);
  border-radius:13px;padding:.95rem 1.25rem;margin:.55rem 0;
  backdrop-filter:blur(6px);
}
.card h4{
  font-size:.68rem;font-weight:700;color:#6868a0;
  text-transform:uppercase;letter-spacing:.85px;margin:0 0 .55rem;
}

/* Banners */
.banner{
  border-radius:11px;padding:.85rem 1.05rem;
  margin:.65rem 0;display:flex;align-items:flex-start;gap:.6rem;
}
.b-ok {background:rgba(0,230,118,.07);border:1px solid rgba(0,230,118,.33)}
.b-err{background:rgba(255,92,122,.07);border:1px solid rgba(255,92,122,.33)}
.b-warn{background:rgba(255,179,0,.07);border:1px solid rgba(255,179,0,.33)}
.b-icon{font-size:1.3rem;flex-shrink:0}
.b-title{font-weight:800;font-size:.86rem;margin-bottom:.13rem}
.b-body{font-size:.75rem;color:#a0a0c8;line-height:1.5}

/* Progress */
.stProgress>div>div{
  background:linear-gradient(90deg,#7c6fff,#00c8f8)!important;
  border-radius:99px!important;
}
.stProgress>div{
  background:rgba(26,26,56,.8)!important;
  border-radius:99px!important;height:10px!important;
}

/* Upload */
[data-testid="stFileUploaderDropzone"]{
  background:rgba(124,111,255,.04)!important;
  border:2px dashed rgba(124,111,255,.4)!important;
  border-radius:13px!important;padding:1.5rem!important;
  transition:all .3s!important;
}
[data-testid="stFileUploaderDropzone"]:hover{
  border-color:rgba(124,111,255,.8)!important;
  background:rgba(124,111,255,.09)!important;
}

/* Buttons */
[data-testid="stDownloadButton"]>button{
  background:linear-gradient(135deg,#00e676,#00c853)!important;
  color:#000!important;font-weight:800!important;
  font-size:.9rem!important;padding:.62rem 1.7rem!important;
  border-radius:10px!important;border:none!important;width:100%!important;
  box-shadow:0 4px 18px rgba(0,230,118,.28)!important;
  transition:transform .15s,box-shadow .15s!important;
}
[data-testid="stDownloadButton"]>button:hover{
  transform:translateY(-2px)!important;
  box-shadow:0 8px 26px rgba(0,230,118,.42)!important;
}
.stButton>button[kind="primary"]{
  background:linear-gradient(135deg,#7c6fff,#00c8f8)!important;
  color:#fff!important;font-weight:800!important;
  font-size:.9rem!important;padding:.62rem 1.7rem!important;
  border-radius:10px!important;border:none!important;width:100%!important;
  box-shadow:0 4px 18px rgba(124,111,255,.28)!important;
  transition:transform .15s,box-shadow .15s!important;
}
.stButton>button[kind="primary"]:hover{
  transform:translateY(-2px)!important;
  box-shadow:0 8px 26px rgba(124,111,255,.42)!important;
}
.stButton>button[kind="secondary"]{
  background:rgba(26,26,64,.8)!important;color:#dde0ff!important;
  font-weight:700!important;border:1px solid rgba(44,44,96,.9)!important;
  border-radius:10px!important;width:100%!important;
  transition:border-color .2s!important;
}
.stButton>button[kind="secondary"]:hover{
  border-color:rgba(124,111,255,.6)!important;
}

/* Selectbox */
[data-testid="stSelectbox"]>div>div{
  background:rgba(26,26,64,.9)!important;
  border:2px solid rgba(44,44,96,.9)!important;
  border-radius:10px!important;color:#dde0ff!important;
}

/* Misc */
audio{border-radius:10px;width:100%;margin-top:.3rem}
#MainMenu,footer,header{visibility:hidden!important}
[data-testid="stExpander"]{
  background:rgba(26,26,64,.5)!important;
  border:1px solid rgba(44,44,96,.7)!important;
  border-radius:12px!important;
}
</style>""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
#  STREAMING FILE READER  — for large file downloads (no full RAM load)
# ─────────────────────────────────────────────────────────────────────────────
def read_file_chunked(path: Path) -> bytes:
    """
    Reads the output MP3 into bytes.
    If file > MAX_OUTPUT_LOAD we return only the first MAX_OUTPUT_LOAD bytes
    for the preview/download widget and warn the user.
    For production, replace with st.download_button(data=open(path,'rb')).
    """
    size = path.stat().st_size
    buf  = bytearray()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) >= MAX_OUTPUT_LOAD:
                break
    return bytes(buf)

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    _css()
    maybe_cleanup_old_temps()   # background stale-file sweep

    # ── Hero ──────────────────────────────────────────────────────────────────
    st.markdown("""
<div class="hero">
  <span class="hero-icon">🎬</span>
  <h1>MP4 → MP3 Converter Pro</h1>
  <p class="hero-sub">
    Stream-process up to 10 GB &nbsp;·&nbsp;
    Video never stored on disk &nbsp;·&nbsp;
    Parallel-session safe
  </p>
</div>""", unsafe_allow_html=True)

    # ── System status ──────────────────────────────────────────────────────────
    ffmpeg_ok, ffmpeg_ver = _ffmpeg_check()
    disk  = disk_free_gb()
    ram   = ram_free_gb()

    def _pcls(v, g, a):   # pill colour class
        return "pg" if v > g else "pa" if v > a else "pr2"

    pills = (
        f'<span class="pill {"pg" if ffmpeg_ok else "pr2"}">'
        f'{"✅" if ffmpeg_ok else "❌"} ffmpeg</span>'
    )
    if disk >= 0:
        pills += (f'<span class="pill {_pcls(disk,5,1)}">'
                  f'💾 {disk:.1f} GB disk</span>')
    if ram >= 0:
        pills += (f'<span class="pill {_pcls(ram,2,.5)}">'
                  f'🧠 {ram:.1f} GB RAM</span>')
    pills += '<span class="pill pb">🐍 Python ' + sys.version.split()[0] + '</span>'

    st.markdown(f'<div class="pr">{pills}</div>', unsafe_allow_html=True)

    if not ffmpeg_ok:
        st.error(
            "**ffmpeg not found.**\n\n"
            "Add a file named **`packages.txt`** to your repo root "
            "with one line: `ffmpeg`\n\nThen redeploy."
        )
        st.stop()

    # Low disk warning
    if 0 < disk < 1:
        st.markdown("""
<div class="banner b-warn">
  <span class="b-icon">⚠️</span>
  <div>
    <div class="b-title">Low Disk Space</div>
    <div class="b-body">Less than 1 GB free — large conversions may fail.</div>
  </div>
</div>""", unsafe_allow_html=True)

    # ── Upload ─────────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="card"><h4>📁 Step 1 — Upload Video</h4></div>',
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        "Drop a video file or click to browse",
        type=ALLOWED,
        label_visibility="collapsed",
        help="Supported: MP4 AVI MKV MOV WMV FLV WEBM M4V TS  |  Max 10 GB",
    )

    # Detect new file → reset previous state
    if uploaded is not None:
        name_changed = st.session_state.in_name != uploaded.name
        size_changed = st.session_state.in_size  != uploaded.size
        if name_changed or size_changed:
            wipe_session(new_sid=True)
            st.session_state.in_name = uploaded.name
            st.session_state.in_size = uploaded.size

        ext = Path(uploaded.name).suffix.upper().lstrip(".")
        st.markdown(
            f'<div class="pr">'
            f'<span class="pill pb">🎞️ {ext}</span>'
            f'<span class="pill pu">📄 {uploaded.name}</span>'
            f'<span class="pill pa">⚖️ {fmt_bytes(uploaded.size)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Settings ───────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="card"><h4>⚙️ Step 2 — Audio Settings</h4></div>',
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        br_label = st.selectbox("🎵 Bitrate",     list(BITRATES),     index=0)
    with c2:
        sr_label = st.selectbox("📊 Sample Rate", list(SAMPLERATES),  index=1)
    with c3:
        ch_label = st.selectbox("🎤 Channels",    list(CHANNELS),     index=0)

    bitrate = BITRATES[br_label]
    sr      = SAMPLERATES[sr_label]
    ch      = CHANNELS[ch_label]

    # ── Convert button ─────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    status = st.session_state.status

    can_convert = (
        uploaded is not None
        and status not in ("converting", "done")
    )
    btn_label = (
        "⚙️  Converting…" if status == "converting" else
        "✅  Done — scroll down" if status == "done" else
        "🚀  Convert to MP3"
    )

    if st.button(btn_label, type="primary",
                 disabled=not can_convert, use_container_width=True):

        # ── Guard: check disk before starting ────────────────────────────────
        d = disk_free_gb()
        if 0 < d < 0.2:
            st.error("Not enough disk space to write output file.")
            st.stop()

        st.session_state.status      = "converting"
        st.session_state.error_msg   = ""
        st.session_state.progress    = 0.0
        st.session_state.cancel_flag = False

        out_dir  = session_dir()
        stem     = Path(uploaded.name).stem
        out_path = out_dir / (stem + ".mp3")
        out_name = stem + ".mp3"

        # ── Live progress UI ──────────────────────────────────────────────────
        prog  = st.progress(0.0, text="Starting…")
        t_ref = [time.time()]

        def _progress(frac):
            """Called from writer thread — only writes to session state."""
            st.session_state.progress = frac
            # We cannot call Streamlit widget methods from other threads,
            # so we use a slot update pattern: the main thread polls.

        # ── Run (blocking — Streamlit is single-threaded per session) ────────
        t0 = time.time()
        ok, err = convert(
            uploaded_file = uploaded,
            out_path      = out_path,
            bitrate       = bitrate,
            sr            = sr,
            ch            = ch,
            progress_cb   = _progress,
            cancel_ev     = None,   # cancel via session_state.cancel_flag
        )
        elapsed = time.time() - t0
        prog.empty()

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

    # ── Conversion progress display (between reruns) ───────────────────────────
    if status == "converting":
        pct = st.session_state.progress
        st.progress(pct, text=f"Converting… {int(pct*100)}%")
        st.info("⏳ Conversion running — this page will refresh when done.")

    # ── Success ────────────────────────────────────────────────────────────────
    elif status == "done" and st.session_state.output_path:
        out_path = Path(st.session_state.output_path)

        if out_path.exists():
            out_sz  = st.session_state.out_size
            in_sz   = st.session_state.in_size
            ratio   = out_sz / max(1, in_sz) * 100
            elapsed = st.session_state.elapsed

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

            # ── Large file warning ────────────────────────────────────────────
            if out_sz > MAX_OUTPUT_LOAD:
                st.markdown(f"""
<div class="banner b-warn">
  <span class="b-icon">⚠️</span>
  <div>
    <div class="b-title">Large Output File</div>
    <div class="b-body">
      Output is {fmt_bytes(out_sz)}.
      Only the first {fmt_bytes(MAX_OUTPUT_LOAD)} loads into
      the browser preview. The full file downloads correctly.
    </div>
  </div>
</div>""", unsafe_allow_html=True)

            # ── Download button — stream file in chunks ───────────────────────
            mp3_data = read_file_chunked(out_path)

            st.download_button(
                label               = "⬇️  Download MP3",
                data                = mp3_data,
                file_name           = st.session_state.out_filename,
                mime                = "audio/mpeg",
                use_container_width = True,
                on_click            = wipe_session,
            )

            # ── Preview ───────────────────────────────────────────────────────
            if out_sz <= MAX_OUTPUT_LOAD:
                st.markdown("**🔊 Preview:**")
                st.audio(mp3_data, format="audio/mpeg")
            else:
                st.info("Preview skipped — file too large for browser audio player.")

            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄  Convert Another File",
                         use_container_width=True, type="secondary"):
                wipe_session()
                st.rerun()

        else:
            st.error("Temp file missing — server may have restarted. Please convert again.")
            wipe_session()

    # ── Error ──────────────────────────────────────────────────────────────────
    elif status == "error":
        st.markdown(f"""
<div class="banner b-err">
  <span class="b-icon">❌</span>
  <div>
    <div class="b-title">Conversion Failed</div>
    <div class="b-body">{st.session_state.error_msg}</div>
  </div>
</div>""", unsafe_allow_html=True)

        if st.button("🔄  Try Again", use_container_width=True, type="secondary"):
            st.session_state.status    = "idle"
            st.session_state.error_msg = ""
            st.rerun()

    # ── Info expander ──────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("ℹ️  Technical Details & Limits"):
        st.markdown(f"""
| Setting | Value |
|---|---|
| **Max upload** | 10 GB (`.streamlit/config.toml`) |
| **Video stored?** | ❌ Never — piped into ffmpeg stdin in {fmt_bytes(PIPE_CHUNK)} chunks |
| **Output stored?** | ✅ Temporary MP3 only, auto-deleted after download |
| **RAM per session** | ~{fmt_bytes(PIPE_CHUNK)} pipe buffer + output size (max {fmt_bytes(MAX_OUTPUT_LOAD)} in browser) |
| **Parallel sessions** | ✅ Each session has isolated temp folder |
| **Timeout** | {FFMPEG_TIMEOUT//60} minutes max per conversion |
| **Auto-cleanup** | Temp folders older than 2 hours deleted automatically |
| **ffmpeg flags** | `-vn -acodec libmp3lame -q:a 0` |
| **Formats** | MP4 · AVI · MKV · MOV · WMV · FLV · WEBM · M4V · TS |
        """)


if __name__ == "__main__":
    main()
