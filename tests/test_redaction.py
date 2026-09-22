import pytest

from corral.redaction import check_file_text_safe

WORKFLOW = ".github/workflows/review.yml"
FAKE = "NotARealSecretValue"


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


@pytest.mark.parametrize("source_name,text", [
    # Quoted literals inside a run-block call are data, not lookups.
    (WORKFLOW, f'token = auth.get_token("{FAKE}")\n'),
    (WORKFLOW, f"token = os.environ.get('GH_TOKEN', '{FAKE}')\n"),
    (WORKFLOW, f'token = auth.get("tokens", "{FAKE}")\n'),
    (WORKFLOW, f"password = base64.b64decode('{FAKE}')\n"),
    (WORKFLOW, f"api_key = str('{FAKE}').strip()\n"),
    # Expression syntax is a reference only for GitHub contexts.
    (WORKFLOW, f"password: ${{{{ {FAKE} }}}}\n"),
    (WORKFLOW, "api_key: ${{ hunter2-literal }}\n"),
    # Workflow exemptions apply only to top-level workflow files.
    (".github/workflows/nested/review.yml",
     "token = auth.get('tokens', {}).get('access_token', '')\n"),
    ("docs/review.yml", "token: ${{ github.token }}\n"),
    # Python ':' is an annotation only for known or capitalized type names.
    ("orchestrator.py", "password: hunter2\n"),
    ("orchestrator.py", f"# password: {FAKE}\n"),
    ("orchestrator.py", '"""Example configuration.\n\n    api_key: hunter2\n"""\n'),
    ("orchestrator.py", 'def load():\n    """Read settings."""\n    secret: hunter2\n'),
    ("orchestrator.py", '# {"password": hunter2}\n'),
    # Fencing epochs are unquoted integers only.
    ("orchestrator.py", 'fencing_token = "42"\n'),
])
def test_source_scanner_refuses_reference_lookalikes(source_name, text):
    assert check_file_text_safe(text, source_name=source_name)


def test_source_scanner_keeps_workflow_context_and_type_references():
    workflow = ("token: ${{ secrets.GITHUB_TOKEN }}\n"
                "api_key: ${{ inputs.api_key }}\n"
                "password: ${{ env.PASSWORD }}\n"
                "secret: ${{ vars.SECRET_NAME }}\n"
                "token: ${{ steps.app-token.outputs.token }}\n"
                "token = payload.get('token')\n"
                "token = auth.get(\"tokens\", {}).get(\"access_token\", None)\n")
    assert check_file_text_safe(workflow, source_name=".github/workflows/merge.yaml") == []
    python = ("token: str\n"
              "api_key: SecretStr\n"
              "fencing_token: FencingEpoch\n"
              "record = {\"fencing_token\": fencing_token}\n"
              "def publish(token: Token) -> None: ...\n")
    assert check_file_text_safe(python, source_name="orchestrator.py") == []
