# app.py — CRASH DETECTOR
# Deploy this first to find the exact error

import sys
import os
import traceback

# ── Test every import individually ──────────────────────────────────────────
errors = []

try:
    import streamlit as st
except Exception as e:
    # If streamlit itself fails we can't do anything
    print("FATAL: streamlit import failed:", e)
    sys.exit(1)

# Must be first
st.set_page_config(page_title="Crash Detector", page_icon="🔍")

import_tests = [
    "subprocess",
    "tempfile",
    "time",
    "threading",
    "uuid",
    "shutil",
    "os",
    "sys",
    "pathlib",
    "traceback",
    "psutil",
]

st.title("🔍 Crash Detector")
st.subheader("Import Tests")

results = {}
for mod in import_tests:
    try:
        __import__(mod)
        results[mod] = "✅ OK"
    except Exception as e:
        results[mod] = f"❌ FAILED: {e}"

for mod, res in results.items():
    st.write(f"`{mod}` → {res}")

st.divider()
st.subheader("Python Info")
st.code(f"""
Python : {sys.version}
Platform: {sys.platform}
Executable: {sys.executable}
""")

st.divider()
st.subheader("Environment Variables")
safe_keys = ["PATH", "HOME", "USER", "PYTHONPATH", "STREAMLIT_SERVER_PORT"]
for k in safe_keys:
    st.write(f"`{k}` = `{os.environ.get(k, 'NOT SET')}`")

st.divider()
st.subheader("ffmpeg Check")
import subprocess
try:
    r = subprocess.run(
        ["ffmpeg", "-version"],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode == 0:
        st.success("ffmpeg found!")
        st.code(r.stdout[:200])
    else:
        st.error(f"ffmpeg returned code {r.returncode}")
        st.code(r.stderr[:200])
except FileNotFoundError:
    st.error("ffmpeg NOT in PATH")
except Exception as e:
    st.error(f"ffmpeg check failed: {e}")

st.divider()
st.subheader("Disk & Temp")
import tempfile
from pathlib import Path
tmp = Path(tempfile.gettempdir())
st.write(f"Temp dir: `{tmp}`")
st.write(f"Writable: `{os.access(tmp, os.W_OK)}`")

try:
    test_file = tmp / "streamlit_test_write.txt"
    test_file.write_text("ok")
    test_file.unlink()
    st.success("Temp dir write: OK")
except Exception as e:
    st.error(f"Temp dir write FAILED: {e}")

st.divider()
st.subheader("requirements.txt content")
req_paths = [
    "/mount/src/mp4-to-mp3con/requirements.txt",
    "requirements.txt",
    "../requirements.txt",
]
found = False
for rp in req_paths:
    if Path(rp).exists():
        st.code(Path(rp).read_text())
        found = True
        break
if not found:
    st.error("requirements.txt NOT FOUND at any expected path")

st.divider()
st.subheader("packages.txt content")
pkg_paths = [
    "/mount/src/mp4-to-mp3con/packages.txt",
    "packages.txt",
    "../packages.txt",
]
found = False
for pp in pkg_paths:
    if Path(pp).exists():
        st.code(Path(pp).read_text())
        found = True
        break
if not found:
    st.error("packages.txt NOT FOUND at any expected path")

st.divider()
st.subheader("Installed packages (pip list)")
try:
    r2 = subprocess.run(
        ["pip", "list"], capture_output=True, text=True, timeout=15
    )
    st.code(r2.stdout)
except Exception as e:
    st.error(f"pip list failed: {e}")

st.divider()
st.subheader("All files in repo")
try:
    r3 = subprocess.run(
        ["find", "/mount/src/mp4-to-mp3con", "-type", "f"],
        capture_output=True, text=True, timeout=10
    )
    st.code(r3.stdout)
except Exception as e:
    st.error(f"find failed: {e}")

st.success("✅ App loaded successfully — all checks above ran without crashing")
