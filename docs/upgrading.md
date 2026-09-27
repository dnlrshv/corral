# Picking up a Corral release

Consumers pin `corral_sha`, `release_tag`, and `min_protocol`. At startup, run the pinned runtime's `corral-service version` and compare its JSON `protocol` to `min_protocol`; fail closed when the runtime protocol is lower. Compare its JSON `commit` to `corral_sha` and fail closed on a null or mismatched commit for production pins. Verify that `release_tag` resolves to that commit. Keep the tag and SHA together in the consumer change. The version command does not read configuration or open the store.

## Maintained service on a host

For a service installed per commit, use `~/.local/share/corral/runtimes/<sha>/.venv` for each installed runtime and `~/.local/share/corral/backups/<sha>/` for the pre-upgrade backup. Use the same registered service configuration and database locations as the current installation. `corral-service-install` renders a launchd plist from the chosen executable, config, interval, and log directory; use the equivalent installed unit on other service managers.

1. Drain the service and verify that no runs are in flight. Stop its scheduler before changing the runtime.
2. Back up the database, service and controller configuration, and the current LaunchAgent plist or unit to `~/.local/share/corral/backups/<sha>/`. Copy the database consistently while it is stopped, including any write-ahead log state if applicable.
3. Verify the tag resolves to the pinned SHA, then install into `~/.local/share/corral/runtimes/<sha>/.venv` with that environment's `pip install "git+https://github.com/<owner>/corral@<sha>"`. This VCS URL records the commit in installed metadata. Check `corral-service version` from that environment and compare `commit` to the pinned SHA.
4. Validate the configuration with the installed `corral-service --config <service-config> validate` command. Validation is read-only and may run against the live config before draining the current service. Resolve any reported error before cutover.
5. Render or update the LaunchAgent plist or unit to use the new runtime executable. Load or start it, then verify its version output and perform one no-inference smoke, such as reading a known event with `corral-service --config <service-config> status --event-id <known-event-id>`.
6. Keep the old runtime and the pre-upgrade backup until the consumer pickup smoke and service observation are complete.

## Rollback

Stop the service and drain any remaining work. If `store_schema` changed, restore the pre-upgrade database backup before starting the older runtime: a newer database refuses to open on an older Corral by design. Restore the previous config and plist or unit as needed, repoint it to the old runtime, start it, and repeat the version and no-inference checks. Reconcile any uncertain work before new dispatches.

## Consumer pickup PR

- Bump `corral_sha`, `release_tag`, and `min_protocol` together, checking that the tag resolves to the SHA.
- Read the release's **Consumer action** subsection and apply its required changes.
- Run the consumer's integration smoke against the pinned runtime and verify startup fails closed on a deliberate version mismatch.
