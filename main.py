
import os
import uuid
import time
import threading
import subprocess
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CLIPS_DIR = ROOT / "clips"
CLIPS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="LIVE CLIPPER AI")
security = HTTPBasic()

APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

lock = threading.Lock()
stop_event = threading.Event()
current_process = None

job = {
    "id": None,
    "running": False,
    "status": "Esperando un enlace",
    "url": None,
    "platform": None,
    "error": None
}


def require_login(credentials: HTTPBasicCredentials = Depends(security)):
    if not APP_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="Configura APP_PASSWORD en Render."
        )

    valid_user = secrets.compare_digest(
        credentials.username, APP_USER
    )
    valid_password = secrets.compare_digest(
        credentials.password, APP_PASSWORD
    )

    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=401,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Basic"}
        )

    return True


class StartRequest(BaseModel):
    url: HttpUrl


def detect_platform(url):
    host = (urlparse(url).hostname or "").lower()

    platforms = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "twitch.tv": "Twitch",
        "tiktok.com": "TikTok",
        "facebook.com": "Facebook",
        "fb.watch": "Facebook",
        "kick.com": "Kick",
        "rumble.com": "Rumble"
    }

    for domain, name in platforms.items():
        if host == domain or host.endswith("." + domain):
            return name

    raise ValueError("Plataforma no reconocida.")


def save_finished_segments(folder, job_id, final=False):
    files = sorted(folder.glob("segment_*.mp4"))

    # El último archivo puede estar todavía abierto por FFmpeg.
    if not final and files:
        files = files[:-1]

    for source in files:
        destination = folder / source.name.replace(
            "segment_", "clip_", 1
        )

        if destination.exists():
            continue

        try:
            if source.stat().st_size < 100_000:
                continue
            source.rename(destination)
        except (FileNotFoundError, PermissionError):
            continue


def capture_stream(url, job_id):
    global current_process

    folder = CLIPS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)

    try:
        with lock:
            job["status"] = "Conectando con el directo..."

        # Obtener una dirección de transmisión accesible.
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-warnings",
                "--no-playlist",
                "-f", "best",
                "-g",
                url
            ],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                result.stderr[-600:] or
                "No se pudo obtener la transmisión."
            )

        stream_url = result.stdout.strip().splitlines()[0]

        output = str(folder / "segment_%03d.mp4")

        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i", stream_url,
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", "30",
            "-reset_timestamps", "1",
            "-segment_format", "mp4",
            output
        ]

        with open(folder / "ffmpeg.log", "w") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log
            )

            with lock:
                current_process = process
                job["status"] = "Capturando directo..."

            while process.poll() is None and not stop_event.is_set():
                save_finished_segments(folder, job_id)
                time.sleep(2)

            if stop_event.is_set() and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

            save_finished_segments(folder, job_id, final=True)

            if process.returncode not in (0, None) and not stop_event.is_set():
                log_text = (folder / "ffmpeg.log").read_text(
                    errors="replace"
                )[-600:]
                raise RuntimeError(
                    log_text or "FFmpeg terminó con un error."
                )

        with lock:
            job["status"] = (
                "Captura detenida"
                if stop_event.is_set()
                else "Transmisión finalizada"
            )

    except Exception as error:
        with lock:
            job["status"] = "Error"
            job["error"] = str(error)

    finally:
        with lock:
            current_process = None
            job["running"] = False
            job["url"] = None
            job["platform"] = None


@app.get("/api/health")
def health():
    return {"status": "online"}


@app.get("/")
def home(_=Depends(require_login)):
    page = STATIC_DIR / "index.html"

    if not page.exists():
        raise HTTPException(
            status_code=500,
            detail="No se encuentra static/index.html"
        )

    return FileResponse(page)


@app.get("/api/status")
def status(_=Depends(require_login)):
    with lock:
        return dict(job)


@app.post("/api/start")
def start(data: StartRequest, _=Depends(require_login)):
    global current_process

    url = str(data.url)

    try:
        platform = detect_platform(url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    with lock:
        if job["running"]:
            raise HTTPException(
                status_code=409,
                detail="Ya existe una captura activa."
            )

        stop_event.clear()
        job_id = uuid.uuid4().hex[:10]

        job.update({
            "id": job_id,
            "running": True,
            "status": "Iniciando captura...",
            "url": url,
            "platform": platform,
            "error": None
        })

    thread = threading.Thread(
        target=capture_stream,
        args=(url, job_id),
        daemon=True
    )
    thread.start()

    return {"ok": True, "id": job_id}


@app.post("/api/stop")
def stop(_=Depends(require_login)):
    global current_process

    with lock:
        if not job["running"]:
            raise HTTPException(
                status_code=400,
                detail="No hay ninguna captura activa."
            )

        stop_event.set()
        job["status"] = "Deteniendo captura..."

        process = current_process

    if process and process.poll() is None:
        process.terminate()

    return {"ok": True, "message": "Detención solicitada"}


@app.get("/api/clips")
def clips(_=Depends(require_login)):
    results = []

    for folder in CLIPS_DIR.iterdir():
        if not folder.is_dir():
            continue

        for file in folder.glob("clip_*.mp4"):
            results.append({
                "job": folder.name,
                "name": file.name,
                "size": file.stat().st_size
            })

    return results


@app.get("/api/clips/{job_id}/{filename}")
def download_clip(
    job_id: str,
    filename: str,
    _=Depends(require_login)
):
    if not job_id.isalnum() or not filename.startswith("clip_"):
        raise HTTPException(status_code=404)

    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404)

    path = CLIPS_DIR / job_id / filename

    if not path.is_file():
        raise HTTPException(status_code=404)

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=filename
    )


# Publicar también la carpeta estática.
app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)
