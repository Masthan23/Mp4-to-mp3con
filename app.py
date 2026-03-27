# app.py  — run:  streamlit run app.py --server.maxUploadSize=10240

import streamlit as st
import subprocess
import tempfile
import os
import io
import time
import threading
import uuid
import psutil
import shutil
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────
#  PAGE CONFIG  (must be first Streamlit call)
# ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MP4 → MP3 Converter Pro",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────
MAX_UPLOAD_MB   = 10_240        # 10 GB (set in .streamlit/config.toml too)
CHUNK_SIZE      = 4 * 1024 * 1024   # 4 MB read/write chunks
TMP_ROOT        = Path(tempfile.gettempdir()) / "mp4_to_mp3_sessions"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

BITRATE_OPTIONS = {
    "320 kbps  – Lossless quality" : "320k",
    "256 kbps  – High quality"     : "256k",
    "192 kbps  – Standard"         : "192k",
    "128 kbps  – Compact"          : "128k",
    "96 kbps   – Voice / podcast"  : "96k",
}
SAMPLERATE_OPTIONS = {
    "48 000 Hz – Studio / video"   : "48000",
    "44 100 Hz – CD quality"       : "44100",
    "22 050 Hz – Compressed"       : "22050",
}
CHANNEL_OPTIONS = {
    "Stereo (2 channels)"          : "2",
    "Mono   (1 channel)"           : "1",
}
ALLOWED_EXT = {
    ".mp4", ".avi", ".mkv", ".mov",
    ".wmv", ".flv", ".webm", ".m4v", ".ts",
}

