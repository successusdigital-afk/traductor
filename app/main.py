from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from .database import get_session, init_db
from .models import Batch, DocumentJob, JobStatus
from .services import OUTPUT_DIR, UPLOAD_DIR, queue_document_job

ALLOWED_BATCH_SIZES = {20, 40, 60}
ALLOWED_EXTENSIONS = {".pdf", ".epub"}

app = FastAPI(title="Traductor de documentos")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters['basename'] = lambda value: Path(value).name


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    with get_session() as session:
        jobs = session.exec(select(DocumentJob).order_by(DocumentJob.created_at.desc())).all()
        job_ids = [job.id for job in jobs]
        batches_by_job: dict[int, list[Batch]] = {}
        if job_ids:
            batches = session.exec(select(Batch).where(Batch.job_id.in_(job_ids))).all()
            for batch in batches:
                batches_by_job.setdefault(batch.job_id, []).append(batch)

    generated_files: list[dict[str, Any]] = []
    for job in jobs:
        for batch in sorted(batches_by_job.get(job.id, []), key=lambda b: b.batch_number):
            if batch.output_path:
                generated_files.append(
                    {
                        "job_id": job.id,
                        "job_name": job.filename,
                        "batch_number": batch.batch_number,
                        "path": batch.output_path,
                        "created_at": job.created_at,
                    }
                )

    context = {
        "request": request,
        "jobs": jobs,
        "batches_by_job": batches_by_job,
        "generated_files": generated_files,
        "batch_sizes": sorted(ALLOWED_BATCH_SIZES),
    }
    return templates.TemplateResponse("index.html", context)


@app.post("/upload")
def upload_document(
    file: UploadFile = File(...),
    batch_size: int = Form(...),
) -> RedirectResponse:
    if batch_size not in ALLOWED_BATCH_SIZES:
        raise HTTPException(status_code=400, detail="Tamaño de lote inválido")

    extension = Path(file.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Formato de archivo no soportado")

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe_name = f"{timestamp}_{Path(file.filename).name}"
    destination = UPLOAD_DIR / safe_name

    with destination.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    with get_session() as session:
        job = DocumentJob(filename=file.filename, original_path=str(destination), batch_size=batch_size)
        session.add(job)
        session.commit()
        session.refresh(job)

    queue_document_job(job.id)

    return RedirectResponse(url="/", status_code=303)


@app.get("/jobs", response_class=JSONResponse)
def job_status() -> JSONResponse:
    with get_session() as session:
        jobs = session.exec(select(DocumentJob)).all()
        payload = []
        for job in jobs:
            payload.append(
                {
                    "id": job.id,
                    "filename": job.filename,
                    "status": job.status.value,
                    "progress": job.progress,
                    "total_pages": job.total_pages,
                    "batch_size": job.batch_size,
                    "error_message": job.error_message,
                }
            )
        return JSONResponse(payload)


@app.get("/files/{job_id}/{filename}")
def download_file(job_id: int, filename: str) -> FileResponse:
    job_dir = OUTPUT_DIR / f"job_{job_id}"
    file_path = job_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(file_path, filename=filename)
