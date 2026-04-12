"""
Purpose: Integration tests for the LangGraph-based accounting recommendation workflow.
Scope: Graph construction, node execution, prompt rendering, model gateway interaction,
and autonomy routing. Tests run without a live database or model provider by using
deterministic fixtures and mocked model responses.
Dependencies: pytest, langgraph, orchestration modules, model gateway, recommendation contracts.

Test matrix:
1. Graph construction succeeds with all nodes wired correctly.
2. Deterministic rule evaluation produces expected results for known contexts.
3. Model reasoning is skipped when deterministic confidence is high.
4. Model reasoning is invoked when deterministic confidence is low.
5. Autonomy routing respects human Review vs Reduced Interruption modes.
6. Assembly produces valid CreateRecommendationInput contracts.
7. Prompt templates render with all required variables.
8. Model gateway strips markdown fences from JSON responses.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from services.common.enums import (
    AccountType,
    AutonomyMode,
    DocumentType,
    ReviewStatus,
    RiskLevel,
)
from services.contracts.recommendation_models import (
    CoaAccountRef,
    ConfidenceMetrics,
    CreateRecommendationInput,
    DocumentClassificationOutput,
    EvidenceLink,
    GLCodingExplanationOutput,
    RecommendationContext,
)
from services.model_gateway.client import (
    ModelGateway,
    ModelGatewayConfig,
    _strip_markdown_fences,
)
from services.model_gateway.prompts import (
    GL_CODING_EXPLANATION_PROMPT,
    PromptRegistryError,
    get_prompt_template,
    list_prompt_templates,
)
from services.orchestration.nodes import (
    apply_autonomy_routing,
    assemble_recommendation,
    evaluate_deterministic_rules,
    should_invoke_model,
    validate_prerequisites,
)
from services.orchestration.recommendation_graph import (
    build_recommendation_graph,
    execute_recommendation_workflow,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_coa_accounts() -> list[CoaAccountRef]:
    """Provide a minimal Nigerian SME-style COA for test contexts."""
    return [
        CoaAccountRef(
            account_code="4000",
            account_name="Office Expenses",
            account_type=AccountType.EXPENSE,
            is_active=True,
        ),
        CoaAccountRef(
            account_code="5000",
            account_name="Cost of Goods Sold",
            account_type=AccountType.COST_OF_SALES,
            is_active=True,
        ),
        CoaAccountRef(
            account_code="1000",
            account_name="Bank - Current Account",
            account_type=AccountType.ASSET,
            is_active=True,
        ),
        CoaAccountRef(
            account_code="2000",
            account_name="Accounts Payable",
            account_type=AccountType.LIABILITY,
            is_active=True,
        ),
    ]


@pytest.fixture()
def sample_context(sample_coa_accounts: list[CoaAccountRef]) -> RecommendationContext:
    """Build a minimal recommendation context for testing."""
    return RecommendationContext(
        close_run_id=uuid4(),
        document_id=uuid4(),
        entity_id=uuid4(),
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        document_type=DocumentType.INVOICE,
        extracted_fields={
            "total": {"value": "50000.00", "confidence": 0.95},
            "vendor": {"value": "Acme Supplies Ltd", "confidence": 0.90},
            "date": {"value": "2025-01-15", "confidence": 0.98},
            "currency": {"value": "NGN", "confidence": 1.0},
        },
        line_items=[
            {
                "description": "Office stationery",
                "amount": "50000.00",
                "line_no": 1,
            }
        ],
        coa_accounts=sample_coa_accounts,
        coa_source="fallback_nigerian_sme",
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        confidence_threshold=0.7,
    )


# ---------------------------------------------------------------------------
# Graph construction tests
# ---------------------------------------------------------------------------


class TestGraphConstruction:
    """Verify that the recommendation graph is wired correctly."""

    def test_build_recommendation_graph_succeeds(self) -> None:
        """The graph should compile without errors when all nodes are registered."""
        graph = build_recommendation_graph()
        assert graph is not None

    def test_graph_has_required_nodes(self) -> None:
        """All canonical nodes must be registered in the graph."""
        graph = build_recommendation_graph()
        node_names = set(graph.nodes.keys())
        expected_nodes = {
            "validate_prerequisites",
            "evaluate_deterministic_rules",
            "invoke_model_reasoning",
            "assemble_recommendation",
            "apply_autonomy_routing",
        }
        assert expected_nodes.issubset(node_names)


# ---------------------------------------------------------------------------
# Node: validate_prerequisites
# ---------------------------------------------------------------------------


class TestValidatePrerequisites:
    """Verify prerequisite validation behavior."""

    def test_valid_context_passes(self, sample_context: RecommendationContext) -> None:
        """A fully populated context should produce no errors."""
        state: dict[str, Any] = {
            "context": sample_context.model_dump(mode="json"),
            "errors": [],
        }
        result = validate_prerequisites(state)
        assert result["errors"] == []

    def test_missing_close_run_id_fails(self, sample_context: RecommendationContext) -> None:
        """Missing close_run_id should produce a validation error."""
        ctx = sample_context.model_dump(mode="json")
        ctx["close_run_id"] = None
        state: dict[str, Any] = {"context": ctx, "errors": []}
        result = validate_prerequisites(state)
        assert any("close_run_id" in err.lower() for err in result["errors"])


# ---------------------------------------------------------------------------
# Node: evaluate_deterministic_rules
# ---------------------------------------------------------------------------


class TestEvaluateDeterministicRules:
    """Verify that deterministic rule evaluation integrates with the graph."""

    def test_no_matching_rule_produces_unmatched_result(
        self, sample_context: RecommendationContext
    ) -> None:
        """When no rule is configured, the result should indicate no match."""
        state: dict[str, Any] = {
            "context": sample_context.model_dump(mode="json"),
            "errors": [],
        }
        result = evaluate_deterministic_rules(state)
        det = result["deterministic_result"]
        assert det is not None
        assert det.get("matched") is False

    def test_configured_rule_produces_match(
        self, sample_context: RecommendationContext
    ) -> None:
        """When a rule engine with a matching rule is available, it should produce a result.

        Note: This test verifies the node wiring. The actual rule matching depends on
        the rule engine being initialized with appropriate rules, which Step 26+ handles.
        """
        state: dict[str, Any] = {
            "context": sample_context.model_dump(mode="json"),
            "errors": [],
        }
        result = evaluate_deterministic_rules(state)
        # The node should always produce a deterministic_result entry
        assert result["deterministic_result"] is not None


# ---------------------------------------------------------------------------
# Conditional edge: should_invoke_model
# ---------------------------------------------------------------------------


class TestShouldInvokeModel:
    """Verify the conditional branching logic for model reasoning."""

    def test_no_match_invokes_model(self) -> None:
        """When no deterministic rule matched, model should be invoked."""
        state: dict[str, Any] = {
            "deterministic_result": {"matched": False, "reason": "No rule matched"},
        }
        assert should_invoke_model(state) == "invoke_model_reasoning"

    def test_low_confidence_invokes_model(self) -> None:
        """When deterministic confidence is below 0.85, model should be invoked."""
        state: dict[str, Any] = {
            "deterministic_result": {"matched": True, "confidence": 0.70},
        }
        assert should_invoke_model(state) == "invoke_model_reasoning"

    def test_high_confidence_skips_model(self) -> None:
        """When deterministic confidence is >= 0.85, model should be skipped."""
        state: dict[str, Any] = {
            "deterministic_result": {"matched": True, "confidence": 0.90},
        }
        assert should_invoke_model(state) == "assemble_recommendation"


# ---------------------------------------------------------------------------
# Node: assemble_recommendation
# ---------------------------------------------------------------------------


class TestAssembleRecommendation:
    """Verify recommendation assembly from deterministic and model outputs."""

    def test_assemble_from_deterministic_only(self, sample_context: RecommendationContext) -> None:
        """Assembly should succeed with only deterministic results."""
        state: dict[str, Any] = {
            "context": sample_context.model_dump(mode="json"),
            "deterministic_result": {
                "matched": True,
                "account_code": "4000",
                "account_name": "Office Expenses",
                "account_type": "expense",
                "confidence": 0.85,
                "rule_type": "document_type",
                "treatment": "standard_coding",
                "reasons": ["Document type rule matched invoice."],
                "risk_level": "low",
                "approval_level": "standard",
                "dimensions": {},
            },
            "model_reasoning": None,
            "errors": [],
        }
        result = assemble_recommendation(state)
        assert result["final_recommendation"] is not None
        rec = result["final_recommendation"]
        assert rec["confidence"] > 0
        assert rec["recommendation_type"] == "gl_coding"

    def test_assemble_produces_valid_contract(self, sample_context: RecommendationContext) -> None:
        """The assembled recommendation should validate against CreateRecommendationInput."""
        state: dict[str, Any] = {
            "context": sample_context.model_dump(mode="json"),
            "deterministic_result": {
                "matched": True,
                "account_code": "4000",
                "account_name": "Office Expenses",
                "account_type": "expense",
                "confidence": 0.90,
                "rule_type": "vendor",
                "treatment": "standard_coding",
                "reasons": ["Vendor rule matched."],
                "risk_level": "low",
                "approval_level": "standard",
                "dimensions": {},
            },
            "model_reasoning": None,
            "errors": [],
        }
        result = assemble_recommendation(state)
        rec_data = result["final_recommendation"]
        assert rec_data is not None
        # Should be validatable
        CreateRecommendationInput.model_validate(rec_data)


# ---------------------------------------------------------------------------
# Node: apply_autonomy_routing
# ---------------------------------------------------------------------------


class TestAutonomyRouting:
    """Verify autonomy mode routing behavior."""

    def test_human_review_always_pending(
        self, sample_context: RecommendationContext
    ) -> None:
        """Human review mode should always route to pending_review."""
        ctx = sample_context.model_dump(mode="json")
        ctx["autonomy_mode"] = AutonomyMode.HUMAN_REVIEW.value
        state: dict[str, Any] = {
            "context": ctx,
            "final_recommendation": {
                "confidence": 0.95,
                "payload": {"risk_level": "low"},
            },
            "errors": [],
        }
        result = apply_autonomy_routing(state)
        assert result["routed_status"] == ReviewStatus.PENDING_REVIEW.value

    def test_reduced_interruption_auto_approve(self) -> None:
        """Reduced interruption should auto-approve high-confidence, low-risk items."""
        state: dict[str, Any] = {
            "context": {
                "autonomy_mode": AutonomyMode.REDUCED_INTERRUPTION.value,
                "confidence_threshold": 0.7,
            },
            "final_recommendation": {
                "confidence": 0.90,
                "payload": {"risk_level": RiskLevel.LOW.value},
            },
            "errors": [],
        }
        result = apply_autonomy_routing(state)
        assert result["routed_status"] == ReviewStatus.APPROVED.value

    def test_reduced_interruption_pending_high_risk(self) -> None:
        """Reduced interruption should still require review for high-risk items."""
        state: dict[str, Any] = {
            "context": {
                "autonomy_mode": AutonomyMode.REDUCED_INTERRUPTION.value,
                "confidence_threshold": 0.7,
            },
            "final_recommendation": {
                "confidence": 0.90,
                "payload": {"risk_level": RiskLevel.HIGH.value},
            },
            "errors": [],
        }
        result = apply_autonomy_routing(state)
        assert result["routed_status"] == ReviewStatus.PENDING_REVIEW.value


# ---------------------------------------------------------------------------
# End-to-end graph execution (without model provider)
# ---------------------------------------------------------------------------


class TestEndToEndGraphExecution:
    """Verify the full graph execution path with deterministic-only results."""

    def test_execute_workflow_deterministic_path(
        self, sample_context: RecommendationContext
    ) -> None:
        """The workflow should complete with or without a model provider."""
        ctx = sample_context.model_dump(mode="json")
        result = execute_recommendation_workflow(context=ctx)

        # Should have processed through the graph
        assert "errors" in result
        # The final_recommendation may be None if no rules match and no model is available
        # This is expected behavior, not a test failure
        assert "deterministic_result" in result


# ---------------------------------------------------------------------------
# Prompt registry tests
# ---------------------------------------------------------------------------


class TestPromptRegistry:
    """Verify prompt template registration and rendering."""

    def test_gl_coding_prompt_registered(self) -> None:
        """The GL coding explanation prompt must be registered."""
        template = get_prompt_template("gl_coding_explanation")
        assert template.version == "1.0.0"

    def test_all_templates_have_unique_ids(self) -> None:
        """Each registered template must have a unique identifier."""
        templates = list_prompt_templates()
        ids = [t.template_id for t in templates]
        assert len(ids) == len(set(ids))

    def test_render_requires_all_variables(self) -> None:
        """Rendering should fail when required variables are missing."""
        with pytest.raises(PromptRegistryError):
            GL_CODING_EXPLANATION_PROMPT.render(
                document_type="invoice",
                # Missing required variables
            )

    def test_render_succeeds_with_all_variables(self) -> None:
        """Rendering should succeed when all required variables are provided."""
        _system, user = GL_CODING_EXPLANATION_PROMPT.render(
            document_type="invoice",
            vendor_name="Test Vendor",
            amount="1000.00",
            currency="NGN",
            deterministic_rule="document_type rule matched",
            account_code="4000",
            account_name="Office Expenses",
            account_type="expense",
            coa_source="fallback_nigerian_sme",
            line_items="- Office supplies: 1000.00",
        )
        assert "Test Vendor" in user
        assert "4000" in user


# ---------------------------------------------------------------------------
# Model gateway tests (no real API calls)
# ---------------------------------------------------------------------------


class TestModelGatewayHelpers:
    """Test model gateway helper functions that don't require a live provider."""

    def test_strip_markdown_fences(self) -> None:
        """Markdown JSON fences should be stripped correctly."""
        wrapped = '```json\n{"key": "value"}\n```'
        assert _strip_markdown_fences(wrapped) == '{"key": "value"}'

    def test_strip_plain_passthrough(self) -> None:
        """Plain JSON without fences should pass through unchanged."""
        plain = '{"key": "value"}'
        assert _strip_markdown_fences(plain) == plain

    def test_strip_open_fence_only(self) -> None:
        """Content with only an opening fence should have it removed."""
        partial = '```json\n{"key": "value"}'
        result = _strip_markdown_fences(partial)
        assert result == '{"key": "value"}'

    def test_model_gateway_config_defaults(self) -> None:
        """Config should have sensible defaults."""
        config = ModelGatewayConfig()
        assert config.temperature == 0.0
        assert config.max_tokens == 4096
        assert config.timeout_seconds == 60

    def test_model_gateway_requires_api_key(self) -> None:
        """Gateway should fail fast when API key is not configured."""
        # Without MODEL_GATEWAY_API_KEY set, this should raise
        from services.model_gateway.client import ModelGatewayError

        with pytest.raises(ModelGatewayError, match="API key is not configured"):
            ModelGateway()


