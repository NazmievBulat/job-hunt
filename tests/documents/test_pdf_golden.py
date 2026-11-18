"""Golden regressions for rendered PDF bytes and page pixels.

Dependency bumps (ReportLab, fonts, pypdfium2) should fail here first. A byte
change with identical pixels is invisible; a pixel change needs a human look at
the saved actual/diff images. Regenerate after review with UPDATE_GOLDENS=1.
"""

import hashlib
import os
from pathlib import Path
from typing import cast

import pypdfium2 as pdfium  # pyright: ignore[reportMissingTypeStubs]
import pytest
from movable_clock import MovableClock
from PIL import Image, ImageChops

from resume_kit.contracts.resume import Resume
from resume_kit.documents.pdf import render_pdf

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"
UPDATE = os.environ.get("UPDATE_GOLDENS") == "1"
RASTER_SCALE = 100 / 72  # 100 DPI: enough to see glyphs, small enough to commit

CASES = {
    "example": ROOT / "examples/resume.example.json",
    "two_page_boundary": ROOT / "tests/documents/fixtures/two_page_boundary.json",
}


def _render(case: str) -> bytes:
    return render_pdf(Resume.model_validate_json(CASES[case].read_text()), clock=MovableClock())


def _rasterize(data: bytes) -> list[Image.Image]:
    document = pdfium.PdfDocument(data)
    try:
        return [
            page.render(scale=RASTER_SCALE, grayscale=True).to_pil().convert("L")  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            for page in document
        ]
    finally:
        document.close()


@pytest.mark.parametrize("case", CASES)
def test_pdf_bytes_match_golden_hash(case: str) -> None:
    digest = hashlib.sha256(_render(case)).hexdigest()
    golden = GOLDEN / f"{case}.sha256"
    if UPDATE:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        golden.write_text(digest + "\n")
        # Golden regeneration updates the fixture instead of comparing it.
        pytest.skip(f"updated {golden.name}")  # noqa: TID251
    assert golden.exists(), f"missing {golden}; run with UPDATE_GOLDENS=1"
    assert digest == golden.read_text().strip(), (
        f"{case} PDF bytes changed; if test_pdf_pixels_match_golden still passes "
        "the change is invisible, so review and rerun with UPDATE_GOLDENS=1"
    )


@pytest.mark.parametrize("case", CASES)
def test_pdf_pixels_match_golden(case: str, tmp_path: Path) -> None:
    pages = _rasterize(_render(case))
    if UPDATE:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for stale in GOLDEN.glob(f"{case}-p*.png"):
            stale.unlink()
        for number, page in enumerate(pages, start=1):
            page.save(GOLDEN / f"{case}-p{number}.png", optimize=True)
        # Golden regeneration updates the fixture instead of comparing it.
        pytest.skip(f"updated {len(pages)} {case} page image(s)")  # noqa: TID251
    expected = sorted(GOLDEN.glob(f"{case}-p*.png"))
    assert expected, f"missing {case} page images; run with UPDATE_GOLDENS=1"
    assert len(pages) == len(expected), f"{case} page count {len(expected)} -> {len(pages)}"
    changed: list[str] = []
    for number, (actual, golden_path) in enumerate(zip(pages, expected, strict=True), start=1):
        with Image.open(golden_path) as golden_image:
            golden = golden_image.convert("L")
        if actual.size == golden.size and ImageChops.difference(actual, golden).getbbox() is None:
            continue
        actual.save(tmp_path / f"{case}-p{number}.actual.png")
        if actual.size == golden.size:
            diff = ImageChops.difference(actual, golden).point(lambda v: 255 if v else 0)  # pyright: ignore[reportUnknownMemberType]
            diff.save(tmp_path / f"{case}-p{number}.diff.png")
        changed.append(f"page {number}")
    assert not changed, (
        f"{case} rendering changed on {', '.join(changed)}; "
        f"inspect actual/diff images in {tmp_path}"
    )


def test_rasterized_pages_are_not_blank() -> None:
    """Guard against a pixel check that trivially passes on empty renders."""
    for case in CASES:
        for page in _rasterize(_render(case)):
            darkest, lightest = cast("tuple[int, int]", page.getextrema())
            assert darkest < 64 and lightest == 255, f"{case} rendered a blank page"
