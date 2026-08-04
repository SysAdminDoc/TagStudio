# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pathlib import Path

from tagstudio.core.library.alchemy.enums import FieldTypeEnum
from tagstudio.core.library.alchemy.fields import FieldID
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.workers import CancellationToken, FaceClusteringWorker, OcrWorker


def test_ocr_worker_writes_idempotent_description_field(library: Library):
    worker = OcrWorker(library, extractor=lambda _path: "detected words")

    first = worker.run(1)
    second = worker.run(1)

    assert (first.processed, first.changed, first.skipped) == (1, 1, 0)
    assert (second.processed, second.changed, second.skipped) == (1, 0, 1)
    entry = library.get_entry_full(1)
    assert entry is not None
    description = next(
        field for field in entry.fields if field.type_key == FieldID.DESCRIPTION.name
    )
    assert description.value == "detected words"
    assert description.type.type == FieldTypeEnum.TEXT_BOX


def test_ocr_worker_can_replace_existing_text(library: Library):
    worker = OcrWorker(library, extractor=lambda _path: "first result")
    worker.run(1)
    replacement = OcrWorker(
        library,
        extractor=lambda _path: "replacement result",
        replace_existing=True,
    )

    result = replacement.run(1)

    assert result.changed == 1
    entry = library.get_entry_full(1)
    assert entry is not None
    assert any(field.value == "replacement result" for field in entry.fields)


def test_face_worker_clusters_embeddings_and_reuses_tags(library: Library):
    def provider(path: Path):
        return [(0.0, 0.0)] if path.name == "foo.txt" else [(0.05, 0.0)]

    worker = FaceClusteringWorker(library, provider=provider, threshold=0.1)
    first = worker.run()
    second = worker.run()

    assert first.processed == 2
    assert first.created_tags == 1
    assert first.changed == 2
    assert second.created_tags == 0
    assert second.changed == 0
    face_tag = library.get_tag_by_name("face-cluster-1")
    assert face_tag is not None
    assert library.get_tag_entries({face_tag.id}, {1, 2}) == {face_tag.id: {1, 2}}


def test_worker_cancellation_is_reported(library: Library):
    cancellation = CancellationToken()
    cancellation.cancel()
    worker = OcrWorker(library, extractor=lambda _path: "not run")

    result = worker.run(cancel=cancellation)

    assert result.cancelled
    assert result.processed == 0
