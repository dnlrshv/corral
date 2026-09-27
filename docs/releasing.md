# Releasing Corral

Release tags are annotated `vX.Y.Z` tags on the merge commit. The tag and GitHub release are created only after that merge has passed required CI.

## Versioning rules

`corral/protocol.py` defines the execution protocol and SQLite store schema versions. Follow its protocol-break definition when changing requests, responses, supported commands, or execution configuration. Additive optional fields do not require a protocol bump. Bump `STORE_SCHEMA_VERSION` for a store schema change that needs a new runtime. Within 0.x, bump the package minor version when either integer changes; otherwise bump the patch version. Keep the versions in `pyproject.toml` and `corral/__init__.py` equal.

## Checklist

1. Prepare a `CHANGELOG.md` section with Added, Changed, Fixed, and **Consumer action**. State exact compatibility and pickup work, including when no consumer changes are required.
2. Bump the package version in both files, and bump protocol or store schema versions when their contracts change. Run the version commands and tests.
3. Merge only after required CI is green. Record the exact merge commit SHA.
4. Create an annotated tag `vX.Y.Z` on that merge commit and publish it. Create a GitHub release for the tag with the corresponding CHANGELOG section as its body.
5. Notify consumers by opening a pickup issue in each registered consumer repository. Include the tag, SHA, protocol minimum, Consumer action, and a link to [the upgrade procedure](upgrading.md).

Never tag a pre-merge candidate or advertise a release before the tag and release exist.
