"""
Local web GUI for the AI DJ Mixing System.

A small single-user control panel: drag & drop MP3s into the library,
set the mix request and a couple of mixing parameters, hit "Process",
watch the pipeline log stream live, and preview the resulting mix.

Meant for localhost use only - there is no authentication, so do not
expose this beyond your own machine.
"""

from __future__ import annotations

import os
import sys
import uuid
import logging
import threading
import contextlib
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)  # all pipeline modules use paths relative to the project root

load_dotenv()

from run_pipeline import run_pipeline  # noqa: E402 (must import after chdir/load_dotenv)
import track_analysis_openai_approach as _song_selection
import bpm_lookup as _bpm_lookup
import structure_detector as _structure_detector

SONGS_DIR = BASE_DIR / "songs"
OUTPUT_DIR = BASE_DIR / "output"
ENV_PATH = BASE_DIR / ".env"
ALLOWED_EXTENSIONS = {".mp3"}

SONGS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB per upload batch

JOBS = {}
JOBS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _safe_song_filename(filename: str) -> str:
    """Strip any path component and null bytes, but keep spaces/hyphens intact -
    the song selector parses 'Artist - Title.mp3' and would break on
    werkzeug's secure_filename(), which replaces spaces with underscores."""
    name = os.path.basename((filename or "").replace("\x00", "")).strip()
    if name in ("", ".", ".."):
        return ""
    return name


def _resolve_song_path(filename: str) -> Path | None:
    """Resolve a song filename to a path guaranteed to live inside SONGS_DIR, or None."""
    safe_name = _safe_song_filename(filename)
    if not safe_name:
        return None
    target = (SONGS_DIR / safe_name).resolve()
    if target.parent != SONGS_DIR.resolve():
        return None
    return target


def _reload_openai_clients():
    """Re-create the OpenAI clients in each pipeline module so a freshly
    saved API key takes effect without restarting the web server."""
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return
    for module in (_song_selection, _bpm_lookup, _structure_detector):
        try:
            module.client = OpenAI(api_key=key)
        except Exception:
            pass


class _JobLogWriter:
    """File-like object: appends every write to a job's log and echoes to the real stdout."""

    def __init__(self, job, real_stream):
        self.job = job
        self.real_stream = real_stream

    def write(self, data):
        if data:
            with JOBS_LOCK:
                self.job["log"].append(data)
            self.real_stream.write(data)

    def flush(self):
        self.real_stream.flush()


class _JobLogHandler(logging.Handler):
    """Logging handler that mirrors log records (from run_pipeline's logger) into a job's log."""

    def __init__(self, job):
        super().__init__()
        self.job = job

    def emit(self, record):
        msg = self.format(record)
        with JOBS_LOCK:
            self.job["log"].append(msg + "\n")


def _run_job(job_id, user_input, overlap_duration, fade_duration):
    job = JOBS[job_id]
    real_stdout = sys.stdout

    handler = _JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    pipeline_logger = logging.getLogger("AI_DJ_Pipeline")
    pipeline_logger.addHandler(handler)

    with RUN_LOCK:
        job["status"] = "running"
        try:
            with contextlib.redirect_stdout(_JobLogWriter(job, real_stdout)):
                run_pipeline(user_input, overlap_duration=overlap_duration, fade_duration=fade_duration)
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            with JOBS_LOCK:
                job["log"].append(f"\n[FATAL] {e}\n")
        finally:
            pipeline_logger.removeHandler(handler)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/songs", methods=["GET"])
def list_songs():
    songs = [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(SONGS_DIR.glob("*.mp3"))
    ]
    return jsonify(songs)


@app.route("/api/upload", methods=["POST"])
def upload_songs():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Geen bestanden ontvangen"}), 400

    saved, rejected = [], []
    for f in files:
        filename = _safe_song_filename(f.filename or "")
        if not filename or not _allowed_file(filename):
            rejected.append(f.filename)
            continue
        target = _resolve_song_path(filename)
        if target is None:
            rejected.append(f.filename)
            continue
        f.save(target)
        saved.append(filename)

    return jsonify({"saved": saved, "rejected": rejected})


@app.route("/api/songs/<path:filename>", methods=["DELETE"])
def delete_song(filename):
    target = _resolve_song_path(filename)
    if target is None or not target.is_file():
        return jsonify({"error": "Niet gevonden"}), 404
    target.unlink()
    return jsonify({"deleted": target.name})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({"has_api_key": bool(os.getenv("OPENAI_API_KEY"))})


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(force=True) or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "Geen API key opgegeven"}), 400

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.strip().startswith("OPENAI_API_KEY=")]
    lines.append(f"OPENAI_API_KEY={api_key}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    os.environ["OPENAI_API_KEY"] = api_key
    _reload_openai_clients()

    return jsonify({"has_api_key": True})


@app.route("/api/run", methods=["POST"])
def start_run():
    if RUN_LOCK.locked():
        return jsonify({"error": "Er draait al een mix. Wacht tot deze klaar is."}), 409

    if not any(SONGS_DIR.glob("*.mp3")):
        return jsonify({"error": "Nog geen nummers geüpload"}), 400

    data = request.get_json(force=True) or {}
    user_input = (data.get("user_input") or "Mix all songs").strip()
    try:
        overlap_duration = float(data.get("overlap_duration", 8.0))
        fade_duration = float(data.get("fade_duration", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Ongeldige parameter"}), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "log": [], "error": None}

    threading.Thread(
        target=_run_job, args=(job_id, user_input, overlap_duration, fade_duration), daemon=True
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Onbekende job"}), 404

    with JOBS_LOCK:
        log_text = "".join(job["log"])

    result = None
    if job["status"] == "done":
        mix_path = OUTPUT_DIR / "mix.mp3"
        waveform_path = OUTPUT_DIR / "waveforms" / "mix_overview.png"
        plan_path = OUTPUT_DIR / "mixing_plan.json"
        result = {
            "mix_url": "/output/mix.mp3" if mix_path.exists() else None,
            "waveform_url": "/output/waveforms/mix_overview.png" if waveform_path.exists() else None,
            "mixing_plan_url": "/output/mixing_plan.json" if plan_path.exists() else None,
        }

    return jsonify({"status": job["status"], "log": log_text, "error": job.get("error"), "result": result})


@app.route("/output/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    # Localhost only, single-user tool - do not expose this to the network as-is.
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
