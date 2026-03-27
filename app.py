# app.py — MP4 to MP3 Converter for Streamlit Cloud
# Python 3.14 compatible — no deprecated syntax

import streamlit as st

st.set_page_config(
    page_title="MP4 → MP3 Converter Pro",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

import subprocess
import tempfile
import time
import threading
import uuid
import shutil
import os
from pathlib import Path

# ── psutil is optional ────────────────────────────────────────────────────────
try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
CHUNK_SIZE   = 4 * 1024 * 1024   # 4 MB
TMP_ROOT     = Path(tempfile.gettempdir()) / "mp4_mp3_app"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

BITRATE_OPTIONS = {
    "320 kbps — Maximum quality" : "320k",
    "256 kbps — High quality"    : "256k",
    "192 kbps — Standard"        : "192k",
    "128 kbps — Compact"         : "128k",
    "96 kbps  — Voice/podcast"   : "96k",
}
SAMPLERATE_OPTIONS = {
    "48000 Hz — Studio/video"    : "48000",
    "44100 Hz — CD quality"      : "44100",
    "22050 Hz — Compressed"      : "22050",
}
CHANNEL_OPTIONS = {
    "Stereo (2 channels)"        : "2",
    "Mono   (1 channel)"         : "1",
}
ALLOWED_TYPES = ["mp4", "avi", "mkv", "mov", "wmv", "flv", "webm", "m4v", "ts"]

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "session_id"        : None,
    "converted_path"    : None,
    "output_filename"   : None,
    "file_size_bytes"   : 0,
    "conversion_done"   : False,
    "conversion_error"  : None,
    "converting"        : False,
    "input_name"        : None,
    "conversion_time"   : 0.0,
    "output_size_bytes" : 0,
}

for _k, _v in DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

if st.session_state.session_id is None:
    st.session_state.session_id = str(uuid.uuid4())

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_session_dir():
    d = TMP_ROOT / st.session_state.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def reset_state():
    """Wipe temp files and reset all session state."""
    try:
        d = TMP_ROOT / st.session_state.session_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    for k, v in DEFAULTS.items():
        st.session_state[k] = v
    st.session_state.session_id = str(uuid.uuid4())


def fmt_bytes(n):
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n/1024:.1f} KB"
    if n < 1_073_741_824:
        return f"{n/1_048_576:.2f} MB"
    return f"{n/1_073_741_824:.2f} GB"


def fmt_time(s):
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s//60)}m {int(s%60)}s"


