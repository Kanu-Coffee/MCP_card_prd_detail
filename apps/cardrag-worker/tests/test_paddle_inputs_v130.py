"""FIX_03 follow-up: orphaned .paddle-input-* symlink self-heal."""

from __future__ import annotations

from pathlib import Path

from cardrag_worker.paddleocr_runner import _paddle_input_path
from cardrag_worker.pipeline import WorkerPipeline


def test_input_context_sweeps_stale_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "cas-object"
    real.write_bytes(b"%PDF-1.4")
    out = tmp_path / "ocr" / "ocr.md"
    stale = out.parent / ".paddle-input-9999.pdf"
    out.parent.mkdir(parents=True)
    stale.symlink_to(tmp_path / "gone.pdf")
    assert stale.is_symlink()
    with _paddle_input_path(real, output_path=out) as active:
        assert active.is_symlink()
        assert not stale.exists() or not stale.is_symlink()
        assert active.read_bytes() == b"%PDF-1.4"
    assert not any(p.name.startswith(".paddle-input-") for p in out.parent.iterdir())


def test_resume_sweep_only_touches_exact_symlinks(tmp_path: Path) -> None:
    doc = tmp_path / "runs" / "run-9" / "documents" / "doc_a" / "ocr" / "checkpoints"
    doc.mkdir(parents=True)
    orphan = doc / ".paddle-input-1083.pdf"
    orphan.symlink_to(tmp_path / "cas.pdf")
    real_pdf = doc / "keep.pdf"
    real_pdf.write_bytes(b"x")
    other_name = doc / ".paddle-input-note.txt"
    other_name.write_bytes(b"y")
    pipeline = WorkerPipeline.__new__(WorkerPipeline)
    pipeline.state_dir = tmp_path
    pipeline._sweep_stale_paddle_input_symlinks("run-9")
    assert not orphan.exists()
    assert real_pdf.exists() and other_name.exists()
    # missing run dir must be a no-op
    pipeline._sweep_stale_paddle_input_symlinks("run-missing")
