"""
Purpose: Verify fail-fast runtime checks that guard worker startup in hosted deployments.
Scope: OCR binary discovery and required Tesseract language-pack validation.
Dependencies: runtime_checks helpers only.
"""

from __future__ import annotations

import subprocess

import pytest
import services.common.runtime_checks as runtime_checks_module


def test_verify_ocr_runtime_requires_binaries(monkeypatch) -> None:
    """Missing OCR binaries should fail fast with a recovery-oriented message."""

    monkeypatch.setattr(
        runtime_checks_module.shutil,
        "which",
        lambda name: None if name == "ocrmypdf" else f"/usr/bin/{name}",
    )

    with pytest.raises(RuntimeError) as error:
        runtime_checks_module.verify_ocr_runtime()

    assert "ocrmypdf" in str(error.value)


def test_verify_ocr_runtime_requires_language_packs(monkeypatch) -> None:
    """Worker startup should reject Tesseract installs without the expected language packs."""

    monkeypatch.setattr(
        runtime_checks_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=["tesseract", "--list-langs"],
            returncode=0,
            stdout="List of available languages in /usr/share/tesseract-ocr:\neng\n",
            stderr="",
        )

    monkeypatch.setattr(runtime_checks_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as error:
        runtime_checks_module.verify_ocr_runtime()

    assert "osd" in str(error.value)


def test_verify_ocr_runtime_accepts_expected_language_packs(monkeypatch) -> None:
    """OCR runtime validation should pass when binaries and required languages exist."""

    monkeypatch.setattr(
        runtime_checks_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=["tesseract", "--list-langs"],
            returncode=0,
            stdout=(
                "List of available languages in /usr/share/tesseract-ocr:\n"
                "eng\n"
                "osd\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(runtime_checks_module.subprocess, "run", fake_run)

    runtime_checks_module.verify_ocr_runtime()
