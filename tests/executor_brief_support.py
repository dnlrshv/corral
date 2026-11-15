"""Neutral executor contract shared by validation and native dispatch tests."""


def sample_brief(goal="Implement addition."):
    return {
        "schema": "corral-executor-brief-v1",
        "goal": goal,
        "why": "Callers need the computed sum.",
        "sources": [{"path": "math_ops.py", "role": "authoritative"}],
        "acceptance": [
            {
                "id": "AC1",
                "text": "Addition returns the sum.",
                "verify": "unit",
                "test": "tests/test_math_ops.py::test_add",
            }
        ],
        "commands": [
            {
                "id": "unit",
                "argv": ["python", "-m", "pytest", "tests/test_math_ops.py"],
                "cwd": ".",
                "where": "workspace",
            }
        ],
        "constraints": [
            {"text": "Keep the add signature.", "reason": "Callers pass two values."}
        ],
        "scope": {
            "change": ["math_ops.py"],
            "read_only": ["README.md"],
            "out": ["CLI changes"],
        },
        "on_ambiguity": "stop-and-report",
        "result": {"schema": "corral-executor-result-v1"},
    }