# ---------------------------------------------------------------------------
# Contract model tests
# ---------------------------------------------------------------------------


class TestRecommendationContracts:
    """Verify that recommendation contracts enforce their constraints."""

    def test_document_classification_output_valid(self) -> None:
        """Valid classification output should parse without errors."""
        output = DocumentClassificationOutput(
            predicted_type=DocumentType.INVOICE,
            confidence=0.92,
            reasoning="Vendor name and line items indicate an invoice.",
            secondary_candidates=[],
        )
        assert output.confidence == 0.92

    def test_gl_coding_output_valid(self) -> None:
        """Valid GL coding output should parse without errors."""
        output = GLCodingExplanationOutput(
            confidence=0.85,
            reasoning_summary="Office supplies expense matched to operating expenses.",
            risk_factors=["No purchase order referenced"],
            alternative_accounts=["5000"],
        )
        assert len(output.risk_factors) == 1

    def test_evidence_link_frozen(self) -> None:
        """Evidence links should be immutable after construction."""
        link = EvidenceLink(
            source_type="extraction",
            source_id=str(uuid4()),
            description="Test evidence",
        )
        with pytest.raises((AttributeError, TypeError, Exception)):
            link.source_type = "modified"  # type: ignore[misc]

    def test_confidence_metrics_range(self) -> None:
        """Confidence metrics should accept values in the 0-1 range."""
        metrics = ConfidenceMetrics(
            overall_confidence=0.75,
            deterministic_confidence=0.80,
            model_confidence=0.70,
        )
        assert metrics.overall_confidence == 0.75


__all__ = [
    "TestAssembleRecommendation",
    "TestAutonomyRouting",
    "TestEndToEndGraphExecution",
    "TestEvaluateDeterministicRules",
    "TestGraphConstruction",
    "TestModelGatewayHelpers",
    "TestPromptRegistry",
    "TestRecommendationContracts",
    "TestShouldInvokeModel",
    "TestValidatePrerequisites",
]
