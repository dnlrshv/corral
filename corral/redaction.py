"""Robust redaction boundaries for error diagnostics, configs, and retro evidence."""

from __future__ import annotations

import re
from typing import Any

_CREDENTIAL_KEY = (
    r"[A-Za-z0-9_]*(?:access[_-]?token|api[_-]?key|auth[_-]?token|token|secret|password|passwd|"
    r"private[_-]?key|database[_-]?url|db[_-]?url|dsn|"
    r"connection[_-]?(?:string|uri|url))[A-Za-z0-9_]*"
)

# Real numeric token counter keys: ``*_tokens`` (including ``max_tokens``) or
# ``*_token_count/_usage/_limit/_budget``; the bounded value below is what keeps an
# opaque number under such a name from passing as a counter.
_SAFE_KEYS = re.compile(
    r"(?i)^[\"']?(?:"
    r"(?:[a-z0-9_]*_)?(?:input|output|total|candidate|candidates|cache(?:_read|_write)?|cached|"
    r"cached_?content|thinking|thoughts?|reasoning|prompt|completion|estimated|max)[_-]?tokens?|"
    r"(?:[a-z0-9_]*?[_-]?)?tokens?[_-]?(?:count|usage|limit|budget)"
    r")[\"']?$"
)
# A counter is a bounded number (``300_000`` grouping allowed); 20 digits are opaque.
_COUNTER_VALUE = re.compile(r"^-?\d(?:_?\d){0,11}(?:\.\d+)?$")

# Python string literal prefixes (``f``, ``b``, ``r``, ``u``, ``rb``, ``br``, ``rf``, ``fr``).
_STRING_PREFIX = r"(?:[rR][bBfF]?|[bBfF][rR]?|[uU])"
_QUOTED_VALUE = re.compile(
    rf"(?P<prefix>{_STRING_PREFIX}?)(?P<q>\"\"\"|'''|[\"'])(?P<body>.*)(?P=q)", re.DOTALL)
# ``key: Annotation = value`` binds ``value``; the annotation is never the value. At
# the top level an annotation is ``|``-joined dotted names, each optionally quoted and
# subscripted, so ``password: value, user=bob`` binds ``value``. Commas, strings and
# calls appear only inside a subscript, and blanks only there or around ``|``.
# Subscripts nest four levels (``Optional[dict[str, tuple[int, list[str]]]]``); call
# arguments nest two levels and may quote parentheses: ``Annotated[str, Field(
# pattern="^(x)$", default_factory=lambda: env.get("X"))] = value``.
_ANNOTATION_STRING = r"\"[^\"\n]*\"|'[^'\n]*'"
_ANNOTATION_CALL = (rf"\((?:[^()\"'\n]|{_ANNOTATION_STRING}"
                    rf"|\((?:[^()\"'\n]|{_ANNOTATION_STRING})*\))*\)")
_ANNOTATION_ITEM = rf"[\w.,| \t]|{_ANNOTATION_STRING}|{_ANNOTATION_CALL}"
_ANNOTATION_SUBSCRIPT = rf"\[(?:{_ANNOTATION_ITEM})*\]"
for _ in range(3):
    _ANNOTATION_SUBSCRIPT = rf"\[(?:{_ANNOTATION_ITEM}|{_ANNOTATION_SUBSCRIPT})*\]"
_ANNOTATION_ATOM = rf"[\"']?[A-Za-z_][\w.]*(?:{_ANNOTATION_SUBSCRIPT})?[\"']?"
_ANNOTATION = rf"{_ANNOTATION_ATOM}(?:[ \t]*\|[ \t]*{_ANNOTATION_ATOM})*[ \t]*"
_ASSIGNMENT = (
    rf"(?i)(?<![A-Za-z0-9_])(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)"
    rf"(?P<sep_space>\s*(?::[ \t]*{_ANNOTATION}(?==))?(?P<sep>[:=])\s*)"
    r"(?![\"']?(?:<redacted>|\[REDACTED\])[\"']?)"
    rf"(?P<val>{_STRING_PREFIX}?(?:\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?''')"
    rf"|{_STRING_PREFIX}?\"[^\"]*\"|{_STRING_PREFIX}?'[^']*'|[^\s\"',}}]+)"
)


def _literal_body(raw: str) -> str:
    """Return a captured value without its string prefix and quotes."""
    quoted = _QUOTED_VALUE.fullmatch(raw)
    return quoted.group("body") if quoted else raw.strip("\"'")


_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{3,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprsce]-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b"),
    re.compile(r"\b(?:glpat-|hf_|xai-|pat_)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"),
    re.compile(
        rf"(?im)^[ \t]*(?P<key>[\"']?{_CREDENTIAL_KEY}[\"']?)\s*:\s*"
        r"[>|][-+0-9]*[^\n]*\n(?:(?:[ \t]+[^\n]*|[ \t]*)\n|[ \t]+[^\n]*\Z)+"
    ),
    re.compile(_ASSIGNMENT),
)

