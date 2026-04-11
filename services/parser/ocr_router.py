"""
Purpose: Decide whether PDF sources should be routed through OCR before final parsing.
Scope: Deterministic OCR routing based on intake flags and digital text density,
runner invocation, and explicit decision metadata for parser audit payloads.
Dependencies: Parser-domain models, PDF parser metadata, and the OCR runner boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from services.parser.models import OcrExecutionResult, ParserResult
from services.parser.ocr_runner import OcrRunner
from services.parser.pdf_parser import MIN_DIGITAL_TEXT_CHARACTERS_PER_PAGE


class OcrRoutingDecision(BaseModel):
    """Describe the deterministic OCR routing decision for one PDF document."""

    model_config = ConfigDict(extra="forbid")

    should_run_ocr: bool = Field()
    reason: str = Field(min_length=1)
    digital_text_characters: int = Field(ge=0)
    text_density_per_page: float = Field(ge=0.0)


class OcrRouter:
    """Apply OCR routing policy and invoke the configured OCR runner only when required."""

    def __init__(self, *, runner: OcrRunner | None = None) -> None:
        """Capture the OCR runner dependency used for scanned PDFs."""

        self._runner = runner or OcrRunner()

    def decide(
        self,
        *,
        parse_result: ParserResult,
        intake_ocr_required: bool,
    ) -> OcrRoutingDecision:
        """Return whether OCR should run for the initial PDF parse result."""

        metadata_value = parse_result.metadata.get("digital_text_characters", 0)
        digital_text_characters = metadata_value if isinstance(metadata_value, int) else 0
        page_count = max(parse_result.page_count or 1, 1)
        text_density = digital_text_characters / page_count
        if intake_ocr_required:
            return OcrRoutingDecision(
                should_run_ocr=True,
                reason="Upload MIME sniffing flagged the PDF as likely scanned.",
                digital_text_characters=digital_text_characters,
                text_density_per_page=round(text_density, 2),
            )
        if text_density < MIN_DIGITAL_TEXT_CHARACTERS_PER_PAGE:
            return OcrRoutingDecision(
                should_run_ocr=True,
                reason="Digital PDF text extraction produced too little text for review.",
                digital_text_characters=digital_text_characters,
                text_density_per_page=round(text_density, 2),
            )

        return OcrRoutingDecision(
            should_run_ocr=False,
            reason="Digital PDF text density is sufficient; OCR is not required.",
            digital_text_characters=digital_text_characters,
            text_density_per_page=round(text_density, 2),
        )

    def run_if_required(
        self,
        *,
        payload: bytes,
        filename: str,
        initial_parse_result: ParserResult,
        intake_ocr_required: bool,
    ) -> tuple[OcrRoutingDecision, OcrExecutionResult | None]:
        """Run OCR only when the deterministic routing decision requires it."""

        decision = self.decide(
            parse_result=initial_parse_result,
            intake_ocr_required=intake_ocr_required,
        )
        if not decision.should_run_ocr:
            return decision, None

        return decision, self._runner.run_pdf_ocr(payload=payload, filename=filename)


__all__ = ["OcrRouter", "OcrRoutingDecision"]
