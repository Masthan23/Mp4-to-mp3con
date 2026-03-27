import streamlit as st
import subprocess
import sys
import os

st.set_page_config(page_title="Debug", page_icon="🔍")
st.title("🔍 Debug Info")

st.subheader("Python version")
st.code(sys.version)

st.subheader("ffmpeg check")
try:
    r = subprocess.run(["ffmpeg", "-version"],
                       capture_output=True, text=True, timeout=10)
    st.success("ffmpeg found!")
    st.code(r.stdout[:300])
except FileNotFoundError:
    st.error("ffmpeg NOT found — packages.txt missing or not deployed")
except Exception as e:
    st.error(f"Error: {e}")

st.subheader("packages.txt exists?")
st.write(os.path.exists("packages.txt"))

st.subheader("Temp dir writable?")
import tempfile, pathlib
tmp = pathlib.Path(tempfile.gettempdir()) / "test_write.txt"
try:
    tmp.write_text("ok")
    tmp.unlink()
    st.success("Temp dir is writable")
except Exception as e:
    st.error(f"Temp dir error: {e}")

st.subheader("All installed packages")
r2 = subprocess.run(["pip", "list"], capture_output=True, text=True)
st.code(r2.stdout)
