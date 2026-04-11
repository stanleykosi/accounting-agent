"""
Purpose: Execute the local OCR toolchain for scanned PDF documents.
Scope: OCRmyPDF/Tesseract dependency checks, temporary file handling, sidecar text
capture, searchable PDF capture, timeout enforcement, and explicit parser errors.
Dependencies: Python subprocess/tempfile utilities and parser-domain models.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from services.parser.models import OcrExecutionResult, ParserErrorCode, ParserPipelineError

DEFAULT_OCR_TIMEOUT_SECONDS = 900


class OcrDependencyUnavailableError(ParserPipelineError):
    """Represent a missing local OCR dependency with explicit recovery instructions."""

    def __init__(self, *, missing_binary: str) -> None:
        """Create an OCR dependency failure for one missing host binary."""

        super().__init__(
            code=ParserErrorCode.OCR_DEPENDENCY_UNAVAILABLE,
            message=(
                f"{missing_binary} is required for scanned PDFs. Install Tesseract and "
                "OCRmyPDF on this host, then retry the parse job."
            ),
        )


class OcrRunner:
    """Run OCRmyPDF and return text plus a searchable normalized PDF derivative."""

    def __init__(self, *, timeout_seconds: int = DEFAULT_OCR_TIMEOUT_SECONDS) -> None:
        """Capture the maximum OCR runtime allowed for a single PDF."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

        self._timeout_seconds = timeout_seconds

    def run_pdf_ocr(self, *, payload: bytes, filename: str) -> OcrExecutionResult:
        """Run OCRmyPDF over one scanned PDF payload and return deterministic outputs."""

        _require_binary("ocrmypdf")
        _require_binary("tesseract")

        with tempfile.TemporaryDirectory(prefix="accounting-agent-ocr-") as temp_dir:
            work_dir = Path(temp_dir)
            input_path = work_dir / "input.pdf"
            output_path = work_dir / "output.pdf"
            sidecar_path = work_dir / "ocr.txt"
            input_path.write_bytes(payload)

            command = [
                "ocrmypdf",
                "--skip-text",
                "--deskew",
                "--rotate-pages",
                "--sidecar",
                str(sidecar_path),
                str(input_path),
                str(output_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise ParserPipelineError(
                    code=ParserErrorCode.OCR_FAILED,
                    message=f"OCR timed out while processing {filename}.",
                ) from error
            except subprocess.CalledProcessError as error:
                raise ParserPipelineError(
                    code=ParserErrorCode.OCR_FAILED,
                    message=(
                        f"OCR failed for {filename}: "
                        f"{(error.stderr or error.stdout or 'no process output').strip()}"
                    ),
                ) from error

            text_payload = sidecar_path.read_text(encoding="utf-8") if sidecar_path.exists() else ""
            searchable_pdf_payload = output_path.read_bytes() if output_path.exists() else None
            return OcrExecutionResult(
                text=text_payload,
                searchable_pdf_payload=searchable_pdf_payload,
                metadata={
                    "command": " ".join(command[:4]),
                    "stdout": completed.stdout.strip()[:2_000],
                    "stderr": completed.stderr.strip()[:2_000],
                },
            )


def _require_binary(binary_name: str) -> None:
    """Fail fast when an OCR dependency is missing from the host PATH."""

    if shutil.which(binary_name) is None:
        raise OcrDependencyUnavailableError(missing_binary=binary_name)


__all__ = ["DEFAULT_OCR_TIMEOUT_SECONDS", "OcrDependencyUnavailableError", "OcrRunner"]
