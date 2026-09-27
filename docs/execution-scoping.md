# Execution route scoping

All route scope fields are optional. `allowed_repositories` lists service repository
profile names. `allowed_workdirs` lists absolute, normalized directory roots; Corral
resolves the checkout and roots before comparing them, so a checkout symlink cannot
escape its root. Configure both on write-capable implementation and repair routes.
The executor endpoint has its own `allowed_repositories` request gate. Its list is
independent of a native route's `allowed_repositories`.

A service repository profile may also set `allowed_routes` to a list of route
ids. Admission requires both the repository allowlist and the route's
`allowed_repositories`/`allowed_workdirs` scopes to pass. Validation reports
invalid or excluded route references.

Routes may set `denied_models` to exact model ids or trailing `prefix*` patterns.
They override profile requests and cannot overlap the route's `supported_models`.
`min_cli_version` (`X.Y.Z`) requires literal `version_argv` arguments. Before a
native model launch, Corral probes the resolved binary under the demonstrated
worker boundary with a five-second limit, caches the result by binary path and
modification time in that process, and refuses versions below the minimum or
unparsable output. `validate` checks declaration shape without running binaries.

The service may set `provider_concurrency` to a map of provider name to positive
slot count. Prepared tasks wait in the queue when their provider's active
reservations reach that count. The budget spans repository profiles and hosts;
host capacity and workspace fences still apply. Native results and service
status include provenance with provider, route, model, effort, probed CLI
version (or null), envelope schema, a SHA-256 of the exact captured stdout bytes
fed to the envelope parser, Corral package version, and installed commit (or
null). The parser consumes at most the final 4 MiB of stdout, so the hash covers
that same bounded byte slice.

Adaptable route declarations are in [the provider examples](../examples/routes/README.md).

`max_packet_bytes` is a positive integer on an inspection route or review policy.
A policy value takes precedence over a route value; without either, the serialized
inspection packet is limited to 4 MiB. Oversized packets fail with their actual
serialized size and configured limit. No document is silently truncated.

Every seat process drops credential-shaped environment names, including token,
API key, secret, password, AWS, Anthropic and OpenAI names. A native route forwards
only names listed in its `credential_env`; the controller must provide their values
through its private credential configuration. Deterministic workers and verifiers
have no route credential allowance. Run `corral-service validate` to check route
declarations and packet limits; it warns when a write-capable route has no scope.

Native workers and native test verifiers require a demonstrated macOS Seatbelt
boundary that denies `.env`-style file reads. The inspection transport runs inside
the native worker boundary and receives packet copies only. Deterministic verifiers
have a scrubbed environment but do not have that file-read boundary. A deterministic
worker uses the boundary only when `use_sandbox` is set; its workspace and TMPDIR
exceptions limit the file-read denial. On hosts without working Seatbelt support,
native routes fail before launch; deterministic paths offer no equivalent file-read
isolation. Run deterministic lanes only with separate OS-level repository and
credential isolation.
