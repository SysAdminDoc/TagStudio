# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from tagstudio.core.library.alchemy.fields import FieldID
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry, Tag


class WorkerError(RuntimeError):
    """Base error for an opt-in metadata worker."""


class WorkerUnavailableError(WorkerError):
    """Raised when an optional local extractor is not installed or configured."""


WorkerUnavailable = WorkerUnavailableError


@dataclass(frozen=True, slots=True)
class WorkerFailure:
    entry_id: int | None
    message: str


@dataclass(frozen=True, slots=True)
class WorkerProgress:
    completed: int
    total: int
    entry_id: int | None


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    processed: int = 0
    changed: int = 0
    created_tags: int = 0
    skipped: int = 0
    cancelled: bool = False
    failures: tuple[WorkerFailure, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failures and not self.cancelled


ProgressCallback = Callable[[WorkerProgress], None]
OcrExtractor = Callable[[Path], str]
FaceEmbedding = Sequence[float]
FaceEmbeddingProvider = Callable[[Path], Iterable[FaceEmbedding]]


class CancellationToken:
    """Thread-safe cancellation state shared with a running worker."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def tesseract_ocr(path: Path) -> str:
    """Extract text with a locally installed Tesseract executable."""
    executable = shutil.which("tesseract")
    if executable is None:
        raise WorkerUnavailable(
            "OCR requires a local Tesseract executable; none was found on PATH"
        )

    completed = subprocess.run(
        [executable, str(path), "stdout", "--psm", "6"],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"Tesseract exited with code {completed.returncode}"
        raise WorkerError(message)
    return completed.stdout.strip()


def _worklist(library: Library, entry_ids: Iterable[int] | int | None) -> list[Entry]:
    entries = sorted(library.all_entries(), key=lambda entry: entry.id)
    if entry_ids is None:
        return entries

    requested_ids = (
        {entry_ids} if isinstance(entry_ids, int) else {int(entry_id) for entry_id in entry_ids}
    )
    selected = [entry for entry in entries if entry.id in requested_ids]
    missing_ids = requested_ids.difference(entry.id for entry in selected)
    if missing_ids:
        raise WorkerError(f"Unknown entry IDs: {sorted(missing_ids)}")
    return selected


def _notify(progress: ProgressCallback | None, completed: int, total: int, entry_id: int) -> None:
    if progress is not None:
        progress(WorkerProgress(completed=completed, total=total, entry_id=entry_id))


class OcrWorker:
    """Opt-in OCR worker that writes extracted text into a text field."""

    def __init__(
        self,
        library: Library,
        extractor: OcrExtractor | None = None,
        *,
        field_id: FieldID | str = FieldID.DESCRIPTION,
        replace_existing: bool = False,
    ) -> None:
        self.library = library
        self.extractor = extractor or tesseract_ocr
        self._uses_default_extractor = extractor is None
        self.field_id = field_id
        self.replace_existing = replace_existing

    def run(
        self,
        entry_ids: Iterable[int] | int | None = None,
        *,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> WorkerRunResult:
        entries = _worklist(self.library, entry_ids)
        if self._uses_default_extractor and shutil.which("tesseract") is None:
            raise WorkerUnavailable(
                "OCR requires a local Tesseract executable; none was found on PATH"
            )

        failures: list[WorkerFailure] = []
        processed = 0
        changed = 0
        skipped = 0
        for completed, entry in enumerate(entries, start=1):
            if cancel is not None and cancel.cancelled:
                return WorkerRunResult(
                    processed=processed,
                    changed=changed,
                    skipped=skipped,
                    cancelled=True,
                    failures=tuple(failures),
                )
            try:
                text = self.extractor(self.library.resolve_entry_path(entry)).strip()
                if not text:
                    skipped += 1
                else:
                    current = self.library.get_entry_full(
                        entry.id, with_fields=True, with_tags=False
                    )
                    existing = next(
                        (
                            field
                            for field in (current.fields if current is not None else [])
                            if field.type_key
                            == (
                                self.field_id.name
                                if isinstance(self.field_id, FieldID)
                                else self.field_id
                            )
                            and field.value
                        ),
                        None,
                    )
                    if existing is not None and not self.replace_existing:
                        skipped += 1
                    else:
                        changed += int(
                            self.library.upsert_text_field(entry.id, self.field_id, text)
                        )
                    processed += 1
            except Exception as error:  # workers report per-entry failures and continue
                failures.append(WorkerFailure(entry.id, str(error)))
            _notify(progress, completed, len(entries), entry.id)

        return WorkerRunResult(
            processed=processed,
            changed=changed,
            skipped=skipped,
            failures=tuple(failures),
        )


class OpenCVFaceEmbeddingProvider:
    """Offline face detector with a deterministic image-patch embedding fallback."""

    def __init__(self, min_neighbors: int = 5) -> None:
        self.min_neighbors = min_neighbors

    def __call__(self, path: Path) -> list[FaceEmbedding]:
        try:
            import cv2
        except ImportError as error:  # pragma: nocover - dependency is part of the app build
            raise WorkerUnavailable("Face clustering requires OpenCV") from error

        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise WorkerError(f"Could not read image: {path}")
        cascade = cv2.CascadeClassifier(
            str(
                Path(cv2.data.haarcascades)  # type: ignore[attr-defined]
                / "haarcascade_frontalface_default.xml"
            )
        )
        if cascade.empty():
            raise WorkerUnavailable("OpenCV face detector data is unavailable")

        faces = cascade.detectMultiScale(image, scaleFactor=1.1, minNeighbors=self.min_neighbors)
        embeddings: list[FaceEmbedding] = []
        for x, y, width, height in faces:
            crop = image[y : y + height, x : x + width]
            resized = cv2.resize(crop, (16, 16)).astype("float32") / 255.0
            embeddings.append(tuple(float(value) for value in resized.flatten()))
        return embeddings


def _distance(left: FaceEmbedding, right: FaceEmbedding) -> float:
    if len(left) != len(right):
        raise WorkerError("Face embeddings must all have the same dimensions")
    return sum((left[index] - right[index]) ** 2 for index in range(len(left))) ** 0.5


class FaceClusteringWorker:
    """Opt-in face clustering worker that writes stable per-run tags to entries."""

    def __init__(
        self,
        library: Library,
        provider: FaceEmbeddingProvider | None = None,
        *,
        threshold: float = 0.35,
        tag_prefix: str = "face-cluster",
    ) -> None:
        if threshold <= 0:
            raise ValueError("Face cluster threshold must be positive")
        if not tag_prefix.strip():
            raise ValueError("Face cluster tag prefix must not be empty")
        self.library = library
        self.provider = provider or OpenCVFaceEmbeddingProvider()
        self.threshold = threshold
        self.tag_prefix = tag_prefix.strip()

    def run(
        self,
        entry_ids: Iterable[int] | int | None = None,
        *,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> WorkerRunResult:
        entries = _worklist(self.library, entry_ids)
        failures: list[WorkerFailure] = []
        records: list[tuple[int, FaceEmbedding]] = []
        processed = 0
        for completed, entry in enumerate(entries, start=1):
            if cancel is not None and cancel.cancelled:
                return WorkerRunResult(
                    processed=processed,
                    cancelled=True,
                    failures=tuple(failures),
                )
            try:
                records.extend(
                    (entry.id, embedding)
                    for embedding in self.provider(self.library.resolve_entry_path(entry))
                )
                processed += 1
            except Exception as error:  # workers report per-entry failures and continue
                failures.append(WorkerFailure(entry.id, str(error)))
            _notify(progress, completed, len(entries), entry.id)

        clusters: list[list[tuple[int, FaceEmbedding]]] = []
        representatives: list[FaceEmbedding] = []
        for entry_id, embedding in records:
            for index, representative in enumerate(representatives):
                if _distance(embedding, representative) <= self.threshold:
                    clusters[index].append((entry_id, embedding))
                    break
            else:
                representatives.append(embedding)
                clusters.append([(entry_id, embedding)])

        changed = 0
        created_tags = 0
        for cluster_index, cluster in enumerate(clusters, start=1):
            name = f"{self.tag_prefix}-{cluster_index}"
            tag = self.library.get_tag_by_name(name)
            if tag is None:
                tag = self.library.add_tag(Tag(name=name))
                if tag is None:
                    failures.append(WorkerFailure(None, f"Could not create tag {name!r}"))
                    continue
                created_tags += 1

            cluster_entry_ids = {entry_id for entry_id, _embedding in cluster}
            assigned = self.library.get_tag_entries({tag.id}, cluster_entry_ids)[tag.id]
            missing_entry_ids = cluster_entry_ids.difference(assigned)
            if missing_entry_ids:
                changed += self.library.add_tags_to_entries(missing_entry_ids, tag.id)

        return WorkerRunResult(
            processed=processed,
            changed=changed,
            created_tags=created_tags,
            failures=tuple(failures),
        )


OCRWorker = OcrWorker
FaceClusterWorker = FaceClusteringWorker
