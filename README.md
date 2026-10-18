# OPNsense Cold Spare for Proxmox VE

A **cold standby OPNsense virtual machine** that can temporarily replace a physical OPNsense firewall, while normally operating as an independent maintenance client. The project is **not** a CARP/pfsync high-availability cluster and does not provide automatic failover.

> **Development status — V6:** Python unit tests and shell syntax checks have passed, but **V6 has not yet undergone a complete OPNsense/FreeBSD and Proxmox integration test**. In particular, the consolidated import hook's NIC renaming and the end-to-end WAN and failback operations must be verified on the target system before relying on it for disaster recovery. Keep a working backup and out-of-band console access.

## How it works

The Proxmox controller (`opnsense-spare`) configures the VM's VirtIO NICs and attaches a small, read-only, mode-specific ISO. During OPNsense boot, a single early **import hook** (`90-opnsense-spare`) reads that ISO *before network services initialize*.

The ISO contains:

- `request.conf`: operating mode, unique request ID, optional import flag and SHA-256 checksums.
- `interfaces.conf`: mapping of every `vtnetN` device to its desired FreeBSD interface name.
- `config.xml`: **only when the operator explicitly requests a new emergency configuration import**.

The hook validates the mapping and renames interfaces **on every boot**, including when the request ID is unchanged. It then selects the appropriate persistent configuration profile. Repeated boots with the same request ID do **not** reimport or overwrite the active `/conf/config.xml`.

The VM maintains separate profiles:

| Guest path | Purpose |
|---|---|
| `/conf/config.xml` | Active, writable OPNsense configuration |
| `/conf/spare/maintenance.xml` | Saved maintenance configuration |
| `/conf/spare/emergency.xml` | Saved emergency configuration |
| `/conf/spare/active-mode` | Last activated mode |
| `/conf/spare/last-request` | Last processed request ID |
| `/conf/spare/transition` | Interrupted-transition marker (if present) |

On a mode change, the previous active configuration is preserved before activating the target profile. A new emergency import replaces the emergency profile, archiving its previous version when present. Configuration import is **entirely local**; it works with `link_down=1` and does not require guest SSH or network connectivity. **Only the optional failback export uses SSH**, after the VM has started in maintenance mode.

## Network requirements and topology

The controller is **independent of any switch vendor and does not require VLANs**. Each physical firewall network must, however, have an equivalent and isolated Layer-2 path to its corresponding VM NIC. You can use dedicated PVE bridges and physical adapters, VLAN trunks, or a combination.

Supported interface mapping example:

```json
"interface_names": {
  "vtnet0": "igc0",
  "vtnet1": "igc1",
  "vtnet2": "igc3"
}
```

For a physical firewall with `igc0` (untagged LAN), `igc1` (tagged VLAN trunk), and `igc3` (untagged DHCP WAN), see [`opnsense-spare-three-nic.json.example`](opnsense-spare-three-nic.json.example). The example uses dedicated bridges `vmbrLAN`, `vmbrTRUNK`, and `vmbrWAN`. For a two-interface installation using one VLAN-aware bridge, see [`opnsense-spare.json.example`](opnsense-spare.json.example).

The controller supports contiguous VirtIO slots `net0` through `netN` (up to 32). Both modes define the same slots. Exactly one configured NIC provides maintenance connectivity; all others must have `link_down=1` in maintenance mode. `--link-down` disconnects **every** emergency NIC; `--link-up` restores the configured emergency connectivity.

**Important limitations:** `ifconfig ... name ...` changes an interface's name, not its driver or hardware capabilities. NIC-specific tunables, offloading, IDS/IPS configuration, PCI passthrough, SR-IOV, and provider-specific WAN requirements may need individual adaptation. This implementation expects **VirtIO** NICs presented as `vtnetN` in the guest.

## New installation

1. Create an OPNsense VM with all required VirtIO NIC slots. Do not enable automatic VM startup. Set up the PVE bridges and physical/VLAN transport paths beforehand.
2. Install OPNsense and configure it as an ordinary **maintenance LAN client**, using an independent IP and MAC address and a default gateway through the physical firewall. Keep production network services disabled in maintenance mode. Align the maintenance configuration's interface names with `interface_names` in the host JSON.
3. Install the **single V6 import hook** in the guest and initialize the saved maintenance profile:

   ```sh
   install -m 755 90-opnsense-spare /usr/local/etc/rc.syshook.d/import/90-opnsense-spare
   /usr/local/etc/rc.syshook.d/import/90-opnsense-spare init
   ```

4. On the PVE host, install the controller and customize the example JSON. **Never deploy example IPs, MACs, bridge names, or VLANs unchanged.**

   ```sh
   apt install xorriso
   install -m 755 opnsense-spare /usr/local/sbin/opnsense-spare
   install -m 600 opnsense-spare.json.example /etc/opnsense-spare.json
   editor /etc/opnsense-spare.json
   ```

5. Stop the VM, check `opnsense-spare maintenance --dry-run`, then run `opnsense-spare maintenance` and confirm VM startup. Verify the interface renaming, management connectivity, DNS, gateway, WebGUI, and SSH. Reboot once more to verify idempotence.
6. Test emergency mode **with `--link-down` first**. Do not connect production MAC/IP addresses to the network while the physical firewall is online. Test the complete failback and export paths before using the spare in production.

**Boot dependency:** The hook intentionally fails closed if its valid mode ISO is missing or the mapping is invalid. Keep Proxmox console access and a VM backup available.