_OUTBOUND_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:gh[oprsu]_|github_pat_|sk-(?:ant-|proj-)?|xox[baprsce]-|"
        r"glpat-|hf_|xai-|ya29\.)[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(_ASSIGNMENT),
)


def redact_text(text: str, *, marker: str = "[REDACTED]") -> str:
    """Redact secrets from text while preserving numeric token counters and valid JSON."""
    if not isinstance(text, str):
        return text

    def _replace(match: re.Match[str]) -> str:
        groupdict = match.groupdict()
        if "key" in groupdict and groupdict["key"]:
            key_raw = groupdict["key"]
            key_str = key_raw.strip("\"'")
            val_raw = groupdict.get("val") or ""
            val_str = _literal_body(val_raw)
            sep_space = groupdict.get("sep_space") or groupdict.get("sep") or "="

            # Real numeric token counters are preserved
            if _SAFE_KEYS.match(key_str) and _COUNTER_VALUE.match(val_str):
                return match.group(0)

            # Already redacted values are preserved
            if val_str in ("<redacted>", "[REDACTED]"):
                return match.group(0)

            # Maintain valid JSON quoting and syntax
            quoted = _QUOTED_VALUE.fullmatch(val_raw)
            if quoted:
                replacement_val = f"{quoted.group('prefix')}{quoted.group('q')}{marker}{quoted.group('q')}"
            elif key_raw.startswith('"') and key_raw.endswith('"') and ":" in sep_space:
                replacement_val = f'"{marker}"'
            else:
                replacement_val = marker

            if "sep_space" not in groupdict:  # YAML block scalar: ``key: |`` and its lines
                suffix = "\n" if match.group(0).endswith("\n") else ""
                return f"{key_raw}: {replacement_val}{suffix}"

            return f"{key_raw}{sep_space}{replacement_val}"

        raw = match.group(0)
        if raw.lower().startswith("bearer "):
            return f"Bearer {marker}"
        if raw.lower().startswith("bearer"):
            return f"Bearer {marker}"
        return marker

    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(_replace, text)
    return text


def redact_nested_text(value: Any, *, marker: str = "[REDACTED]") -> Any:
    """Redact every string, including mapping keys, inside diagnostic data."""
    if isinstance(value, str):
        return redact_text(value, marker=marker)
    if isinstance(value, (list, tuple)):
        return [redact_nested_text(item, marker=marker) for item in value]
    if isinstance(value, dict):
        return {redact_nested_text(key, marker=marker): redact_nested_text(item, marker=marker)
                for key, item in value.items()}
    return value


def check_outbound_safe(text: str) -> list[str]:
    """Check text against outbound credential patterns, exempting numeric token counters."""
    offenders = []
    for pattern in _OUTBOUND_CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            groupdict = match.groupdict()
            if "key" in groupdict and groupdict["key"]:
                key_str = groupdict["key"].strip("\"'")
                val_str = _literal_body(groupdict.get("val") or "")
                # Exemption for real numeric token counters
                if _SAFE_KEYS.match(key_str) and _COUNTER_VALUE.match(val_str):
                    continue
                # Exemption for already-redacted values
                if val_str in ("<redacted>", "[REDACTED]"):
                    continue
            offenders.append(pattern.pattern)
    return offenders


def _markdown_credential_token_prose(match: re.Match[str], text: str, source_name: str | None) -> bool:
    """Recognize the literal Markdown label ``Credentials/tokens: secrets ...``.

    This label describes a review rule.  It is not an assignment, and all other
    credential-shaped Markdown syntax continues through the ordinary scanner.
    """
    if not source_name or not source_name.lower().endswith(".md"):
        return False
    start = text.rfind("\n", 0, match.start()) + 1
    prefix = text[start:match.start()]
    line_end = text.find("\n", match.end())
    suffix = text[match.end():None if line_end < 0 else line_end]
    return (bool(re.fullmatch(r"[ \t]*[-*+]\s+credentials/", prefix, re.IGNORECASE))
            and match.group("key").strip("\"'").lower() == "tokens"
            and _literal_body(match.group("val")).lower() == "secrets"
            and bool(re.match(r"\s+[A-Za-z]", suffix)))


_WORKFLOW_SOURCE = re.compile(r"\.github/workflows/[^/]+\.(?i:ya?ml)")
_WORKFLOW_CONTEXT_REFERENCE = re.compile(
    r"\$\{\{\s*(?:secrets|inputs|env|vars|github|matrix|steps)\.[A-Za-z0-9_.\-]+\s*\}\}")
