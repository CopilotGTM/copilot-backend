from scripts.security import check_casa_release_publication as publication


def test_release_publication_check_passes_when_required_markers_exist():
    backend_text = "\n".join(publication.BACKEND_REQUIRED_MARKERS.values())
    infra_text = "\n".join(publication.INFRA_REQUIRED_MARKERS.values())

    result = publication.check_publication_texts(backend_text=backend_text, infra_text=infra_text)

    assert result.passed is True
    assert result.failures == []


def test_release_publication_check_reports_missing_backend_and_infra_markers():
    result = publication.check_publication_texts(backend_text="", infra_text="")

    assert result.passed is False
    assert any(
        "backend pipeline missing secrets scan artifact" in failure for failure in result.failures
    )
    assert any(
        "infra pipeline missing Terraform IaC scan artifact" in failure
        for failure in result.failures
    )


def test_release_publication_report_is_operator_friendly():
    result = publication.PublicationCheckResult(
        passed=False,
        failures=["backend pipeline missing secrets scan artifact"],
    )

    report = publication.format_report(result)

    assert report.startswith("CASA release publication check: FAIL")
    assert "- backend pipeline missing secrets scan artifact" in report