## Migrating from V5

1. Back up the VM, existing PVE controller/JSON/state file, guest hooks, and `/conf/spare/` profiles. Ensure the VM is stopped in its confirmed maintenance mode.
2. Convert the V5 host JSON **without overwriting the original**:

   ```sh
   python3 migrate-v5-config.py /etc/opnsense-spare.json \
     --name vtnet0=ix1 --name vtnet1=igc3 \
     -o /etc/opnsense-spare-v6.json
   ```

3. Review the new JSON: `interface_names`, `maintenance_client_nic`, every NIC profile, MAC addresses, bridges, VLANs, SSH options, and the transfer path. Ensure target FreeBSD interface names match the guest's saved maintenance configuration. Back up the old JSON before installing the new one.
4. Install the V6 guest import hook. **Disable/remove the old** `/usr/local/etc/rc.syshook.d/early/10-spare-interface-names`; otherwise both hooks will attempt renaming.
5. Install the V6 host controller. With the VM stopped, invoke `opnsense-spare maintenance` to generate a **V6 ISO with `interfaces.conf`**. An older V5 ISO will not satisfy the new hook.
6. Boot in **maintenance only** and verify NIC names and connectivity, including after a second boot. Only then try an isolated emergency import.

If the prior state is `prepared` or `incomplete`, do not migrate blindly. Reconcile the state with the actual last guest mode first. A manually started VM can invalidate the host's assumed state.

## Operation

Run on the **Proxmox host as root**; except for a retry of a pending export, the VM must be stopped:

```sh
opnsense-spare maintenance
opnsense-spare emergency --link-down --transfer /path/to/physical-config.xml
opnsense-spare emergency --link-up --transfer /path/to/physical-config.xml
opnsense-spare maintenance --transfer /path/to/failback.xml
opnsense-spare emergency --recover-incomplete
```

- **Maintenance** connects the VM as a LAN client with its own MAC and addressing. It may coexist with the physical firewall.
- **Emergency** uses the configured production MAC addresses and network mapping. On entry, the controller asks whether to import an external `config.xml`. Declining import activates the saved emergency profile instead.
- **Isolated emergency testing:** `--link-down` sets `link_down=1` on all VM NICs, allowing guest boot and configuration tests while the physical firewall remains active. Use the PVE console.
- **Live emergency operation:** `--link-up` enables all emergency links and requires operator confirmation that the physical firewall is off and the WAN/network cabling has been moved as appropriate.
- **Failback:** when switching from emergency to maintenance, the controller offers to copy the guest's saved emergency profile to a file on the PVE host using SSH. The transfer can be retried after a failure or explicitly skipped. It never automatically imports that configuration on the physical firewall.
- **Declined VM start:** answering `n` to the start prompt leaves a `prepared` state, not an error. Do not manually start a prepared VM without reconciling guest/host state.
- **Recovery:** `--recover-incomplete` examines an interrupted transition. It cannot safely resolve every partial failure; do not manually edit `state.json` as a shortcut.

The controller uses two stable ISO filenames (`opnsense-spare-<VMID>-maintenance.iso` and `opnsense-spare-<VMID>-emergency.iso`) and unique request IDs *inside* the ISO. Old per-request ISOs from prior versions are not removed automatically.

The optional `--transfer` path refers to a **file on the PVE host**, not inside the guest. Set `transfer_default` in JSON to avoid repeating it. The maintenance export supports an SSH agent (`"ssh_identity": null`) and strict host-key verification.

## Safety notes

- **Never run the physical firewall and a network-connected emergency VM simultaneously** with overlapping production IP/MAC addresses.
- Emergency switching is manual by design. `onboot=0` is enforced.
- Validate the PVE bridge wiring, VLAN tagging/untagging, WAN connectivity (DHCP or PPPoE), gateway, routing, DHCP, VPN, and firewall behavior for *your* topology.
- The mode ISO may contain a complete firewall configuration, **including secrets**. The controller creates the ISO with permission `0600`; protect PVE backups, snapshots, transfer files, and any old ISO images accordingly. Never commit real `config.xml` or `state.json` data.
- The guest hook fails closed on absent/invalid media, invalid mapping, source-name conflicts, or an interrupted profile transition. This may halt the VM and require console recovery.
- An older EFI loader can fail before OPNsense hooks run. Verify loader compatibility after major upgrades.

## Tests and status

```sh
python3 -m unittest -v test_host test_v6
python3 -m py_compile opnsense-spare migrate-v5-config.py
sh -n 90-opnsense-spare
```

The automated tests exercise controller state transitions, imports/exports, ISO generation, VLAN validation, and two-/three-NIC configurations. They are **not** a substitute for a real OPNsense boot, WAN cutover, and failback test.

### Project files

| File | Purpose |
|---|---|
| `opnsense-spare` | Proxmox host controller (Python 3) |
| `90-opnsense-spare` | Single OPNsense guest import hook (FreeBSD `/bin/sh`) |
| `opnsense-spare.json.example` | Example two-NIC configuration |
| `opnsense-spare-three-nic.json.example` | Example three-NIC topology |
| `migrate-v5-config.py` | Migration helper for V5 JSON |
| `test_host.py`, `test_v6.py` | Unit and regression tests |
| `CHANGELOG-V5.md`, `CHANGELOG-V6.md` | Development history |

**No live configuration, private keys, or production secrets belong in the Git repository.**