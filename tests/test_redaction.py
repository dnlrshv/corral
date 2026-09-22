from corral.redaction import check_file_text_safe


def test_source_scanner_allows_nonsecret_fencing_references_only_in_python():
    text = "fencing_token: FencingEpoch\nfencing_token = 42\n"
    assert check_file_text_safe(text, source_name="orchestrator.py") == []
    assert check_file_text_safe('fencing_token = "opaque-value"\n',
                                source_name="orchestrator.py")
    assert check_file_text_safe("fencing_token: FencingEpoch\n", source_name="config.yaml")


def test_source_scanner_allows_github_workflow_references_not_literal_values():
    workflow = ("token: ${{ github.token }}\n"
                "private-key: ${{ secrets.DEPLOY_KEY }}\n"
                "gh_token: ${{ matrix.github-token }}\n"
                "token: write\n"
                "token = auth.get('tokens', {}).get('access_token', '')\n")
    assert check_file_text_safe(workflow, source_name=".github/workflows/review.yml") == []
    assert check_file_text_safe("token: opaque-value\n",
                                source_name=".github/workflows/review.yml")
    assert check_file_text_safe("api_key=opaque-value\n", source_name="notes.md")
