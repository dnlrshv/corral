import pytest

from corral.redaction import (
    check_file_text_safe,
    check_outbound_safe,
    redact_text,
    safe_config_diagnostic,
)
from corral.retro.bridge.security import sanitize_text

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
              "tokens_used: int = 0\n"
              "token = b\"\"\n"
              "def publish(token: Token) -> None: ...\n")
    assert check_file_text_safe(python, source_name="orchestrator.py") == []


@pytest.mark.parametrize("line", [
    f'api_key = f"{FAKE}"',
    f"api_key = b'{FAKE}'",
    f'secret = rb"{FAKE}"',
    f'password = u"{FAKE}"',
    f"token = Br'{FAKE}'",
    f'api_key: str = "{FAKE}"',
    f'password: Optional[str] = "{FAKE}"',
    f'api_key: ClassVar[bytes] = b"{FAKE}"',
    f'    token: str = "{FAKE}"  # dataclass default',
])
def test_prefixed_and_annotated_literals_are_redacted_and_refused(line):
    text = line + "\n"
    assert FAKE not in redact_text(text)
    assert FAKE not in sanitize_text(text)
    assert check_outbound_safe(text)
    assert check_file_text_safe(text, source_name="candidate.py")


def test_redacted_literals_keep_their_prefix_and_stay_idempotent():
    redacted = redact_text(f'api_key = rb"{FAKE}"\npassword: str = f"{FAKE}"\n')
    assert redacted == 'api_key = rb"[REDACTED]"\npassword: str = f"[REDACTED]"\n'
    assert redact_text(redacted) == redacted
    assert not check_outbound_safe(redacted)


def test_token_limits_are_counters_but_singular_token_names_are_credentials():
    for text in ("max_tokens: 4096\n", '{"max_output_tokens": 1024, "cached_tokens": 12}',
                 "daily_token_limit: int = 300_000\n"):
        assert redact_text(text) == text
        assert not check_outbound_safe(text)
        assert check_file_text_safe(text, source_name="settings.py") == []
    for text in ("output_token: 8374650192837465019283\n",
                 "deploy_input_token: 8374650192837465019283\n",
                 "input_tokens: 8374650192837465019283\n"):
        assert "8374650192837465019283" not in redact_text(text)
        assert check_outbound_safe(text)
    assert safe_config_diagnostic({"max_tokens": 4096, "output_token": 8374650192837465019283}) == {
        "max_tokens": 4096, "output_token": "[REDACTED]"}


def test_triple_quoted_and_multiline_values_are_redacted_in_place():
    for text in (f'api_key = """{FAKE}"""\n', f"secret: str = rb'''\n{FAKE}\n'''\n"):
        assert FAKE not in redact_text(text)
        assert check_file_text_safe(text, source_name="candidate.py")
    assert redact_text(f'api_key =\n    "{FAKE}"\n') == 'api_key =\n    "[REDACTED]"\n'
    assert redact_text(f"password: |\n  {FAKE}\nother: 1\n") == "password: [REDACTED]\nother: 1\n"


def test_python_block_colon_before_a_docstring_is_not_an_assignment():
    source = 'def load() -> AuthToken:\n    """Load the snapshot."""\n    return AuthToken()\n'
    assert check_file_text_safe(source, source_name="loader.py") == []
    assert check_file_text_safe(f'CONFIG = {{"password":\n    "{FAKE}"}}\n', source_name="loader.py")
