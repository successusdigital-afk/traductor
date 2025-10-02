from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

from bs4 import BeautifulSoup
from docx import Document
from ebooklib import ITEM_DOCUMENT, epub
from pypdf import PdfReader
from sqlmodel import delete, select
from transformers import MarianMTModel, MarianTokenizer

from .database import get_session
from .models import Batch, DocumentJob, JobStatus

logger = logging.getLogger(__name__)

_STORAGE_ROOT = Path(__file__).resolve().parent / "storage"
UPLOAD_DIR = _STORAGE_ROOT / "uploads"
OUTPUT_DIR = _STORAGE_ROOT / "output"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class Translator:
    """Lazy loader around the MarianMT English->Spanish model."""

    _model_name = "Helsinki-NLP/opus-mt-en-es"
    _tokenizer: MarianTokenizer | None = None
    _model: MarianMTModel | None = None

    def _ensure_model(self) -> None:
        if self._model is None or self._tokenizer is None:
            logger.info("Loading translation model %s", self._model_name)
            self._tokenizer = MarianTokenizer.from_pretrained(self._model_name)
            self._model = MarianMTModel.from_pretrained(self._model_name)

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        self._ensure_model()
        assert self._model is not None
        assert self._tokenizer is not None

        segments = _chunk_text(text)
        translations: list[str] = []
        for segment in segments:
            inputs = self._tokenizer([segment], return_tensors="pt", padding=True, truncation=True)
            generated_tokens = self._model.generate(**inputs, max_length=512)
            outputs = self._tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)
            translations.extend(outputs)
        return "\n\n".join(translations)


_translator = Translator()
_executor = ThreadPoolExecutor(max_workers=max(os.cpu_count() or 2, 4))


def process_document_job(job_id: int) -> None:
    with get_session() as session:
        job = session.get(DocumentJob, job_id)
        if job is None:
            logger.error("Job %s not found", job_id)
            return
        job.status = JobStatus.PROCESSING
        job.progress = 0.0
        session.add(job)
        session.commit()
        session.refresh(job)

    try:
        pages = _read_document(Path(job.original_path))
        total_pages = len(pages)
        batches = _prepare_batches(total_pages, job.batch_size)

        with get_session() as session:
            job = session.get(DocumentJob, job_id)
            if job is None:
                raise RuntimeError("Job not found during processing")
            job.total_pages = total_pages
            session.exec(delete(Batch).where(Batch.job_id == job.id))
            session.commit()
            for batch_number, (start_page, end_page) in enumerate(batches, start=1):
                batch = Batch(
                    job_id=job.id,
                    batch_number=batch_number,
                    start_page=start_page,
                    end_page=end_page,
                    status=JobStatus.PENDING,
                )
                session.add(batch)
            session.commit()

        for index, (start_page, end_page) in enumerate(batches, start=1):
            batch_pages = pages[start_page - 1 : end_page]
            translated_text = _translate_pages(batch_pages)
            output_path = _write_word_document(job_id, index, translated_text)

            with get_session() as session:
                batch = session.exec(
                    select(Batch).where(Batch.job_id == job_id, Batch.batch_number == index)
                ).one()
                batch.status = JobStatus.COMPLETED
                batch.output_path = str(output_path)
                session.add(batch)

                job = session.get(DocumentJob, job_id)
                if job is None:
                    raise RuntimeError("Job not found for progress update")
                completed_batches = session.exec(
                    select(Batch).where(Batch.job_id == job_id, Batch.status == JobStatus.COMPLETED)
                ).all()
                job.progress = len(completed_batches) / len(batches)
                session.add(job)
                session.commit()

        with get_session() as session:
            job = session.get(DocumentJob, job_id)
            if job:
                job.status = JobStatus.COMPLETED
                job.progress = 1.0
                session.add(job)
                session.commit()

    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process job %s", job_id)
        with get_session() as session:
            job = session.get(DocumentJob, job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = str(exc)
                session.add(job)
                session.commit()


def queue_document_job(job_id: int) -> None:
    _executor.submit(process_document_job, job_id)


def _read_document(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".epub":
        return _read_epub(path)
    raise ValueError(f"Formato de archivo no soportado: {suffix}")


def _read_pdf(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text)
    return pages


def _read_epub(path: Path) -> list[str]:
    book = epub.read_epub(str(path))
    pages: list[str] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = soup.get_text(separator="\n")
        cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if cleaned:
            pages.extend(_chunk_text(cleaned, max_chars=2000))
    return pages


def _prepare_batches(total_pages: int, batch_size: int) -> list[tuple[int, int]]:
    batches: list[tuple[int, int]] = []
    current = 1
    while current <= total_pages:
        end = min(current + batch_size - 1, total_pages)
        batches.append((current, end))
        current = end + 1
    return batches


def _translate_pages(pages: Sequence[str]) -> str:
    translated_segments = [_translator.translate(page) for page in pages]
    return "\n\n".join(translated_segments)


def _write_word_document(job_id: int, batch_number: int, translated_text: str) -> Path:
    output_dir = OUTPUT_DIR / f"job_{job_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    document = Document()
    for paragraph in translated_text.split("\n\n"):
        document.add_paragraph(paragraph)
    output_path = output_dir / f"lote_{batch_number:03d}.docx"
    document.save(output_path)
    return output_path


def _chunk_text(text: str, max_chars: int = 1000) -> list[str]:
    segments: list[str] = []
    current = []
    length = 0
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if length + len(paragraph) > max_chars and current:
            segments.append(" \n".join(current))
            current = [paragraph]
            length = len(paragraph)
        else:
            current.append(paragraph)
            length += len(paragraph)
    if current:
        segments.append(" \n".join(current))
    return segments or [text]
