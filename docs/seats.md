# `seats.yaml` reference

A **seat** is a named, immutable provider/model invocation configuration for the weekly retrospective. The registry contains invocation facts only: provider identity, model ID, credential environment-variable name, adapter, and adapter options. It contains no credentials. See [adoption](adoption.md) for initial wiring, [retro](retro.md) for how seats participate in the weekly loop, and [governance](governance.md) for the human-review boundary.

Point `corral.yaml` at the registry and name the roles:

```yaml
seats_file: seats.yaml
retro:
  drafter_seat: retro-drafter
  verifier_seats: [retro-verifier, local-verifier]
  require_distinct_provider: true
```

The registry requires `schema_version: 1` and a `seats` mapping. Seat names must be unique. Every seat requires non-empty `provider`, `model`, and `adapter` strings plus an explicit `auth_env` string or `null`.

## Anthropic SDK adapter

```yaml
schema_version: 1
seats:
  retro-drafter:
    provider: vendor-a
    model: model-id
    auth_env: CORRAL_DRAFTER_API_KEY
    adapter: anthropic-sdk
```

```bash
export CORRAL_DRAFTER_API_KEY='…'
```

`anthropic-sdk` uses the optional `anthropic` dependency supplied by `corral[preflight]`. The probe is local: it checks that `auth_env` is configured and set and that the SDK imports. It does not generate text. Completion creates an Anthropic client with only the explicitly read token and the configured timeout, then sends one user message with the requested output budget. The adapter does not allow the SDK to discover ambient standard credential variables when the named credential is absent.

## OpenAI-compatible endpoint adapter

```yaml
schema_version: 1
seats:
  retro-verifier:
    provider: vendor-b
    model: model-id
    auth_env: CORRAL_VERIFIER_API_KEY
    adapter: openai-compatible-endpoint
    options:
      base_url_env: CORRAL_VERIFIER_BASE_URL
      protocol: chat-completions
```

```bash
export CORRAL_VERIFIER_API_KEY='…'
export CORRAL_VERIFIER_BASE_URL='https://provider.example/v1'
```

This adapter uses the Python standard library. `options.base_url_env` is required, and `options.protocol` must be `chat-completions` or `responses`. The adapter appends `/chat/completions` or `/responses` unless the configured URL already ends with that suffix. Probe checks only the named base URL and credential variables. Completion sends a bearer token, model, prompt, timeout, and adapter-appropriate output-token field; it accepts the corresponding response shape.

## Shell-command adapter

```yaml
schema_version: 1
seats:
  local-verifier:
    provider: local-cli
    model: model-id
    auth_env: null
    adapter: shell-command
    options:
      argv: [provider-cli, --model, "{model}"]
```

With a provider that requires a named credential and prompt file:

```yaml
  isolated-cli-verifier:
    provider: vendor-c-cli
    model: model-id
    auth_env: CORRAL_LOCAL_PROVIDER_TOKEN
    adapter: shell-command
    options:
      argv: [provider-cli, --model, "{model}", --prompt-file, "{prompt_file}"]
```

```bash
export CORRAL_LOCAL_PROVIDER_TOKEN='…'
```

`options.argv` is a non-empty list and is executed directly, never through a shell. Only `{model}` and `{prompt_file}` placeholders are accepted. Without `{prompt_file}`, the prompt is sent on stdin. With it, corral creates a private temporary directory and a prompt file with owner-only permissions, substitutes its path, and removes it after completion. Probe resolves the executable on `PATH` and checks the named credential when one is configured. Local commands own any provider-specific token-limit flags in `argv`; corral does not append one.

## Probe and completion semantics

All adapters use the same status vocabulary:

| Status | Meaning |
| --- | --- |
| `ok` | The probe is ready or the completion returned text. |
| `unavailable` | Required configuration, credentials, package, endpoint, or executable is absent. |
| `timeout` | The completion exceeded the configured timeout. |
| `error` | The provider, response parser, or local process failed. |

`probe(seat)` is a readiness check and never asks a model to generate text. It returns the seat/provider/model provenance and a diagnostic. `complete(seat, prompt, timeout, max_tokens)` performs one bounded request and folds expected failures into a status rather than leaking provider-specific exceptions into the retrospective.

Run the registry-level diagnostic with:

```bash
corral retro seats check
```

The configured drafter and first verifier are required for this command; later verifier seats are reported but treated as fallbacks.

## Provider distinctness and fallback

`retro.require_distinct_provider: true` compares normalized `provider` labels, not adapter names. A verifier seat with the same provider label as the drafter is skipped. Choose truthful stable labels: two endpoints backed by the same provider should not be presented as independent merely because their model or adapter strings differ.

Verification tries `retro.verifier_seats` in order. It advances when a seat is missing, same-provider, fails probe, times out, errors, or returns no successful completion. The first successful completion supplies verifier provenance. A malformed verdict gets one correction attempt on that same responding seat.

If no verifier succeeds, gotcha candidates follow `retro.gotcha_unavailable_policy` (default `proceed-unverified`), while instruction proposals follow the stricter `retro.instruction_unavailable_policy` (default `fail-closed`). An explicit verifier `REFUTE` rejects a candidate. Keep these two policies separate: durable gotcha capture can degrade visibly, but proposed normative instruction edits require independent verification by default.

## Security model

Credentials are isolated by name:

- A seat reads only its configured `auth_env`; the YAML stores the variable name, never the secret.
- The Anthropic adapter refuses to instantiate the SDK without that explicit token, preventing ambient SDK credential discovery.
- The compatible-endpoint adapter reads only its named token and base-URL variables.
- A shell child receives a default-deny environment: only `PATH`, `HOME`, `LANG`, and the seat's configured `auth_env` when present. Other ambient secrets are not inherited.
- Shell commands are argv-only with `shell=False`; model IDs and prompt-file paths are values, not executable shell syntax.
- Duplicate YAML keys, unknown adapters, unsupported placeholders, and incomplete adapter options fail registry loading.

Use separate environment variables for separate seats, scope their provider permissions narrowly, and inject them only into the retrospective job that needs them. The full commented registry is [`seats.example.yaml`](../seats.example.yaml).
