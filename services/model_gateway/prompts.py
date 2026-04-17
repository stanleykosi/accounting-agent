"""
Purpose: Implement the versioned prompt registry for bounded LLM reasoning tasks.
Scope: Centralizes all prompt templates used by the recommendation workflow, including
document classification reasoning, GL coding explanation, journal narrative generation,
and ambiguous mapping ranking. Every prompt carries a version string for audit lineage.
Dependencies: Pydantic for template validation, Python string formatting.

Design notes:
- Prompt versions are first-class audit entities. Every recommendation records which
  prompt version produced it.
- Templates use minimal Python formatting (not a template engine) to keep the dependency
  surface lean and the rendering behavior deterministic.
- The system prompt always enforces: deterministic arithmetic already done, LLM fills
  only bounded reasoning gaps, output must be valid JSON matching the target schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PromptRegistryError(ValueError):
    """Represent a hard failure in prompt template construction."""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """Describe one versioned prompt template with its render metadata."""

    template_id: str
    version: str
    system_prompt: str
    user_prompt_template: str
    description: str
    required_variables: tuple[str, ...] = field(default_factory=tuple)

    def render(self, **variables: Any) -> tuple[str, str]:
        """Render the system and user prompts with the provided variables.

        Args:
            **variables: Key-value pairs for template substitution.

        Returns:
            Tuple of (system_prompt, rendered_user_prompt).

        Raises:
            PromptRegistryError: When a required variable is missing.
        """
        missing = [
            var_name
            for var_name in self.required_variables
            if var_name not in variables
        ]
        if missing:
            formatted_missing = ", ".join(sorted(missing))
            raise PromptRegistryError(
                f"Prompt template '{self.template_id}' v{self.version} "
                f"requires variables: {formatted_missing}."
            )

        try:
            user_prompt = self.user_prompt_template.format(**variables)
        except KeyError as error:
            raise PromptRegistryError(
                f"Prompt template '{self.template_id}' v{self.version} "
                f"references unknown variable {error}."
            ) from error

        return self.system_prompt, user_prompt


# ---------------------------------------------------------------------------
# System prompt constant
# ---------------------------------------------------------------------------

_ACCOUNTING_REASONING_SYSTEM_PROMPT = (
    "You are an accounting reasoning assistant embedded in an enterprise-grade "
    "Accounting AI Agent. Your role is strictly bounded: you provide classification "
    "reasoning, GL coding explanations, and narrative context for recommendations "
    "that have already been pre-processed by deterministic accounting rules.\n\n"
    "IMPORTANT CONSTRAINTS:\n"
    "- All arithmetic, tax computations, totals, and period checks were already "
    "performed by deterministic Python code. NEVER perform math yourself.\n"
    "- You must NEVER output raw model outputs that bypass schema validation.\n"
    "- Your output must be valid JSON that exactly matches the provided schema.\n"
    "- Ground all reasoning in the provided evidence. Do not invent vendor names, "
    "amounts, dates, or account codes that are not present in the input.\n"
    "- If the evidence is insufficient to reach a confident conclusion, state that "
    "explicitly and assign a low confidence score.\n"
    "- The default accounting context is Nigerian SME practice unless a company "
    "chart of accounts has been uploaded or synced from QuickBooks.\n"
    "- Currency defaults to Naira (NGN) unless the document specifies otherwise.\n"
    "- Always respond with ONLY valid JSON. No prose, no markdown fences."
)

_DOCUMENT_EXTRACTION_ASSIST_SYSTEM_PROMPT = (
    "You are a document parsing assistant embedded in an enterprise-grade Accounting AI Agent. "
    "You help classify accounting documents and recover top-level fields from parser text and "
    "OCR output, but you do not replace the deterministic parser.\n\n"
    "IMPORTANT CONSTRAINTS:\n"
    "- Use only the provided parser text, OCR text, and table preview.\n"
    "- Prefer explicitly labeled values over inferred values.\n"
    "- Never derive amounts or dates from document IDs, reference numbers, or unrelated digits.\n"
    "- If a field is ambiguous, omit it instead of guessing.\n"
    "- Return only supported canonical field names.\n"
    "- Your output must be valid JSON that exactly matches the provided schema.\n"
    "- Always respond with ONLY valid JSON. No prose, no markdown fences."
)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

DOCUMENT_CLASSIFICATION_PROMPT = PromptTemplate(
    template_id="document_classification",
    version="1.0.0",
    system_prompt=_ACCOUNTING_REASONING_SYSTEM_PROMPT,
    user_prompt_template=(
        "Classify the following document and provide structured reasoning.\n\n"
        "Document context:\n"
        "- Document type hints from deterministic parser: {document_type_hints}\n"
        "- Extracted text summary: {text_summary}\n"
        "- Key entities found: {key_entities}\n"
        "- Close run period: {period_start} to {period_end}\n\n"
        "Available document types: {available_types}\n\n"
        "Return a JSON object with:\n"
        "- 'predicted_type': one of the available types\n"
        "- 'confidence': a float between 0.0 and 1.0\n"
        "- 'reasoning': a brief explanation of why this type was selected\n"
        "- 'secondary_candidates': list of alternative types with scores"
    ),
    description="Classify an ingested document into a canonical document type.",
    required_variables=(
        "document_type_hints",
        "text_summary",
        "key_entities",
        "period_start",
        "period_end",
        "available_types",
    ),
)

GL_CODING_EXPLANATION_PROMPT = PromptTemplate(
    template_id="gl_coding_explanation",
    version="1.0.0",
    system_prompt=_ACCOUNTING_REASONING_SYSTEM_PROMPT,
    user_prompt_template=(
        "Provide structured reasoning for a GL coding recommendation.\n\n"
        "Context:\n"
        "- Document type: {document_type}\n"
        "- Vendor: {vendor_name}\n"
        "- Amount: {amount} {currency}\n"
        "- Deterministic rule matched: {deterministic_rule}\n"
        "- Suggested account: {account_code} - {account_name} ({account_type})\n"
        "- Chart of accounts source: {coa_source}\n\n"
        "Extracted line items: {line_items}\n\n"
        "Return a JSON object with:\n"
        "- 'confidence': float between 0.0 and 1.0\n"
        "- 'reasoning_summary': concise explanation for why this account was selected\n"
        "- 'risk_factors': list of risk signals (e.g., missing PO, unusual amount)\n"
        "- 'alternative_accounts': list of plausible alternative account codes"
    ),
    description=(
        "Explain why a deterministic GL coding rule selected a particular account "
        "and surface any risk signals that warrant human review."
    ),
    required_variables=(
        "document_type",
        "vendor_name",
        "amount",
        "currency",
        "deterministic_rule",
        "account_code",
        "account_name",
        "account_type",
        "coa_source",
        "line_items",
    ),
)

AMBIGUOUS_MAPPING_RANKING_PROMPT = PromptTemplate(
    template_id="ambiguous_mapping_ranking",
    version="1.0.0",
    system_prompt=_ACCOUNTING_REASONING_SYSTEM_PROMPT,
    user_prompt_template=(
        "Rank candidate COA accounts for an ambiguous transaction.\n\n"
        "Transaction context:\n"
        "- Description: {description}\n"
        "- Amount: {amount} {currency}\n"
        "- Document type: {document_type}\n"
        "- Vendor: {vendor_name}\n\n"
        "Candidate accounts: {candidate_accounts}\n\n"
        "Return a JSON object with:\n"
        "- 'rankings': list of objects with 'account_code', 'score' (0-1), and 'reason'\n"
        "- 'top_recommendation': the account_code with the highest score\n"
        "- 'confidence': overall confidence in the top recommendation (0-1)"
    ),
    description=(
        "When deterministic rules cannot select a single account, rank the "
        "candidate accounts from the active COA set."
    ),
    required_variables=(
        "description",
        "amount",
        "currency",
        "document_type",
        "vendor_name",
        "candidate_accounts",
    ),
)

JOURNAL_NARRATIVE_PROMPT = PromptTemplate(
    template_id="journal_narrative",
    version="1.0.0",
    system_prompt=_ACCOUNTING_REASONING_SYSTEM_PROMPT,
    user_prompt_template=(
        "Write a concise, professional journal entry description.\n\n"
        "Journal context:\n"
        "- Entry type: {entry_type}\n"
        "- Period: {period_start} to {period_end}\n"
        "- Source documents: {source_documents}\n"
        "- Total debit: {total_debit}\n"
        "- Total credit: {total_credit}\n"
        "- Currency: {currency}\n"
        "- Key accounts involved: {accounts_involved}\n\n"
        "Return a JSON object with:\n"
        "- 'description': a one-to-two-sentence description suitable for the journal header\n"
        "- 'memo_lines': list of brief memo notes for individual line items"
    ),
    description=(
        "Generate a human-readable description for a draft journal entry "
        "that will be presented to reviewers."
    ),
    required_variables=(
        "entry_type",
        "period_start",
        "period_end",
        "source_documents",
        "total_debit",
        "total_credit",
        "currency",
        "accounts_involved",
    ),
)

COMMENTARY_ENHANCE_PROMPT = PromptTemplate(
    template_id="commentary_enhance",
    version="1.0.0",
    system_prompt=(
        "You are a financial reporting assistant embedded in an enterprise-grade "
        "Accounting AI Agent. Your role is to enhance draft management commentary "
        "for clarity, professionalism, and readability. You must NOT invent new "
        "numbers, facts, or conclusions — only improve the writing quality.\n\n"
        "IMPORTANT CONSTRAINTS:\n"
        "- Preserve all numerical values and conclusions from the draft.\n"
        "- Do not add new metrics, ratios, or financial figures not present in the draft.\n"
        "- Maintain a professional, objective tone suitable for CFO and management review.\n"
        "- Keep the commentary concise — remove redundancy and tighten phrasing.\n"
        "- Return ONLY the enhanced commentary text. No JSON, no markdown fences, no preamble."
    ),
    user_prompt_template=(
        "Enhance the following draft management commentary for the {section_key} section "
        "of the financial report for {entity_name} ({period_start} to {period_end}).\n\n"
        "Draft commentary:\n{draft_commentary}\n\n"
        "Return the enhanced commentary text only."
    ),
    description=(
        "Enhance draft management commentary for professional presentation quality "
        "without altering financial conclusions or numerical values."
    ),
    required_variables=(
        "entity_name",
        "period_start",
        "period_end",
        "section_key",
        "draft_commentary",
    ),
)

DOCUMENT_PARSE_ASSIST_PROMPT = PromptTemplate(
    template_id="document_parse_assist",
    version="1.0.0",
    system_prompt=_DOCUMENT_EXTRACTION_ASSIST_SYSTEM_PROMPT,
    user_prompt_template=(
        "Review the parsed accounting document below and return bounded classification plus "
        "top-level field candidates.\n\n"
        "Document context:\n"
        "- Filename: {filename}\n"
        "- OCR required: {ocr_required}\n"
        "- Deterministic document type: {deterministic_type}\n"
        "- Deterministic classification confidence: {deterministic_confidence}\n"
        "- Close run period: {period_start} to {period_end}\n"
        "- Current deterministic field hints: {current_field_hints}\n\n"
        "Available document types: {available_types}\n"
        "Canonical field names by document type: {field_name_reference}\n\n"
        "Parsed text excerpt:\n{text_excerpt}\n\n"
        "Parsed table preview:\n{table_preview}\n\n"
        "Return a JSON object with:\n"
        "- 'predicted_type': one available document type\n"
        "- 'classification_confidence': float 0.0-1.0\n"
        "- 'classification_reasoning': brief explanation grounded in the evidence\n"
        "- 'field_candidates': list of field candidates with canonical field_name, value, "
        "confidence, and evidence_quote\n\n"
        "Only include field candidates that are directly supported by the evidence."
    ),
    description=(
        "Assist document parsing by improving classification confidence and recovering "
        "top-level fields from low-structure parser text."
    ),
    required_variables=(
        "filename",
        "ocr_required",
        "deterministic_type",
        "deterministic_confidence",
        "period_start",
        "period_end",
        "current_field_hints",
        "available_types",
        "field_name_reference",
        "text_excerpt",
        "table_preview",
    ),
)


# ---------------------------------------------------------------------------
# Registry lookup
# ---------------------------------------------------------------------------

_PROMPT_REGISTRY: dict[str, PromptTemplate] = {
    template.template_id: template
    for template in (
        DOCUMENT_CLASSIFICATION_PROMPT,
        DOCUMENT_PARSE_ASSIST_PROMPT,
        GL_CODING_EXPLANATION_PROMPT,
        AMBIGUOUS_MAPPING_RANKING_PROMPT,
        JOURNAL_NARRATIVE_PROMPT,
        COMMENTARY_ENHANCE_PROMPT,
    )
}


def get_prompt_template(template_id: str) -> PromptTemplate:
    """Look up a prompt template by its canonical identifier.

    Args:
        template_id: The template_id string (e.g., 'document_classification').

    Returns:
        The matching PromptTemplate instance.

    Raises:
        PromptRegistryError: When the template_id is unknown.
    """
    template = _PROMPT_REGISTRY.get(template_id)
    if template is None:
        known_ids = ", ".join(sorted(_PROMPT_REGISTRY.keys()))
        raise PromptRegistryError(
            f"Unknown prompt template '{template_id}'. Known IDs: {known_ids}."
        )
    return template


def list_prompt_templates() -> tuple[PromptTemplate, ...]:
    """Return all registered prompt templates in declaration order."""
    return tuple(_PROMPT_REGISTRY.values())


__all__ = [
    "AMBIGUOUS_MAPPING_RANKING_PROMPT",
    "COMMENTARY_ENHANCE_PROMPT",
    "DOCUMENT_CLASSIFICATION_PROMPT",
    "DOCUMENT_PARSE_ASSIST_PROMPT",
    "GL_CODING_EXPLANATION_PROMPT",
    "JOURNAL_NARRATIVE_PROMPT",
    "PromptRegistryError",
    "PromptTemplate",
    "get_prompt_template",
    "list_prompt_templates",
]
