# V6 – Universal network mappings (2026-10-18)

- Verified upstream boot sequence: import hook before early and network startup.
- Removed need for `early/10-spare-interface-names`: validates and renames NICs within `import/90-opnsense-spare` **on every boot**, prior to request-idempotence check.
- `interfaces.conf` on every boot ISO with SHA256 in `request.conf`; no executing ISO content as code.
- Arbitrary number of consecutive VirtIO VM NIC slots (`net0`..`netN`, up to 32) with central `interface_names` mapping.
- Per-mode `networks` profiles without vendor-specific switch or VLAN assumptions.
- Configurable `maintenance_client_nic` and `emergency_instructions`.
- Maintenance NIC isolation checks, duplicate MAC/target name and VLAN preflight.
- `--link-down`/`--link-up` now apply to **all Emergency NICs**.
- Existing V5 pending export, prepared, boot idempotence, emergency import / archive, and ISO naming retained.
- Includes optional V5->V6 JSON converter and three-NIC example.

**Not tested on an actual FreeBSD/OPNsense V6 VM**. Run isolated maintenance smoke test before real emergency.