# ``auth.get('tokens', {}).get('access_token', '')``: snake_case keys and empty defaults only.
_WORKFLOW_LOOKUP_CHAIN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.get\(\s*(['\"])[a-z_][a-z0-9_]*\1\s*(?:,\s*(?:''|\"\"|\{\}|\[\]|None|0)\s*)?\))+")


def _github_workflow_source(source_name: str | None) -> bool:
    """Workflow exemptions apply only to top-level GitHub Actions workflow files."""
    return bool(source_name and _WORKFLOW_SOURCE.fullmatch(source_name))


def _github_workflow_expression(value: str, key: str, source_name: str | None) -> bool:
    """Allow only non-literal GitHub Actions references in workflow source.

    Workflow documents routinely bind credential-named inputs to GitHub
    expression syntax. A context reference is resolved by GitHub at runtime and is
    not a credential value included in the inspected document; any other
    expression body (for example a bare literal) remains data.
    """
    if not _github_workflow_source(source_name):
        return False
    if _WORKFLOW_CONTEXT_REFERENCE.fullmatch(value):
        return True
    return key.lower() == "token" and value.lower() in {"read", "write", "none", "inherit"}


def _github_workflow_script_reference(value: str, source_name: str | None,
                                      separator: str) -> bool:
    """Recognize a non-literal Python lookup embedded in a workflow ``run`` block."""
    if separator != "=" or not _github_workflow_source(source_name):
        return False
    # A run-block lookup may name credential fields but has no credential value:
    # ``token = auth.get('tokens', {}).get('access_token', '')``.  Any other call,
    # and any quoted argument that is not a snake_case key or an empty default, is data.
    return bool(_WORKFLOW_LOOKUP_CHAIN.fullmatch(value.strip()))


# The closing line of a multi-line signature is ``...) -> Name:``: the ``->`` follows a
# ``)`` outside any string or comment.
_PYTHON_BLOCK_OPENER = re.compile(
    r"[ \t]*(?:(?:async[ \t]+)?(?:def|class|if|elif|else|for|while|with|try|except|finally"
    r"|match|case)\b|[^\"'#]*\)[ \t]*->)")
_LINE_STRING = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'")


def _opens_python_block(text: str, position: int) -> bool:
    """Whether the line before ``position`` is a compound-statement header.

    A block-opening ``:`` is never inside an open bracket, so a key after an unclosed
    ``(``, ``[`` or ``{`` is a mapping key (``if x: d = {API_KEY:``). Counting with
    ``<=`` keeps the closing line of a multi-line signature (``) -> Token:``). Brackets
    and ``->`` inside a one-line string are data, so strings are dropped first.
    """
    prefix = _LINE_STRING.sub("", text[text.rfind("\n", 0, position) + 1:position])
    return (sum(map(prefix.count, "([{")) <= sum(map(prefix.count, ")]}"))
            and bool(_PYTHON_BLOCK_OPENER.match(prefix)))


def _in_comment(text: str, position: int) -> bool:
    """Whether ``position`` follows a ``#`` on its line (a full-line or trailing comment)."""
    line_start = text.rfind("\n", 0, position) + 1
    return "#" in text[line_start:position]


def check_source_text_safe(text: str, *, source_name: str | None = None) -> list[str]:
    """Find credentials while allowing expressions only in recognized source files.

    The generic outbound check treats every credential-shaped assignment as data.
    This variant narrowly recognizes Python expressions and explicit environment
    references.  A dotted or callable value in YAML/text is still data and is
    rejected.
    """
    offenders = []
    assignment = _OUTBOUND_CREDENTIAL_PATTERNS[-1]
    for pattern in _OUTBOUND_CREDENTIAL_PATTERNS[:-1]:
        if pattern.search(text):
            offenders.append(pattern.pattern)
    python_source = bool(source_name and source_name.lower().endswith((".py", ".pyi")))
    type_names = {"str", "bytes", "int", "float", "bool", "dict", "list", "tuple",
                  "set", "object", "None", "Any", "Optional", "SecretStr"}
    explicit_reference = re.compile(
        r"^(?:\$\{\{\s*(?:secrets|env)\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}"
        r"|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)$"
    )
    for match in assignment.finditer(text):
        key = match.group("key").strip("\"'")
        raw = match.group("val")
        value = _literal_body(raw)
        if _SAFE_KEYS.match(key) and _COUNTER_VALUE.match(value):
            continue
        if value in ("<redacted>", "[REDACTED]"):
            continue
        line_end = text.find("\n", match.start())
        line = text[match.start():None if line_end < 0 else line_end]
        separator_at = line.find(match.group("sep"))
        line_value = line[separator_at + 1:].strip().strip("\"'")
        if explicit_reference.fullmatch(line_value):
            continue
        if _markdown_credential_token_prose(match, text, source_name):
            continue
        if _github_workflow_expression(line_value, key, source_name):
            continue
        if _github_workflow_script_reference(line_value, source_name, match.group("sep")):
            continue
        quoted = bool(_QUOTED_VALUE.fullmatch(raw))
        if quoted and not value:
            continue  # An empty literal carries no credential value.
        # Unquoted calls, attributes and container lookups are source expressions. A quoted
        # value is data and remains subject to the credential-assignment boundary.
        if python_source and not quoted and any(char in raw for char in ".()[]{}"):
            continue
        # A plain identifier on the right side of Python-style assignment is a reference.
        # YAML/JSON colon assignments remain data, as do literals containing digits/dashes.
        if (python_source and not quoted and match.group("sep") == "="
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw)):
            continue
        # Fencing epochs, counters and limits bound to *token* names are integers, not
        # bearer credentials. Keep this source-only exception literal and typed: a
        # quoted value remains a credential-shaped data assignment and is rejected below.
        if (python_source and not quoted and "token" in key.lower()
                and _COUNTER_VALUE.match(raw)):
            continue
        # In Python source, ``key: Name`` is an annotation only for a known or
        # capitalized type name, and ``"key": name`` in a mapping literal references a
        # variable. Other identifiers, comment lines and literals stay blocked. A line
        # scanner cannot see docstrings: ``api_key: Capitalized`` inside one still
        # passes, which a tokenize-aware pass would have to close.
        if (python_source and not quoted and match.group("sep") == ":"
                and not _in_comment(text, match.start())
                and (raw in type_names or re.fullmatch(r"[A-Z][A-Za-z0-9_]*", raw)
                     or (match.group("key")[:1] in "\"'"
                         and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw)))):
            continue
        if python_source and not quoted and value in type_names:
            continue
        # A ``:`` that ends a compound-statement line opens a block (``if not token:``,
        # ``def load() -> Snapshot:``); the next line is not its value. A quoted key is
        # always a mapping key, whatever starts the line.
        if (python_source and match.group("sep") == ":" and "\n" in match.group("sep_space")
                and match.group("key")[:1] not in "\"'"
                and _opens_python_block(text, match.start())):
            continue
        offenders.append(assignment.pattern)
    return offenders


def check_diff_text_safe(text: str) -> list[str]:
    """Scan each Git diff hunk using the syntax of the file that owns its bytes."""
    documents: dict[str, list[str]] = {}
    metadata: list[str] = []
    old_path = new_path = None
    in_hunk = False
    saw_hunk = False
    for line in text.splitlines():
        if line.startswith("--- "):
            old_path = line[4:].split("\t", 1)[0]
            old_path = old_path.removeprefix("a/")
            in_hunk = False
            metadata.append(line)
            continue
        if line.startswith("+++ "):
            new_path = line[4:].split("\t", 1)[0]
            new_path = new_path.removeprefix("b/")
            in_hunk = False
            metadata.append(line)
            continue
        if line.startswith("@@"):
            in_hunk = True
            saw_hunk = True
            metadata.append(line)
            continue
        if in_hunk and line[:1] in ("+", "-", " "):
            path = old_path if line.startswith("-") else new_path
            if path and path != "/dev/null":
                documents.setdefault(path, []).append(line[1:])
            else:
                metadata.append(line)
        else:
            metadata.append(line)
    if not saw_hunk or not documents:
        return check_outbound_safe(text)
    offenders = check_outbound_safe("\n".join(metadata))
    for name, lines in documents.items():
        offenders.extend(check_source_text_safe("\n".join(lines), source_name=name))
    return offenders


def check_file_text_safe(text: str, *, source_name: str) -> list[str]:
    """Dispatch outbound source scanning using explicit file syntax."""
    if source_name.lower().endswith(('.diff', '.patch')):
        return check_diff_text_safe(text)
    return check_source_text_safe(text, source_name=source_name)


def safe_config_diagnostic(data: Any) -> Any:
    """Return a deep copy of config/diagnostic data with credentials redacted.

    Preserves non-secret configuration structure (e.g. hosts, paths, profiles,
    and supported numeric token counters) while redacting tokens, keys, passwords,
    and secret strings. Output is valid JSON-serializable structure.
    """
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            k_str = str(k)
            if re.search(rf"(?i)^(?:{_CREDENTIAL_KEY})$", k_str):
                if (_SAFE_KEYS.match(k_str) and isinstance(v, (int, float)) and not isinstance(v, bool)
                        and _COUNTER_VALUE.match(str(v))):
                    result[k] = v
                elif _SAFE_KEYS.match(k_str) and isinstance(v, str) and _COUNTER_VALUE.match(v):
                    result[k] = v
                else:
                    result[k] = "[REDACTED]"
            else:
                result[k] = safe_config_diagnostic(v)
        return result
    elif isinstance(data, list):
        return [safe_config_diagnostic(item) for item in data]
    elif isinstance(data, str):
        return redact_text(data, marker="[REDACTED]")
    return data