# ─────────────────────────────────────────────────────────
#  SESSION STATE BOOTSTRAP
# ─────────────────────────────────────────────────────────
def _init_state():
    defaults = {
        "session_id"        : str(uuid.uuid4()),
        "converted_path"    : None,   # Path to the temp MP3 output
        "output_filename"   : None,   # Suggested download name
        "file_size_bytes"   : 0,
        "conversion_done"   : False,
        "conversion_error"  : None,
        "converting"        : False,
        "input_name"        : None,
        "conversion_time"   : 0.0,
        "output_size_bytes" : 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ─────────────────────────────────────────────────────────
#  SESSION TEMP DIRECTORY  (isolated per user)
# ─────────────────────────────────────────────────────────
def get_session_dir() -> Path:
    d = TMP_ROOT / st.session_state.session_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def cleanup_session():
    """Delete everything in this session's temp folder."""
    d = TMP_ROOT / st.session_state.session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    # Reset state
    st.session_state.converted_path    = None
    st.session_state.output_filename   = None
    st.session_state.conversion_done   = False
    st.session_state.conversion_error  = None
    st.session_state.converting        = False
    st.session_state.input_name        = None
    st.session_state.conversion_time   = 0.0
    st.session_state.output_size_bytes = 0

# ─────────────────────────────────────────────────────────
#  SYSTEM CHECKS
# ─────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def check_ffmpeg() -> tuple[bool, str]:
    """Returns (available, version_string)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            ver = r.stdout.splitlines()[0] if r.stdout else "ffmpeg"
            return True, ver
        return False, "ffmpeg returned non-zero"
    except FileNotFoundError:
        return False, "ffmpeg not found in PATH"
    except Exception as e:
        return False, str(e)

def get_disk_free_gb() -> float:
    usage = psutil.disk_usage(str(TMP_ROOT))
    return usage.free / (1024 ** 3)

def get_ram_free_gb() -> float:
    return psutil.virtual_memory().available / (1024 ** 3)

# ─────────────────────────────────────────────────────────
#  CORE CONVERTER  — streams via FFmpeg pipe
# ─────────────────────────────────────────────────────────
def convert_stream_to_mp3(
    uploaded_file,          # st.UploadedFile
    output_path: Path,
    bitrate    : str,
    sample_rate: str,
    channels   : str,
    progress_cb,            # callable(float 0..1)
    cancel_event: threading.Event,
) -> tuple[bool, str]:
    """
    Pipes the uploaded file bytes → ffmpeg stdin → mp3 output_path.
    Never writes the source video to disk.

    Returns (success, error_message).
    """
    cmd = [
        "ffmpeg", "-y",
        "-i",      "pipe:0",        # read from stdin
        "-vn",                      # drop video stream
        "-acodec", "libmp3lame",
        "-b:a",    bitrate,
        "-ar",     sample_rate,
        "-ac",     channels,
        "-q:a",    "0",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "ffmpeg not found. Install ffmpeg first."
    except Exception as e:
        return False, f"Failed to start ffmpeg: {e}"

    # ── Writer thread: chunk the upload into ffmpeg stdin ──
    total_size  = st.session_state.file_size_bytes
    bytes_sent  = [0]
    write_error = [None]

    def _write_stdin():
        try:
            uploaded_file.seek(0)
            while True:
                if cancel_event.is_set():
                    break
                chunk = uploaded_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                bytes_sent[0] += len(chunk)
                if total_size > 0:
                    progress_cb(
                        min(0.95, bytes_sent[0] / total_size)
                    )
        except BrokenPipeError:
            pass  # ffmpeg closed stdin (error handled via returncode)
        except Exception as e:
            write_error[0] = str(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    writer = threading.Thread(target=_write_stdin, daemon=True)
    writer.start()

    # ── Wait for ffmpeg to finish ──
    stdout_data, stderr_data = proc.communicate()
    writer.join(timeout=10)

    if cancel_event.is_set():
        proc.kill()
        return False, "Cancelled by user."

    if write_error[0]:
        return False, f"Write error: {write_error[0]}"

    if proc.returncode != 0:
        err = stderr_data.decode(errors="replace").strip()
        # Extract the last meaningful ffmpeg error line
        lines = [l for l in err.splitlines() if l.strip()]
        short = lines[-1] if lines else "Unknown ffmpeg error"
        return False, f"ffmpeg error (code {proc.returncode}): {short}"

    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "Output file is empty — conversion may have failed."

    progress_cb(1.0)
    return True, ""

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    if n < 1024:         return f"{n} B"
    if n < 1_048_576:    return f"{n/1024:.1f} KB"
    if n < 1_073_741_824:return f"{n/1_048_576:.2f} MB"
    return f"{n/1_073_741_824:.2f} GB"

def fmt_time(s: float) -> str:
    if s < 60:   return f"{s:.1f}s"
    m = int(s // 60)
    return f"{m}m {s % 60:.0f}s"

def ext_ok(name: str) -> bool:
    return Path(name).suffix.lower() in ALLOWED_EXT

# ─────────────────────────────────────────────────────────
#  CSS INJECTION
# ─────────────────────────────────────────────────────────
def inject_css():
    st.markdown("""
    <style>
    /* ── Global ─────────────────────────────────────────── */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', 'Segoe UI', system-ui, sans-serif !important;
    }
    .stApp {
        background: linear-gradient(135deg, #0f0f1a 0%, #12122a 50%, #0f1a2a 100%);
        min-height: 100vh;
    }
    .block-container {
        max-width: 800px !important;
        padding-top: 2rem !important;
    }

    /* ── Hero header ─────────────────────────────────────── */
    .hero {
        text-align: center;
        padding: 2.5rem 1.5rem 1.5rem;
        margin-bottom: 1.5rem;
    }
    .hero-icon {
        font-size: 4rem;
        display: block;
        margin-bottom: .6rem;
        animation: float 3s ease-in-out infinite;
    }
    @keyframes float {
        0%,100% { transform: translateY(0); }
        50%      { transform: translateY(-10px); }
    }
    .hero h1 {
        font-size: clamp(1.4rem, 4vw, 2.2rem);
        font-weight: 900;
        background: linear-gradient(135deg, #dde0ff 0%, #9d95ff 55%, #00c8f8 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        margin: 0 0 .5rem;
    }
    .hero-sub {
        color: #6868a0;
        font-size: .9rem;
        margin: 0;
    }

    /* ── Cards ───────────────────────────────────────────── */
    .card {
        background: rgba(26,26,64,.75);
        border: 1px solid rgba(44,44,96,.9);
        border-radius: 14px;
        padding: 1.4rem 1.6rem;
        margin-bottom: 1.2rem;
        backdrop-filter: blur(8px);
        transition: border-color .3s;
    }
    .card:hover { border-color: rgba(124,111,255,.45); }
    .card h3 {
        font-size: .8rem;
        font-weight: 700;
        color: #6868a0;
        text-transform: uppercase;
        letter-spacing: .9px;
        margin: 0 0 1rem;
    }

    /* ── Stat badges ─────────────────────────────────────── */
    .stat-row {
        display: flex;
        gap: .7rem;
        flex-wrap: wrap;
        margin: 1rem 0;
    }
    .stat-pill {
        display: inline-flex;
        align-items: center;
        gap: .4rem;
        background: rgba(124,111,255,.1);
        border: 1px solid rgba(124,111,255,.25);
        color: #9d95ff;
        border-radius: 99px;
        padding: .3rem .9rem;
        font-size: .75rem;
        font-weight: 700;
    }
    .stat-pill.green {
        background: rgba(0,230,118,.08);
        border-color: rgba(0,230,118,.3);
        color: #00e676;
    }
    .stat-pill.red {
        background: rgba(255,92,122,.08);
        border-color: rgba(255,92,122,.3);
        color: #ff5c7a;
    }
    .stat-pill.blue {
        background: rgba(0,200,248,.08);
        border-color: rgba(0,200,248,.3);
        color: #00c8f8;
    }
    .stat-pill.amber {
        background: rgba(255,179,0,.08);
        border-color: rgba(255,179,0,.3);
        color: #ffb300;
    }

    /* ── Success / error banners ─────────────────────────── */
    .banner {
        border-radius: 12px;
        padding: 1.1rem 1.4rem;
        margin: 1rem 0;
        display: flex;
        align-items: flex-start;
        gap: .8rem;
    }
    .banner.success {
        background: rgba(0,230,118,.08);
        border: 1px solid rgba(0,230,118,.35);
    }
    .banner.error {
        background: rgba(255,92,122,.08);
        border: 1px solid rgba(255,92,122,.35);
    }
    .banner-icon { font-size: 1.5rem; flex-shrink: 0; margin-top: .1rem; }
    .banner-title { font-weight: 800; margin-bottom: .2rem; font-size: .95rem; }
    .banner-body  { font-size: .82rem; color: #a0a0c8; line-height: 1.5; }

    /* ── Progress bar ────────────────────────────────────── */
    .stProgress > div > div {
        background: linear-gradient(90deg,#7c6fff,#00c8f8) !important;
        border-radius: 99px !important;
    }
    .stProgress > div {
        background: rgba(26,26,56,.8) !important;
        border-radius: 99px !important;
        height: 10px !important;
    }

    /* ── File uploader ───────────────────────────────────── */
    [data-testid="stFileUploaderDropzone"] {
        background: rgba(124,111,255,.04) !important;
        border: 2px dashed rgba(124,111,255,.4) !important;
        border-radius: 14px !important;
        padding: 2rem !important;
        transition: all .3s !important;
    }
    [data-testid="stFileUploaderDropzone"]:hover {
        border-color: rgba(124,111,255,.8) !important;
        background: rgba(124,111,255,.09) !important;
    }

    /* ── Download button ─────────────────────────────────── */
    [data-testid="stDownloadButton"] > button {
        background: linear-gradient(135deg,#00e676,#00c853) !important;
        color: #000 !important;
        font-weight: 800 !important;
        font-size: 1rem !important;
        padding: .75rem 2.5rem !important;
        border-radius: 10px !important;
        border: none !important;
        width: 100% !important;
        transition: transform .15s, box-shadow .15s !important;
        box-shadow: 0 4px 20px rgba(0,230,118,.3) !important;
    }
    [data-testid="stDownloadButton"] > button:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 8px 28px rgba(0,230,118,.45) !important;
    }

    /* ── Convert button ──────────────────────────────────── */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg,#7c6fff,#00c8f8) !important;
        color: #fff !important;
        font-weight: 800 !important;
        font-size: 1rem !important;
        padding: .75rem 2.5rem !important;
        border-radius: 10px !important;
        border: none !important;
        width: 100% !important;
        transition: transform .15s, box-shadow .15s !important;
        box-shadow: 0 4px 20px rgba(124,111,255,.3) !important;
    }
    .stButton > button[kind="primary"]:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 8px 28px rgba(124,111,255,.45) !important;
    }

    /* ── Select boxes ────────────────────────────────────── */
    [data-testid="stSelectbox"] > div > div {
        background: rgba(26,26,64,.9) !important;
        border: 2px solid rgba(44,44,96,.9) !important;
        border-radius: 10px !important;
        color: #dde0ff !important;
    }
    [data-testid="stSelectbox"] > div > div:focus-within {
        border-color: #7c6fff !important;
        box-shadow: 0 0 0 3px rgba(124,111,255,.18) !important;
    }

    /* ── Hide default Streamlit chrome ───────────────────── */
    #MainMenu, footer, header { visibility: hidden !important; }
    </style>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────
#  MAIN UI
# ─────────────────────────────────────────────────────────
def main():
    inject_css()

    # ── Hero ────────────────────────────────────────────────
    st.markdown("""
    <div class="hero">
      <span class="hero-icon">🎬</span>
      <h1>MP4 → MP3 Converter Pro</h1>
      <p class="hero-sub">
        Stream-process videos up to 10 GB · Zero video storage · Powered by FFmpeg
      </p>
    </div>
    """, unsafe_allow_html=True)

    # ── System status bar ────────────────────────────────────
    ffmpeg_ok, ffmpeg_ver = check_ffmpeg()
    disk_gb  = get_disk_free_gb()
    ram_gb   = get_ram_free_gb()

    col1, col2, col3 = st.columns(3)
    with col1:
        if ffmpeg_ok:
            st.markdown(
                '<div class="stat-pill green">✅ ffmpeg ready</div>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                '<div class="stat-pill red">❌ ffmpeg missing</div>',
                unsafe_allow_html=True
            )
    with col2:
        disk_cls = "green" if disk_gb > 5 else "amber" if disk_gb > 1 else "red"
        st.markdown(
            f'<div class="stat-pill {disk_cls}">💾 {disk_gb:.1f} GB free</div>',
            unsafe_allow_html=True
        )
    with col3:
        ram_cls = "green" if ram_gb > 2 else "amber" if ram_gb > 0.5 else "red"
        st.markdown(
            f'<div class="stat-pill {ram_cls}">🧠 {ram_gb:.1f} GB RAM free</div>',
            unsafe_allow_html=True
        )

    if not ffmpeg_ok:
        st.error(
            f"⚠️ **ffmpeg not found!**  \n"
            f"`{ffmpeg_ver}`  \n"
            "Install: `sudo apt install ffmpeg` or `brew install ffmpeg`"
        )
        st.stop()

    st.markdown("<br>", unsafe_allow_html=True)

    # ── File uploader ────────────────────────────────────────
    st.markdown("""
    <div class="card">
      <h3>📁 Step 1 — Select Your Video File</h3>
    </div>
    """, unsafe_allow_html=True)

    uploaded = st.file_uploader(
        label="Drop a video file or click to browse",
        type=["mp4", "avi", "mkv", "mov", "wmv",
              "flv", "webm", "m4v", "ts"],
        help=(
            "Supports MP4, AVI, MKV, MOV, WMV, FLV, WEBM, M4V, TS  |  "
            "Max 10 GB  |  Video is NOT saved — processed in-memory stream"
        ),
        label_visibility="collapsed",
    )

    # Detect new upload → reset state
    if uploaded is not None:
        new_name = uploaded.name
        new_size = uploaded.size
        if (
            st.session_state.input_name != new_name
            or st.session_state.file_size_bytes != new_size
        ):
            cleanup_session()
            st.session_state.input_name        = new_name
            st.session_state.file_size_bytes   = new_size

        # Show file info card
        size_str = fmt_bytes(new_size)
        ext      = Path(new_name).suffix.upper().lstrip(".")
        st.markdown(f"""
        <div class="stat-row">
          <span class="stat-pill blue">🎞️ {ext}</span>
          <span class="stat-pill">📄 {new_name}</span>
          <span class="stat-pill amber">⚖️ {size_str}</span>
        </div>
        """, unsafe_allow_html=True)

    # ── Quality Settings ─────────────────────────────────────
    st.markdown("""
    <div class="card">
      <h3>⚙️ Step 2 — Audio Quality Settings</h3>
    </div>
    """, unsafe_allow_html=True)

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        bitrate_label = st.selectbox(
            "🎵 Bitrate",
            list(BITRATE_OPTIONS.keys()),
            index=0,
            help="Higher = better quality, larger file"
        )
    with col_b:
        sr_label = st.selectbox(
            "📊 Sample Rate",
            list(SAMPLERATE_OPTIONS.keys()),
            index=1,
            help="44 100 Hz = CD quality (recommended)"
        )
    with col_c:
        ch_label = st.selectbox(
            "🎤 Channels",
            list(CHANNEL_OPTIONS.keys()),
            index=0,
            help="Stereo for music, Mono for voice"
        )

    bitrate = BITRATE_OPTIONS[bitrate_label]
    sr      = SAMPLERATE_OPTIONS[sr_label]
    ch      = CHANNEL_OPTIONS[ch_label]

    # ── Convert Button ───────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)

    convert_disabled = (
        uploaded is None
        or st.session_state.converting
        or st.session_state.conversion_done
    )
    if st.button(
        "🚀  Convert to MP3" if not st.session_state.converting else "⚙️  Converting…",
        type="primary",
        disabled=convert_disabled,
        use_container_width=True,
    ):
        if uploaded is None:
            st.warning("Please upload a video file first.")
            st.stop()

        # ── Run conversion ───────────────────────────────────
        st.session_state.converting     = True
        st.session_state.conversion_done  = False
        st.session_state.conversion_error = None

        # Build output path (only the MP3 is written to disk)
        session_dir  = get_session_dir()
        stem         = Path(uploaded.name).stem
        output_path  = session_dir / f"{stem}.mp3"
        out_filename = f"{stem}.mp3"

        cancel_event = threading.Event()

        # Progress UI
        prog_bar   = st.progress(0, text="Initialising…")
        status_msg = st.empty()

        start_ts = time.time()

        def _update_progress(frac: float):
            pct  = int(frac * 100)
            prog_bar.progress(
                frac,
                text=f"Converting… {pct}%  |  "
                     f"{fmt_bytes(int(st.session_state.file_size_bytes * frac))} / "
                     f"{fmt_bytes(st.session_state.file_size_bytes)}"
            )

        # ── Single thread call (Streamlit is not async) ───────
        success, err_msg = convert_stream_to_mp3(
            uploaded_file = uploaded,
            output_path   = output_path,
            bitrate       = bitrate,
            sample_rate   = sr,
            channels      = ch,
            progress_cb   = _update_progress,
            cancel_event  = cancel_event,
        )

        elapsed = time.time() - start_ts
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

    # ── Results ──────────────────────────────────────────────
    if st.session_state.conversion_done and st.session_state.converted_path:
        out_path = Path(st.session_state.converted_path)

        if out_path.exists():
            # Success banner
            ratio = (
                st.session_state.output_size_bytes /
                max(1, st.session_state.file_size_bytes) * 100
            )
            st.markdown(f"""
            <div class="banner success">
              <span class="banner-icon">✅</span>
              <div>
                <div class="banner-title">Conversion Complete!</div>
                <div class="banner-body">
                  ⏱ Converted in {fmt_time(st.session_state.conversion_time)} &nbsp;·&nbsp;
                  📦 Output: {fmt_bytes(st.session_state.output_size_bytes)}
                  ({ratio:.1f}% of original) &nbsp;·&nbsp;
                  🎵 {bitrate_label.split('–')[0].strip()}
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            # Read bytes for download
            with open(out_path, "rb") as fh:
                mp3_bytes = fh.read()

            st.download_button(
                label="⬇️  Download MP3",
                data=mp3_bytes,
                file_name=st.session_state.output_filename,
                mime="audio/mpeg",
                use_container_width=True,
                on_click=cleanup_session,   # delete temp after download
            )

            # Audio preview
            st.markdown("**🔊 Preview (first 30 s):**")
            st.audio(mp3_bytes, format="audio/mpeg")

            # Convert another
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄  Convert Another File", use_container_width=True):
                cleanup_session()
                st.rerun()

        else:
            st.error("⚠️ Output file was deleted. Please convert again.")
            cleanup_session()

    elif st.session_state.conversion_error:
        st.markdown(f"""
        <div class="banner error">
          <span class="banner-icon">❌</span>
          <div>
            <div class="banner-title">Conversion Failed</div>
            <div class="banner-body">{st.session_state.conversion_error}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        if st.button("🔄  Try Again", use_container_width=True):
            st.session_state.conversion_error = None
            st.session_state.conversion_done  = False
            st.rerun()

    # ── Footer tips ──────────────────────────────────────────
    st.markdown("<br><br>", unsafe_allow_html=True)
    with st.expander("ℹ️  Tips & Information"):
        st.markdown("""
        | Topic | Detail |
        |---|---|
        | **Max file size** | 10 GB (set in `.streamlit/config.toml`) |
        | **Video storage** | ❌ Source video is **never written to disk** — piped directly through ffmpeg |
        | **Temp files** | Only the converted MP3 is stored temporarily; deleted after download |
        | **Large files** | Files are streamed in 4 MB chunks — RAM usage stays low |
        | **Formats** | MP4, AVI, MKV, MOV, WMV, FLV, WEBM, M4V, TS |
        | **ffmpeg flags** | `-vn` (no video) · `-acodec libmp3lame` · `-q:a 0` (VBR) |
        | **Sessions** | Each browser tab gets an isolated temp folder |
        """)

if __name__ == "__main__":
    main()
