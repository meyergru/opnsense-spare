# Changelog

## Stateless Proxmox controller (2026-10-18)

- Removed persistent host mode, prepared, pending-export, and incomplete-transition tracking; PVE is the authority for VM status and hardware configuration.
- VM starts may be declined and later performed manually with `qm start`; no subsequent reconciliation or recovery flag is necessary.
- Configuration import/export requires explicit `--transfer`; an export-only retry can run while the VM is already in maintenance mode.
- Disables PVE autostart before NIC changes, and verifies every applied NIC and the attached mode ISO before starting the VM; failed partial hardware updates never trigger a start.
- Kept the OPNsense guest boot hook and both mode ISO formats unchanged.
- Updated examples and regression tests for two-/three-NIC layouts and manual cutover/failback workflows.

Implementation milestones, listed newest first. Historical version labels are retained here for reference; the current controller and documentation are not version-specific.

## V6 — Universal network mappings (2026-10-18)

- Verified upstream boot sequence: import hook before early and network startup.
- Removed need for `early/10-spare-interface-names`: validates and renames NICs within `import/90-opnsense-spare` **on every boot**, prior to request-idempotence check.
- `interfaces.conf` on every boot ISO with SHA256 in `request.conf`; no executing ISO content as code.
- Arbitrary number of consecutive VirtIO VM NIC slots (`net0`..`netN`, up to 32) with central `interface_names` mapping.
- Per-mode `networks` profiles without vendor-specific switch or VLAN assumptions.
- Configurable `maintenance_client_nic` and `emergency_instructions`.
- Maintenance NIC isolation checks, duplicate MAC/target name and VLAN preflight.
- `--link-down`/`--link-up` now apply to **all Emergency NICs**.
- Existing V5 pending export, prepared, boot idempotence, emergency import / archive, and ISO naming retained.
- Added a three-NIC configuration example.

**Not tested on an actual FreeBSD/OPNsense V6 VM**. Run isolated maintenance smoke test before real emergency.

## V5 — Host state-machine fixes

- Answering `n` to the VM startup prompt leaves `prepared` rather than `incomplete`. A successfully configured but not booted profile must not be recorded as running.
- Subsequent invocations verify the prepared NIC settings and ISO SHA-256 before reconfiguring. A manually started VM cannot be reconciled automatically.
- Pending failback exports no longer block emergency mode unconditionally. Operators can explicitly decline the pending export after confirmation.
- `--recover-incomplete` can classify the V4 declined-start failure as prepared if all NIC profiles and the ISO match. Recovery from unknown partial failures remains conservative.
- Guest hook and host JSON format are unchanged from V4.

**Migration caution:** A previously hand-edited `state.json` cannot be reconstructed automatically. Check the true guest/host mode before another production boot.

## V4 — Stable ISO names and isolated testing

- Use a stable ISO filename per mode, with a unique request ID inside each image; replace ISO files atomically.
- Add `--link-down` and `--link-up` for isolated emergency tests, and regenerate NIC settings even when selecting the current mode.
- Track ISO checksums and configured NICs for conservative interrupted-transition recovery.

## V3 — SSH agent support

- Allow `ssh_identity` to be omitted or `null`, enabling the operator’s SSH agent for maintenance-mode exports.

## V2 — Confirmation and recovery hardening

- Normalize confirmation prompts, validate VLAN trunk syntax before changing the VM, and add guarded incomplete-transition recovery.
- Correct the failback export to read the saved emergency configuration.

## Initial prototype

- Introduce maintenance and emergency profiles, ISO-carried mode requests, the early OPNsense guest hook, Proxmox NIC reconfiguration, and SSH-based failback export.