def check_ffmpeg():
    """Returns (ok, message)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            line = r.stdout.splitlines()[0] if r.stdout else "ffmpeg ok"
            return True, line
        return False, "ffmpeg returned error code"
    except FileNotFoundError:
        return False, "ffmpeg not found in PATH"
    except Exception as e:
        return False, str(e)


def get_disk_gb():
    if PSUTIL_OK:
        try:
            return psutil.disk_usage(str(TMP_ROOT)).free / (1024**3)
        except Exception:
            pass
    # fallback using os
    try:
        s = os.statvfs(str(TMP_ROOT))
        return (s.f_bavail * s.f_frsize) / (1024**3)
    except Exception:
        return -1.0


def get_ram_gb():
    if PSUTIL_OK:
        try:
            return psutil.virtual_memory().available / (1024**3)
        except Exception:
            pass
    return -1.0

# ─────────────────────────────────────────────────────────────────────────────
#  FFMPEG CONVERSION  — pipes bytes, never writes source video to disk
# ─────────────────────────────────────────────────────────────────────────────
def run_conversion(uploaded_file, output_path, bitrate,
                   sample_rate, channels, progress_cb, cancel_ev):
    """
    Streams uploaded_file → ffmpeg stdin → output_path (MP3).
    Returns (success: bool, error_msg: str).
    """
    cmd = [
        "ffmpeg", "-y",
        "-i",      "pipe:0",
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a",    bitrate,
        "-ar",     sample_rate,
        "-ac",     channels,
        "-q:a",    "0",
        str(output_path),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin  = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg not found. Add 'ffmpeg' to packages.txt."
    except Exception as e:
        return False, f"Cannot start ffmpeg: {e}"

    total     = int(st.session_state.file_size_bytes)
    sent      = [0]
    write_err = [None]

    def _writer():
        try:
            uploaded_file.seek(0)
            while not cancel_ev.is_set():
                chunk = uploaded_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                sent[0] += len(chunk)
                if total > 0:
                    progress_cb(min(0.93, sent[0] / total))
        except BrokenPipeError:
            pass
        except Exception as e:
            write_err[0] = str(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()

    _out, stderr_bytes = proc.communicate()
    writer.join(timeout=15)

    if cancel_ev.is_set():
        try:
            proc.kill()
        except Exception:
            pass
        return False, "Cancelled."

    if write_err[0]:
        return False, f"Pipe error: {write_err[0]}"

    if proc.returncode != 0:
        err   = stderr_bytes.decode(errors="replace").strip()
        lines = [l for l in err.splitlines() if l.strip()]
        msg   = lines[-1] if lines else "Unknown ffmpeg error"
        return False, f"ffmpeg error (code {proc.returncode}): {msg}"

    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "Output file is empty — file may have no audio track."

    progress_cb(1.0)
    return True, ""

# ─────────────────────────────────────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────────────────────────────────────
CSS = """
<style>
html, body, [class*="css"] {
    font-family: 'Segoe UI', system-ui, sans-serif !important;
}
.stApp {
    background: linear-gradient(135deg,#0f0f1a 0%,#12122a 60%,#0f1a2a 100%);
    min-height: 100vh;
}
.block-container { max-width:760px !important; padding-top:1.5rem !important; }

/* Hero */
.hero { text-align:center; padding:1.8rem 1rem 1rem; margin-bottom:.8rem; }
.hero-icon {
    font-size:3.5rem; display:block; margin-bottom:.4rem;
    animation:float 3s ease-in-out infinite;
}
@keyframes float {
    0%,100%{transform:translateY(0)} 50%{transform:translateY(-9px)}
}
.hero h1 {
    font-size:clamp(1.3rem,4vw,2rem); font-weight:900;
    background:linear-gradient(135deg,#dde0ff 0%,#9d95ff 55%,#00c8f8 100%);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
    background-clip:text; margin:0 0 .3rem;
}
.hero-sub { color:#6868a0; font-size:.83rem; margin:0; }

/* Pills */
.pill {
    display:inline-flex; align-items:center; gap:.3rem;
    border-radius:99px; padding:.25rem .8rem;
    font-size:.71rem; font-weight:700; border:1px solid; white-space:nowrap;
}
.p-purple { background:rgba(124,111,255,.1);border-color:rgba(124,111,255,.3);color:#9d95ff; }
.p-green  { background:rgba(0,230,118,.08); border-color:rgba(0,230,118,.3); color:#00e676; }
.p-red    { background:rgba(255,92,122,.08);border-color:rgba(255,92,122,.3);color:#ff5c7a; }
.p-amber  { background:rgba(255,179,0,.08); border-color:rgba(255,179,0,.3); color:#ffb300; }
.p-blue   { background:rgba(0,200,248,.08); border-color:rgba(0,200,248,.3); color:#00c8f8; }
.pill-row { display:flex; gap:.45rem; flex-wrap:wrap; margin:.5rem 0 .9rem; }

/* Card */
.card {
    background:rgba(26,26,64,.75); border:1px solid rgba(44,44,96,.9);
    border-radius:13px; padding:1rem 1.3rem; margin:.6rem 0;
    backdrop-filter:blur(6px);
}
.card h4 {
    font-size:.7rem; font-weight:700; color:#6868a0;
    text-transform:uppercase; letter-spacing:.9px; margin:0 0 .6rem;
}

/* Banners */
.banner {
    border-radius:12px; padding:.9rem 1.1rem;
    margin:.7rem 0; display:flex; align-items:flex-start; gap:.65rem;
}
.b-ok  { background:rgba(0,230,118,.07);border:1px solid rgba(0,230,118,.35); }
.b-err { background:rgba(255,92,122,.07);border:1px solid rgba(255,92,122,.35); }
.b-icon  { font-size:1.35rem; flex-shrink:0; }
.b-title { font-weight:800; font-size:.88rem; margin-bottom:.15rem; }
.b-body  { font-size:.76rem; color:#a0a0c8; line-height:1.55; }

/* Progress */
.stProgress > div > div {
    background:linear-gradient(90deg,#7c6fff,#00c8f8) !important;
    border-radius:99px !important;
}
.stProgress > div {
    background:rgba(26,26,56,.8) !important;
    border-radius:99px !important; height:10px !important;
}

/* Upload zone */
[data-testid="stFileUploaderDropzone"] {
    background:rgba(124,111,255,.04) !important;
    border:2px dashed rgba(124,111,255,.4) !important;
    border-radius:13px !important; padding:1.6rem !important;
    transition:all .3s !important;
}
[data-testid="stFileUploaderDropzone"]:hover {
    border-color:rgba(124,111,255,.8) !important;
    background:rgba(124,111,255,.09) !important;
}

/* Download button */
[data-testid="stDownloadButton"] > button {
    background:linear-gradient(135deg,#00e676,#00c853) !important;
    color:#000 !important; font-weight:800 !important;
    font-size:.93rem !important; padding:.65rem 1.8rem !important;
    border-radius:10px !important; border:none !important; width:100% !important;
    box-shadow:0 4px 20px rgba(0,230,118,.3) !important;
    transition:transform .15s,box-shadow .15s !important;
}
[data-testid="stDownloadButton"] > button:hover {
    transform:translateY(-2px) !important;
    box-shadow:0 8px 28px rgba(0,230,118,.45) !important;
}

/* Primary button */
.stButton > button[kind="primary"] {
    background:linear-gradient(135deg,#7c6fff,#00c8f8) !important;
    color:#fff !important; font-weight:800 !important;
    font-size:.93rem !important; padding:.65rem 1.8rem !important;
    border-radius:10px !important; border:none !important; width:100% !important;
    box-shadow:0 4px 20px rgba(124,111,255,.3) !important;
}
.stButton > button[kind="primary"]:hover {
    transform:translateY(-2px) !important;
    box-shadow:0 8px 28px rgba(124,111,255,.45) !important;
}

/* Secondary button */
.stButton > button[kind="secondary"] {
    background:rgba(26,26,64,.8) !important; color:#dde0ff !important;
    font-weight:700 !important; border:1px solid rgba(44,44,96,.9) !important;
    border-radius:10px !important; width:100% !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color:rgba(124,111,255,.6) !important;
}

/* Selectbox */
[data-testid="stSelectbox"] > div > div {
    background:rgba(26,26,64,.9) !important;
    border:2px solid rgba(44,44,96,.9) !important;
    border-radius:10px !important; color:#dde0ff !important;
}

/* Misc */
audio { border-radius:10px; width:100%; margin-top:.35rem; }
#MainMenu, footer, header { visibility:hidden !important; }
[data-testid="stExpander"] {
    background:rgba(26,26,64,.5) !important;
    border:1px solid rgba(44,44,96,.7) !important;
    border-radius:12px !important;
}
</style>
"""

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN UI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    st.markdown(CSS, unsafe_allow_html=True)

    # ── Hero ──────────────────────────────────────────────────────────────────
    st.markdown("""
    <div class="hero">
      <span class="hero-icon">🎬</span>
      <h1>MP4 → MP3 Converter Pro</h1>
      <p class="hero-sub">
        Stream-process files up to 10 GB &nbsp;·&nbsp;
        Source video never stored &nbsp;·&nbsp;
        Powered by FFmpeg
      </p>
    </div>
    """, unsafe_allow_html=True)

    # ── Status pills ──────────────────────────────────────────────────────────
    ffmpeg_ok, _ffmpeg_msg = check_ffmpeg()
    disk_gb = get_disk_gb()
    ram_gb  = get_ram_gb()

    p_ffmpeg = (
        '<span class="pill p-green">✅ ffmpeg ready</span>'
        if ffmpeg_ok else
        '<span class="pill p-red">❌ ffmpeg missing</span>'
    )
    p_disk = ""
    if disk_gb >= 0:
        cls = "p-green" if disk_gb > 5 else "p-amber" if disk_gb > 1 else "p-red"
        p_disk = f'<span class="pill {cls}">💾 {disk_gb:.1f} GB free</span>'

    p_ram = ""
    if ram_gb >= 0:
        cls = "p-green" if ram_gb > 2 else "p-amber" if ram_gb > 0.5 else "p-red"
        p_ram = f'<span class="pill {cls}">🧠 {ram_gb:.1f} GB RAM</span>'

    st.markdown(
        f'<div class="pill-row">{p_ffmpeg}{p_disk}{p_ram}</div>',
        unsafe_allow_html=True,
    )

    if not ffmpeg_ok:
        st.error(
            "**ffmpeg not found.**\n\n"
            "Make sure `packages.txt` exists in your repo root "
            "and contains one line: `ffmpeg`\n\n"
            "Then redeploy the app."
        )
        st.stop()

    # ── Upload ────────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="card"><h4>📁 Step 1 — Upload Your Video</h4></div>',
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        "Drop a video or click to browse",
        type=ALLOWED_TYPES,
        label_visibility="collapsed",
        help="MP4 AVI MKV MOV WMV FLV WEBM M4V TS  |  Max 10 GB",
    )

    # Reset state when a new file is selected
    if uploaded is not None:
        if (st.session_state.input_name    != uploaded.name
                or st.session_state.file_size_bytes != uploaded.size):
            reset_state()
            st.session_state.input_name      = uploaded.name
            st.session_state.file_size_bytes = uploaded.size

        ext = Path(uploaded.name).suffix.upper().lstrip(".")
        st.markdown(
            f'<div class="pill-row">'
            f'<span class="pill p-blue">🎞️ {ext}</span>'
            f'<span class="pill p-purple">📄 {uploaded.name}</span>'
            f'<span class="pill p-amber">⚖️ {fmt_bytes(uploaded.size)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Settings ──────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="card"><h4>⚙️ Step 2 — Audio Settings</h4></div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        br_label = st.selectbox("🎵 Bitrate",     list(BITRATE_OPTIONS),     index=0)
    with c2:
        sr_label = st.selectbox("📊 Sample Rate", list(SAMPLERATE_OPTIONS),  index=1)
    with c3:
        ch_label = st.selectbox("🎤 Channels",    list(CHANNEL_OPTIONS),     index=0)

    bitrate     = BITRATE_OPTIONS[br_label]
    sample_rate = SAMPLERATE_OPTIONS[sr_label]
    channels    = CHANNEL_OPTIONS[ch_label]

    # ── Convert button ────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)

    can_convert = (
        uploaded is not None
        and not st.session_state.converting
        and not st.session_state.conversion_done
    )

    btn_txt = "⚙️  Converting…" if st.session_state.converting else "🚀  Convert to MP3"

    if st.button(btn_txt, type="primary", disabled=not can_convert,
                 use_container_width=True):

        # ── Start conversion ──────────────────────────────────────────────────
        st.session_state.converting       = True
        st.session_state.conversion_done  = False
        st.session_state.conversion_error = None

        session_dir  = get_session_dir()
        stem         = Path(uploaded.name).stem
        output_path  = session_dir / (stem + ".mp3")
        out_filename = stem + ".mp3"

        cancel_ev = threading.Event()
        prog_bar  = st.progress(0.0, text="Starting…")
        t0        = time.time()

        def on_progress(frac):
            elapsed   = time.time() - t0
            pct       = int(frac * 100)
            done_b    = int(st.session_state.file_size_bytes * frac)
            speed     = done_b / elapsed if elapsed > 0.1 else 0
            total_b   = int(st.session_state.file_size_bytes)
            remaining = (total_b - done_b) / speed if speed > 10 else 0
            eta_str   = f"  |  ETA {fmt_time(remaining)}" if remaining > 1 else ""
            prog_bar.progress(
                frac,
                text=f"Converting… {pct}%  |  "
                     f"{fmt_bytes(done_b)} / {fmt_bytes(total_b)}{eta_str}",
            )

        success, err_msg = run_conversion(
            uploaded_file = uploaded,
            output_path   = output_path,
            bitrate       = bitrate,
            sample_rate   = sample_rate,
            channels      = channels,
            progress_cb   = on_progress,
            cancel_ev     = cancel_ev,
        )

        elapsed = time.time() - t0
        prog_bar.empty()

        if success:
            st.session_state.conversion_done   = True
            st.session_state.converted_path    = str(output_path)
            st.session_state.output_filename   = out_filename
            st.session_state.conversion_time   = elapsed
            st.session_state.output_size_bytes = output_path.stat().st_size
        else:
            st.session_state.conversion_error  = err_msg

        st.session_state.converting = False
        st.rerun()

    # ── Download section ──────────────────────────────────────────────────────
    if st.session_state.conversion_done and st.session_state.converted_path:
        out_path = Path(st.session_state.converted_path)

        if out_path.exists():
            out_sz  = st.session_state.output_size_bytes
            in_sz   = st.session_state.file_size_bytes
            ratio   = (out_sz / max(1, in_sz)) * 100
            elapsed = st.session_state.conversion_time

            st.markdown(f"""
            <div class="banner b-ok">
              <span class="b-icon">✅</span>
              <div>
                <div class="b-title">Conversion Complete!</div>
                <div class="b-body">
                  ⏱ {fmt_time(elapsed)} &nbsp;·&nbsp;
                  📦 {fmt_bytes(out_sz)}
                  ({ratio:.1f}% of original) &nbsp;·&nbsp;
                  🎵 {br_label.split('—')[0].strip()}
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            # Read converted file for download widget
            with open(out_path, "rb") as fh:
                mp3_data = fh.read()

            st.download_button(
                label               = "⬇️  Download MP3",
                data                = mp3_data,
                file_name           = st.session_state.output_filename,
                mime                = "audio/mpeg",
                use_container_width = True,
                on_click            = reset_state,
            )

            st.markdown("**🔊 Preview:**")
            st.audio(mp3_data, format="audio/mpeg")

            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄  Convert Another File",
                         use_container_width=True, type="secondary"):
                reset_state()
                st.rerun()

        else:
            st.error("Temp file missing — please convert again.")
            reset_state()

    # ── Error section ─────────────────────────────────────────────────────────
    elif st.session_state.conversion_error:
        st.markdown(f"""
        <div class="banner b-err">
          <span class="b-icon">❌</span>
          <div>
            <div class="b-title">Conversion Failed</div>
            <div class="b-body">{st.session_state.conversion_error}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        if st.button("🔄  Try Again", use_container_width=True, type="secondary"):
            st.session_state.conversion_error = None
            st.session_state.conversion_done  = False
            st.rerun()

    # ── Info expander ─────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("ℹ️  How it works"):
        st.markdown("""
| | |
|---|---|
| **Max file size** | 10 GB (via `.streamlit/config.toml`) |
| **Video stored?** | ❌ Never — piped directly into ffmpeg stdin |
| **Temp files** | Only the output MP3 is written to disk |
| **Cleanup** | Auto-deleted on Download or Convert Another |
| **Stream chunk** | 4 MB per write — RAM stays low for large files |
| **Formats** | MP4 · AVI · MKV · MOV · WMV · FLV · WEBM · M4V · TS |
        """)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
