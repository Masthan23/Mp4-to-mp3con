# app.py
# streamlit run app.py
# Deploy: push to GitHub + connect to share.streamlit.io
# Required files in repo root:
#   packages.txt     → contains: ffmpeg
#   requirements.txt → contains: streamlit>=1.32.0 / psutil>=5.9.0
#   .streamlit/config.toml

import streamlit as st
import subprocess
import tempfile
import os
import io
import time
import threading
import uuid
import shutil
from pathlib import Path

# ── Try importing psutil gracefully ──────────────────────────────────────────
try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  PAGE CONFIG  — must be the FIRST Streamlit call
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title  = "MP4 → MP3 Converter Pro",
    page_icon   = "🎬",
    layout      = "centered",
    initial_sidebar_state = "collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
CHUNK_SIZE = 4 * 1024 * 1024      # 4 MB per pipe-write chunk

# Temp root — use system temp, always available on cloud
TMP_ROOT = Path(tempfile.gettempdir()) / "mp4_to_mp3_conv"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

BITRATE_OPTIONS = {
    "320 kbps  — Maximum quality" : "320k",
    "256 kbps  — High quality"    : "256k",
    "192 kbps  — Standard"        : "192k",
    "128 kbps  — Compact"         : "128k",
    "96 kbps   — Voice/podcast"   : "96k",
}
SAMPLERATE_OPTIONS = {
    "48 000 Hz — Studio/video"    : "48000",
    "44 100 Hz — CD quality"      : "44100",
    "22 050 Hz — Compressed"      : "22050",
}
CHANNEL_OPTIONS = {
    "Stereo  (2 ch)"              : "2",
    "Mono    (1 ch)"              : "1",
}
ALLOWED_TYPES = ["mp4", "avi", "mkv", "mov", "wmv", "flv", "webm", "m4v", "ts"]

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION STATE  — safe bootstrap (no type-hint syntax that breaks Py 3.9)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = {
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

for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# Assign a session ID once
if st.session_state.session_id is None:
    st.session_state.session_id = str(uuid.uuid4())

# ─────────────────────────────────────────────────────────────────────────────
#  TEMP DIRECTORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_session_dir():
    """Return this session's isolated temp folder."""
    d = TMP_ROOT / st.session_state.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_session():
    """Delete converted MP3 temp file and reset state."""
    d = TMP_ROOT / st.session_state.session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    for k, v in _DEFAULTS.items():
        st.session_state[k] = v
    # Re-assign a fresh session id so next run gets a clean dir
    st.session_state.session_id = str(uuid.uuid4())

# ─────────────────────────────────────────────────────────────────────────────
#  SYSTEM CHECKS  — called inside main(), NOT at module level
# ─────────────────────────────────────────────────────────────────────────────
def check_ffmpeg():
    """Return (ok: bool, message: str). Safe to call at any time."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            first_line = (r.stdout or "").splitlines()[0] if r.stdout else "ffmpeg"
            return True, first_line
        return False, "ffmpeg exited with non-zero code"
    except FileNotFoundError:
        return False, "ffmpeg binary not found in PATH"
    except Exception as exc:
        return False, str(exc)


def get_disk_free_gb():
    if PSUTIL_OK:
        try:
            return psutil.disk_usage(str(TMP_ROOT)).free / (1024 ** 3)
        except Exception:
            pass
    return -1.0


def get_ram_free_gb():
    if PSUTIL_OK:
        try:
            return psutil.virtual_memory().available / (1024 ** 3)
        except Exception:
            pass
    return -1.0

# ─────────────────────────────────────────────────────────────────────────────
#  CORE CONVERTER  — pipes upload bytes → ffmpeg stdin → mp3 on disk
#  Source video is NEVER written to disk.
# ─────────────────────────────────────────────────────────────────────────────
def convert_to_mp3(uploaded_file, output_path, bitrate, sample_rate,
                   channels, progress_cb, cancel_event):
    """
    Streams uploaded_file → ffmpeg pipe → output_path (MP3).
    Returns (success: bool, error_message: str).
    """
    cmd = [
        "ffmpeg", "-y",
        "-i",       "pipe:0",       # read video from stdin
        "-vn",                      # discard video track
        "-acodec",  "libmp3lame",
        "-b:a",     bitrate,
        "-ar",      sample_rate,
        "-ac",      channels,
        "-q:a",     "0",            # best VBR quality
        str(output_path),
    ]

    # ── Launch ffmpeg ─────────────────────────────────────────────────────────
    try:
        proc = subprocess.Popen(
            cmd,
            stdin  = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg not found. Is it installed? (packages.txt must contain 'ffmpeg')"
    except Exception as exc:
        return False, "Could not start ffmpeg: " + str(exc)

    total_bytes = st.session_state.file_size_bytes
    sent        = [0]
    write_err   = [None]

    # ── Writer thread: push chunks into ffmpeg's stdin ───────────────────────
    def _writer():
        try:
            uploaded_file.seek(0)
            while not cancel_event.is_set():
                chunk = uploaded_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                sent[0] += len(chunk)
                if total_bytes > 0:
                    progress_cb(min(0.93, sent[0] / total_bytes))
        except BrokenPipeError:
            pass   # ffmpeg closed early — returncode will tell us why
        except Exception as exc:
            write_err[0] = str(exc)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_writer, daemon=True)
    t.start()

    # ── Wait for ffmpeg ───────────────────────────────────────────────────────
    _stdout, stderr_bytes = proc.communicate()
    t.join(timeout=15)

    if cancel_event.is_set():
        try:
            proc.kill()
        except Exception:
            pass
        return False, "Cancelled."

    if write_err[0]:
        return False, "Pipe write error: " + write_err[0]

    if proc.returncode != 0:
        stderr_str = stderr_bytes.decode(errors="replace").strip()
        lines      = [l for l in stderr_str.splitlines() if l.strip()]
        short_err  = lines[-1] if lines else "Unknown ffmpeg error"
        return False, f"ffmpeg failed (code {proc.returncode}): {short_err}"

    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "ffmpeg produced no output. The file may have no audio track."

    progress_cb(1.0)
    return True, ""

# ─────────────────────────────────────────────────────────────────────────────
#  FORMATTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def fmt_bytes(n):
    n = int(n)
    if n < 1024:            return f"{n} B"
    if n < 1_048_576:       return f"{n / 1024:.1f} KB"
    if n < 1_073_741_824:   return f"{n / 1_048_576:.2f} MB"
    return f"{n / 1_073_741_824:.2f} GB"


def fmt_time(s):
    s = float(s)
    if s < 60:
        return f"{s:.1f} s"
    m = int(s // 60)
    return f"{m}m {s % 60:.0f}s"

# ─────────────────────────────────────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────────────────────────────────────
def inject_css():
    st.markdown("""
    <style>
    /* ── Global reset ─────────────────────────────────────── */
    html, body, [class*="css"] {
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif !important;
    }
    .stApp {
        background: linear-gradient(135deg,#0f0f1a 0%,#12122a 60%,#0f1a2a 100%);
        min-height: 100vh;
    }
    .block-container { max-width: 760px !important; padding-top: 1.5rem !important; }

    /* ── Hero ─────────────────────────────────────────────── */
    .hero { text-align:center; padding:2rem 1rem 1.2rem; margin-bottom:1rem; }
    .hero-icon {
        font-size:3.8rem; display:block; margin-bottom:.5rem;
        animation: float 3s ease-in-out infinite;
    }
    @keyframes float {
        0%,100% { transform:translateY(0); }
        50%      { transform:translateY(-10px); }
    }
    .hero h1 {
        font-size: clamp(1.3rem,4vw,2rem);
        font-weight: 900;
        background: linear-gradient(135deg,#dde0ff 0%,#9d95ff 55%,#00c8f8 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        margin: 0 0 .4rem;
    }
    .hero-sub { color:#6868a0; font-size:.85rem; margin:0; }

    /* ── Stat pills ───────────────────────────────────────── */
    .pill {
        display:inline-flex; align-items:center; gap:.35rem;
        border-radius:99px; padding:.28rem .85rem;
        font-size:.72rem; font-weight:700; border:1px solid;
        white-space:nowrap;
    }
    .pill-purple {
        background:rgba(124,111,255,.1); border-color:rgba(124,111,255,.3);
        color:#9d95ff;
    }
    .pill-green {
        background:rgba(0,230,118,.08); border-color:rgba(0,230,118,.3);
        color:#00e676;
    }
    .pill-red {
        background:rgba(255,92,122,.08); border-color:rgba(255,92,122,.3);
        color:#ff5c7a;
    }
    .pill-amber {
        background:rgba(255,179,0,.08); border-color:rgba(255,179,0,.3);
        color:#ffb300;
    }
    .pill-blue {
        background:rgba(0,200,248,.08); border-color:rgba(0,200,248,.3);
        color:#00c8f8;
    }
    .pill-row { display:flex; gap:.5rem; flex-wrap:wrap; margin:.6rem 0 1rem; }

    /* ── Info card ────────────────────────────────────────── */
    .info-card {
        background:rgba(26,26,64,.75); border:1px solid rgba(44,44,96,.9);
        border-radius:13px; padding:1.1rem 1.4rem; margin:.8rem 0;
        backdrop-filter:blur(6px);
    }
    .info-card h4 {
        font-size:.72rem; font-weight:700; color:#6868a0;
        text-transform:uppercase; letter-spacing:.8px; margin:0 0 .75rem;
    }

    /* ── Banners ──────────────────────────────────────────── */
    .banner {
        border-radius:12px; padding:1rem 1.2rem;
        margin:.8rem 0; display:flex; align-items:flex-start; gap:.7rem;
    }
    .banner.ok  { background:rgba(0,230,118,.07); border:1px solid rgba(0,230,118,.35); }
    .banner.err { background:rgba(255,92,122,.07); border:1px solid rgba(255,92,122,.35); }
    .b-icon     { font-size:1.4rem; flex-shrink:0; margin-top:.05rem; }
    .b-title    { font-weight:800; font-size:.9rem; margin-bottom:.2rem; }
    .b-body     { font-size:.78rem; color:#a0a0c8; line-height:1.55; }

    /* ── Progress ─────────────────────────────────────────── */
    .stProgress > div > div {
        background:linear-gradient(90deg,#7c6fff,#00c8f8) !important;
        border-radius:99px !important;
    }
    .stProgress > div {
        background:rgba(26,26,56,.8) !important;
        border-radius:99px !important;
        height:10px !important;
    }

    /* ── Upload drop zone ─────────────────────────────────── */
    [data-testid="stFileUploaderDropzone"] {
        background:rgba(124,111,255,.04) !important;
        border:2px dashed rgba(124,111,255,.4) !important;
        border-radius:13px !important;
        padding:1.8rem !important;
        transition:all .3s !important;
    }
    [data-testid="stFileUploaderDropzone"]:hover {
        border-color:rgba(124,111,255,.8) !important;
        background:rgba(124,111,255,.09) !important;
    }

    /* ── Download button ──────────────────────────────────── */
    [data-testid="stDownloadButton"] > button {
        background:linear-gradient(135deg,#00e676,#00c853) !important;
        color:#000 !important; font-weight:800 !important;
        font-size:.95rem !important; padding:.7rem 2rem !important;
        border-radius:10px !important; border:none !important;
        width:100% !important;
        box-shadow:0 4px 20px rgba(0,230,118,.3) !important;
        transition:transform .15s, box-shadow .15s !important;
    }
    [data-testid="stDownloadButton"] > button:hover {
        transform:translateY(-2px) !important;
        box-shadow:0 8px 28px rgba(0,230,118,.45) !important;
    }

    /* ── Primary button (Convert) ─────────────────────────── */
    .stButton > button[kind="primary"] {
        background:linear-gradient(135deg,#7c6fff,#00c8f8) !important;
        color:#fff !important; font-weight:800 !important;
        font-size:.95rem !important; padding:.7rem 2rem !important;
        border-radius:10px !important; border:none !important;
        width:100% !important;
        box-shadow:0 4px 20px rgba(124,111,255,.3) !important;
        transition:transform .15s, box-shadow .15s !important;
    }
    .stButton > button[kind="primary"]:hover {
        transform:translateY(-2px) !important;
        box-shadow:0 8px 28px rgba(124,111,255,.45) !important;
    }

    /* ── Secondary button ─────────────────────────────────── */
    .stButton > button[kind="secondary"] {
        background:rgba(26,26,64,.8) !important;
        color:#dde0ff !important; font-weight:700 !important;
        border:1px solid rgba(44,44,96,.9) !important;
        border-radius:10px !important; width:100% !important;
        transition:border-color .2s !important;
    }
    .stButton > button[kind="secondary"]:hover {
        border-color:rgba(124,111,255,.6) !important;
    }

    /* ── Select boxes ─────────────────────────────────────── */
    [data-testid="stSelectbox"] > div > div {
        background:rgba(26,26,64,.9) !important;
        border:2px solid rgba(44,44,96,.9) !important;
        border-radius:10px !important; color:#dde0ff !important;
    }

    /* ── Audio player ─────────────────────────────────────── */
    audio { border-radius:10px; width:100%; margin-top:.4rem; }

    /* ── Hide default chrome ──────────────────────────────── */
    #MainMenu, footer, header { visibility:hidden !important; }

    /* ── Expander ─────────────────────────────────────────── */
    [data-testid="stExpander"] {
        background:rgba(26,26,64,.5) !important;
        border:1px solid rgba(44,44,96,.7) !important;
        border-radius:12px !important;
    }
    </style>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN  — everything inside main() so nothing runs at import time
# ─────────────────────────────────────────────────────────────────────────────
def main():
    inject_css()

    # ── Hero header ───────────────────────────────────────────────────────────
    st.markdown("""
    <div class="hero">
      <span class="hero-icon">🎬</span>
      <h1>MP4 → MP3 Converter Pro</h1>
      <p class="hero-sub">
        Stream-process videos up to 10 GB &nbsp;·&nbsp;
        Zero video storage &nbsp;·&nbsp;
        Powered by FFmpeg
      </p>
    </div>
    """, unsafe_allow_html=True)

    # ── System status ─────────────────────────────────────────────────────────
    ffmpeg_ok, ffmpeg_msg = check_ffmpeg()
    disk_gb = get_disk_free_gb()
    ram_gb  = get_ram_free_gb()

    pills_html = '<div class="pill-row">'
    pills_html += (
        '<span class="pill pill-green">✅ ffmpeg ready</span>'
        if ffmpeg_ok else
        '<span class="pill pill-red">❌ ffmpeg missing</span>'
    )
    if disk_gb >= 0:
        disk_cls = "green" if disk_gb > 5 else "amber" if disk_gb > 1 else "red"
        pills_html += f'<span class="pill pill-{disk_cls}">💾 {disk_gb:.1f} GB disk free</span>'
    if ram_gb >= 0:
        ram_cls = "green" if ram_gb > 2 else "amber" if ram_gb > 0.5 else "red"
        pills_html += f'<span class="pill pill-{ram_cls}">🧠 {ram_gb:.1f} GB RAM free</span>'
    pills_html += '</div>'
    st.markdown(pills_html, unsafe_allow_html=True)

    # Hard stop if ffmpeg missing
    if not ffmpeg_ok:
        st.error(
            f"**ffmpeg not found!** `{ffmpeg_msg}`\n\n"
            "Add a file called **`packages.txt`** to your repository root "
            "containing exactly one line:\n```\nffmpeg\n```\n"
            "Then redeploy the app."
        )
        st.stop()

    # ── File uploader ─────────────────────────────────────────────────────────
    st.markdown(
        '<div class="info-card"><h4>📁 Step 1 — Select Your Video File</h4></div>',
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        label="Drop a video or click Browse",
        type=ALLOWED_TYPES,
        help=(
            "MP4, AVI, MKV, MOV, WMV, FLV, WEBM, M4V, TS  ·  Max 10 GB  ·  "
            "Source video is NEVER saved to disk"
        ),
        label_visibility="collapsed",
    )

    # Detect a new file → reset previous conversion state
    if uploaded is not None:
        if (
            st.session_state.input_name     != uploaded.name
            or st.session_state.file_size_bytes != uploaded.size
        ):
            cleanup_session()
            st.session_state.input_name        = uploaded.name
            st.session_state.file_size_bytes   = uploaded.size

        ext = Path(uploaded.name).suffix.upper().lstrip(".")
        st.markdown(
            f'<div class="pill-row">'
            f'<span class="pill pill-blue">🎞️ {ext}</span>'
            f'<span class="pill pill-purple">📄 {uploaded.name}</span>'
            f'<span class="pill pill-amber">⚖️ {fmt_bytes(uploaded.size)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Quality settings ──────────────────────────────────────────────────────
    st.markdown(
        '<div class="info-card"><h4>⚙️ Step 2 — Audio Quality Settings</h4></div>',
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        br_label = st.selectbox(
            "🎵 Bitrate",
            list(BITRATE_OPTIONS.keys()),
            index=0,
        )
    with col2:
        sr_label = st.selectbox(
            "📊 Sample Rate",
            list(SAMPLERATE_OPTIONS.keys()),
            index=1,
        )
    with col3:
        ch_label = st.selectbox(
            "🎤 Channels",
            list(CHANNEL_OPTIONS.keys()),
            index=0,
        )

    bitrate     = BITRATE_OPTIONS[br_label]
    sample_rate = SAMPLERATE_OPTIONS[sr_label]
    channels    = CHANNEL_OPTIONS[ch_label]

    # ── Convert button ────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)

    btn_label    = "⚙️  Converting…" if st.session_state.converting else "🚀  Convert to MP3"
    btn_disabled = (
        uploaded is None
        or st.session_state.converting
        or st.session_state.conversion_done
    )

    if st.button(btn_label, type="primary", disabled=btn_disabled, use_container_width=True):

        if uploaded is None:
            st.warning("Please upload a video file first.")
            st.stop()

        # ── Mark as converting ────────────────────────────────────────────────
        st.session_state.converting       = True
        st.session_state.conversion_done  = False
        st.session_state.conversion_error = None

        # Build output path (only the MP3 touches disk)
        session_dir  = get_session_dir()
        stem         = Path(uploaded.name).stem
        output_path  = session_dir / (stem + ".mp3")
        out_filename = stem + ".mp3"

        cancel_event = threading.Event()

        # Live progress bar
        prog_bar = st.progress(0.0, text="Starting…")
        eta_slot = st.empty()
        t_start  = time.time()

        def _on_progress(frac):
            elapsed  = time.time() - t_start
            pct      = int(frac * 100)
            done_b   = int(st.session_state.file_size_bytes * frac)
            speed    = done_b / elapsed if elapsed > 0 else 0
            remaining = (
                (st.session_state.file_size_bytes - done_b) / speed
                if speed > 0 else 0
            )
            prog_bar.progress(
                frac,
                text=(
                    f"Converting… {pct}%  |  "
                    f"{fmt_bytes(done_b)} / {fmt_bytes(st.session_state.file_size_bytes)}"
                    + (f"  |  ETA {fmt_time(remaining)}" if remaining > 1 else "")
                ),
            )

        # ── Run ffmpeg ────────────────────────────────────────────────────────
        success, err_msg = convert_to_mp3(
            uploaded_file = uploaded,
            output_path   = output_path,
            bitrate       = bitrate,
            sample_rate   = sample_rate,
            channels      = channels,
            progress_cb   = _on_progress,
            cancel_event  = cancel_event,
        )

        elapsed = time.time() - t_start
        prog_bar.empty()
        eta_slot.empty()

        # ── Store results in session state ────────────────────────────────────
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

    # ── Success: show download ────────────────────────────────────────────────
    if st.session_state.conversion_done and st.session_state.converted_path:
        out_path = Path(st.session_state.converted_path)

        if out_path.exists():
            out_sz  = st.session_state.output_size_bytes
            in_sz   = st.session_state.file_size_bytes
            ratio   = (out_sz / max(1, in_sz)) * 100
            elapsed = st.session_state.conversion_time

            st.markdown(f"""
            <div class="banner ok">
              <span class="b-icon">✅</span>
              <div>
                <div class="b-title">Conversion Complete!</div>
                <div class="b-body">
                  ⏱ {fmt_time(elapsed)} &nbsp;·&nbsp;
                  📦 {fmt_bytes(out_sz)} output
                  &nbsp;({ratio:.1f}% of original) &nbsp;·&nbsp;
                  🎵 {br_label.split('—')[0].strip()}
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            # Read bytes for the download widget
            with open(out_path, "rb") as fh:
                mp3_data = fh.read()

            st.download_button(
                label        = "⬇️  Download MP3",
                data         = mp3_data,
                file_name    = st.session_state.output_filename,
                mime         = "audio/mpeg",
                use_container_width = True,
                on_click     = cleanup_session,   # auto-delete after download click
            )

            # In-browser preview
            st.markdown("**🔊 Preview:**")
            st.audio(mp3_data, format="audio/mpeg")

            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄  Convert Another File",
                         use_container_width=True, type="secondary"):
                cleanup_session()
                st.rerun()

        else:
            # File disappeared (server restart, etc.)
            st.error("Temp file is gone — please convert again.")
            cleanup_session()

    # ── Error ─────────────────────────────────────────────────────────────────
    elif st.session_state.conversion_error:
        st.markdown(f"""
        <div class="banner err">
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
    with st.expander("ℹ️  How it works / Tips"):
        st.markdown("""
| Topic | Detail |
|---|---|
| **Max upload** | 10 GB — set in `.streamlit/config.toml` (`maxUploadSize = 10240`) |
| **Video storage** | ❌ Source video is **never written to disk** |
| **Streaming** | Uploaded bytes are piped in 4 MB chunks directly into `ffmpeg stdin` |
| **Temp files** | Only the output MP3 is stored in a session-isolated temp folder |
| **Auto-cleanup** | Temp MP3 is deleted when you click **Download** or **Convert Another** |
| **Formats** | MP4 · AVI · MKV · MOV · WMV · FLV · WEBM · M4V · TS |
| **ffmpeg flags** | `-vn` (drop video) · `-acodec libmp3lame` · `-q:a 0` (best VBR) |
| **Cloud deploy** | Needs `packages.txt` containing `ffmpeg` in the repo root |
        """)

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
