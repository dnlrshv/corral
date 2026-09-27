# Execution route scoping

All route scope fields are optional. `allowed_repositories` lists service repository
profile names. `allowed_workdirs` lists absolute, normalized directory roots; Corral
resolves the checkout and roots before comparing them, so a checkout symlink cannot
escape its root. Configure both on write-capable implementation and repair routes.
The executor endpoint has its own `allowed_repositories` request gate. Its list is
independent of a native route's `allowed_repositories`.

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
