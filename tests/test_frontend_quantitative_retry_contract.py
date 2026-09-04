from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js").read_text(
    encoding="utf-8"
)


def _function_body(name: str, next_name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    end = SOURCE.index(f"function {next_name}(", start)
    return SOURCE[start:end]


def test_complete_attachment_audit_can_retry_incomplete_quantitative_rules() -> None:
    helper = _function_body(
        "quantitativeRuleRetryRequired",
        "invalidateQuantitativeEstimate",
    )
    assert 'rule_source_status).toUpperCase() === "INCOMPLETE"' in helper
    assert 'activation_status).toUpperCase() === "REVIEW_REQUIRED"' in helper

    evaluation_only = _function_body(
        "evaluationOnlyManualAnalysis",
        "manualAnalysisAuthHeaders",
    )
    assert "!quantitativeRuleRetryRequired(notice)" in evaluation_only


def test_manual_action_refreshes_rule_status_before_selecting_retry_mode() -> None:
    request_body = _function_body("requestManualAnalysis", "handleNoticeKeydown")
    refresh = "await loadQuantitativeEstimate(noticeKey, { force: true })"
    availability = "const availability = manualAnalysisAvailability(notice)"
    assert refresh in request_body
    assert request_body.index(refresh) < request_body.index(availability)
    assert '["ANALYZED", "EVALUATED"].includes(notice?.analysisState)' in request_body
    assert "requestBody.retry_reviewed = true" in request_body


def test_quantitative_retry_has_an_operator_visible_label_and_reason() -> None:
    availability = _function_body(
        "manualAnalysisAvailability",
        "manualAnalysisLabel",
    )
    assert 'code: "QUANTITATIVE_RETRY"' in availability
    assert 'label: "정량 근거 재검증"' in availability
    assert "공고별 정량표 근거가 검토 상태" in availability
