# Provider route templates

These JSONC objects are route declarations to copy into a host's `native_routes`
mapping. Comments are guidance; remove them before loading as JSON. Set each
route id to the file stem, adjust the model, endpoint, account reference, binary,
and launch authorization, then register a matching controller profile. The
examples keep `launch_authorized: false` until the operator checks them.

- `codex`: the argv shape follows `corral.execution.coding_launcher`. Codex uses
  its own auth file. Configure a dedicated `runtime_home` and a narrow
  `runtime_read` grant to that file for this route; never copy its contents into
  a route or a repository. The example intentionally omits a machine-specific
  path. Confirm the minimum CLI version for the installed CLI.
- `agy-review`: API-key mode passes `GEMINI_API_KEY` through `credential_env`.
  Use a profile with only the `review` role. Verify `--plan` against your CLI;
  this flag is the template's proposed read-only review mode.
- `deepseek-review`: use an inspection-only profile with `roles: ["review"]`
  and `tools: ["inspect-packet", "report"]`. Replace `SET_HTTPS_ENDPOINT` with
  the configured API endpoint. This route gives the transport a packet, not a
  workspace. The retired-model pattern illustrates `denied_models`.
- `qwen-review`: use a review-only profile and verify its entire argv contract
  against the installed Qwen Code CLI before enabling launch. The parser accepts
  the declared stream schema, but this repository does not pin a Qwen CLI argv.

For every route, list only observed model IDs in `supported_models`. Match each
profile's `provider`, `account_ref`, model and effort to the route. Route scopes
and repository `allowed_routes` can narrow admission further.
