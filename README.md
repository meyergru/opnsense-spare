# OPNsense Cold Spare for Proxmox VE

A **cold standby OPNsense virtual machine** that can temporarily replace a physical OPNsense firewall, while normally operating as an independent maintenance client. The project is **not** a CARP/pfsync high-availability cluster and does not provide automatic failover.

> **Test status:** A live failover from a physical OPNsense to the Proxmox VM succeeded on 18 October 2026. The simplified host controller has automated regression tests; its revised workflow still needs validation on a real PVE host before operational reliance. Keep a working backup and out-of-band console access.

## Tutorial and forum discussions

- [Step-by-step tutorial on the OPNsense forum](https://forum.opnsense.org/index.php?topic=53175) — installation and configuration of the virtual cold spare.
- [Proxmox forum discussion](https://forum.proxmox.com/threads/virtual-cold-spare-for-a-physical-opnsense-using-proxmox.187127/) — discussion and reference for Proxmox users.

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

### Preserve the physical interface names *and* MAC addresses

A restored physical OPNsense `config.xml` refers to the original interface names (for example `ix1` and `igc3`), not to Proxmox VirtIO names (`vtnet0`, `vtnet1`). To avoid modifying every imported configuration, the guest hook performs a **temporary FreeBSD interface rename on every guest boot**, *before* OPNsense configures networking:

```text
PVE net0 -> FreeBSD vtnet0 -> ifconfig name ix1  -> OPNsense config.xml: ix1
PVE net1 -> FreeBSD vtnet1 -> ifconfig name igc3 -> OPNsense config.xml: igc3
```

Define that mapping centrally in `interface_names` in the host JSON; the controller copies it to the attached ISO as `interfaces.conf`. The single guest `import/90-opnsense-spare` hook reads it. There is no separate interface-renaming script. **The renamed device is still a VirtIO device**; this does not emulate the original Intel driver or its hardware capabilities.

MAC addresses are a *separate* setting. In each `networks.emergency.netN` string, set `virtio=<MAC>` to the **actual MAC address of the corresponding physical OPNsense interface** (check `ifconfig` on the physical firewall). This minimizes disruption to DHCP/ISP leases, ARP/neighbor caches and other MAC-sensitive peers. The **maintenance client NIC must instead use a unique, non-production MAC**; the other maintenance NICs are disconnected. Do **not** reuse the illustrative `02:00:00:...` addresses from the examples in a live installation.

| PVE setting | What it means | Where to obtain it |
|---|---|---|
| `interface_names.vtnetN` | FreeBSD name expected by the physical `config.xml` | Physical OPNsense `config.xml` / `ifconfig -a` |
| `networks.emergency.netN: virtio=...` | Production MAC to clone during emergency | Physical OPNsense `ifconfig <interface>` (`ether`) |
| `networks.maintenance.netN: virtio=...` | Independent maintenance MAC | Generate a unique locally administered MAC |
| `networks.*.netN: bridge=...` | Layer-2 path of that interface | Your PVE bridges and switch/port layout |

**Before the first boot with interface renaming enabled:** the VM's saved maintenance `config.xml` may still refer to `vtnet0`. Once the hook renames it to the physical name, that old assignment will no longer work. Prepare to reassign the maintenance interface using the **PVE console** to the renamed interface (e.g. `ix1`), configure its independent LAN IP/gateway, and verify it again after reboot. Never depend on SSH alone for this initial transition.

### VLAN transport is optional

In a VLAN-aware PVE bridge, `tag=VID` sets the VM port's **untagged/native** VLAN (PVID); `trunks=VID;VID;...` permits **tagged** VLANs toward the guest. With `tag=4094,trunks=1;10;50`, a deliberately unused VLAN 4094 acts as the native/PVID and VLANs 1, 10 and 50 can arrive at the guest tagged. This is useful when the physical OPNsense expects *even VLAN 1* as a tagged VLAN, but the PVE host's switch uplink uses native/untagged VLAN 1. Check the actual 802.1Q behavior on both directions; UniFi is **not** required. For an untagged LAN or DHCP WAN, an independent `bridge=vmbrLAN` / `bridge=vmbrWAN` with no `tag` or `trunks` is sufficient when the physical connections are separately available.


The controller supports contiguous VirtIO slots `net0` through `netN` (up to 32). Both modes define the same slots. Exactly one configured NIC provides maintenance connectivity; all others must have `link_down=1` in maintenance mode. `--link-down` disconnects **every** emergency NIC; `--link-up` restores the configured emergency connectivity.

**Important limitations:** `ifconfig ... name ...` changes an interface's name, not its driver or hardware capabilities. NIC-specific tunables, offloading, IDS/IPS configuration, PCI passthrough, SR-IOV, and provider-specific WAN requirements may need individual adaptation. This implementation expects **VirtIO** NICs presented as `vtnetN` in the guest.

## New installation

1. Create an OPNsense VM with **OVMF (UEFI)** firmware and an **EFI disk**, plus all required VirtIO NIC slots. Add a virtual serial port (`serial0: socket`) in addition to the VGA console. Do not enable automatic VM startup. Set up the PVE bridges and physical/VLAN transport paths beforehand.
2. Install OPNsense and configure it as an ordinary **maintenance LAN client**, using an independent IP and MAC address and a default gateway through the physical firewall. Keep production network services disabled in maintenance mode. Align the maintenance configuration's interface names with `interface_names` in the host JSON.
3. Install the **single import hook** in the guest and initialize the saved maintenance profile (keep PVE console access for the initial interface reassignment):

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

5. Stop the VM, check `opnsense-spare maintenance --dry-run`, then run `opnsense-spare maintenance` and confirm VM startup. **At the PVE console**, correct any old `vtnetN` maintenance interface assignment to the configured physical name; then verify management IP, gateway, WebGUI, and SSH. Reboot once more to verify idempotence.
6. Test emergency mode **with `--link-down` first**. Do not connect production MAC/IP addresses to the network while the physical firewall is online. Test the complete failback and export paths before using the spare in production.

### Firmware, serial console and CPU microcode

- **Boot firmware:** Use Proxmox **OVMF (UEFI)** and add an **EFI disk** when creating the VM. The physical appliance may use legacy BIOS: importing its `config.xml` does not change the VM firmware or bootloader. Do not change a working VM from BIOS to UEFI (or vice versa) without preparing its boot disk.
- **Add a serial port:** In the Proxmox GUI select **VM → Hardware → Add → Serial Port**, choose **socket**, port **0**. Alternatively, on the PVE host run `qm set <VMID> --serial0 socket` while the VM is stopped. Keep the VGA display available as well.
- **Use the console:** `qm terminal <VMID>` on the PVE host opens the serial console. In the Proxmox web UI, **Hardware → Display → Serial terminal 0** makes **Console** open the serial terminal (xterm.js); retain a VGA display if you want the normal noVNC console instead. A serial port alone does not cause OPNsense to write output there: under **System → Settings → Administration → Console**, enable **Serial console** as the primary or secondary console and use 115200 baud. Verify access after importing the physical configuration, which may change that setting.
- **CPU microcode:** The cold-spare import transfers only the XML configuration, not microcode binaries. A CPU microcode plugin listed in the physical firewall's configuration is not a reason to alter the imported XML: any update attempt inside the guest cannot update the physical CPU of the Proxmox host. Microcode management belongs to the host; installing or updating that plugin separately in the guest has no practical benefit.

**Boot dependency:** The hook intentionally fails closed if its valid mode ISO is missing or the mapping is invalid. Keep Proxmox console access and a VM backup available.

## Operation

Run on the **Proxmox host as root**. Mode changes require the VM to be **stopped**. The controller reads the current PVE configuration instead of keeping a separate persistent host state: a VM prepared by the script may be started manually with `qm start <VMID>`.

```sh
opnsense-spare maintenance
opnsense-spare emergency --link-down --transfer /path/to/physical-config.xml
opnsense-spare emergency --link-up --transfer /path/to/physical-config.xml
opnsense-spare emergency --link-up                     # reuse saved emergency profile
opnsense-spare maintenance --transfer /path/to/failback.xml
```

- **Maintenance** attaches an independently addressed LAN-client profile. The VM may coexist with the physical firewall. Its guest configuration is restored from the guest's saved maintenance profile.
- **Emergency** configures the production MAC addresses and Layer-2 mapping. For an initial emergency activation, use **`--transfer /path/to/physical-config.xml`** to import a physical firewall backup. Without that flag, the guest uses its **previously saved emergency profile**, if one exists. No import is inferred from a default file path.
- **Isolated test:** `--link-down` disconnects *all* emergency NICs. The physical firewall may remain running. Inspect the VM through the Proxmox console.
- **Live takeover:** `--link-up` connects emergency NICs and explicitly asks you to confirm that the physical firewall is off or isolated. The controller does not detect or perform failover automatically.
- **Start or prepare:** A successful mode command offers to start the VM. Answer `n` to leave it stopped and **ready for a manual `qm start`**. It can later be stopped and reconfigured using the normal command; no history reconciliation is needed.
- **Export destination:** Existing export destinations must be regular files (never directories or symlinks). New XML is staged before replacement; the previous export is retained as a timestamped backup.
- **PVE storage safety:** Before changing VM hardware, the controller verifies that the generated ISO resolves to the configured PVE storage path and rejects a non-CD-ROM device at `ide2`. These checks require working `pvesm path` on the PVE host.
- **Failback export:** `--transfer` with `maintenance` explicitly requests an SSH export of the saved **emergency** guest configuration *after* the maintenance VM boots. Without `--transfer`, there is no export prompt. You can also run `opnsense-spare maintenance --transfer /path/to/failback.xml` against an **already running maintenance VM**; this only exports, without modifying VM hardware. If you declined the VM start, do the export after manually starting it. Nothing is imported automatically on the physical firewall.
- **Failed preparation:** `qm set` is not an atomic multi-NIC operation. The controller disables PVE autostart before changing NICs, and reads back **all** NIC definitions and the attached ISO before offering a VM start. If any operation fails, the controller will not start the VM. **Do not manually start it while partially configured**; inspect `qm config <VMID>` and rerun the desired mode command to apply the complete profile.

The controller uses two stable ISO filenames (`opnsense-spare-<VMID>-maintenance.iso` and `opnsense-spare-<VMID>-emergency.iso`) and unique request IDs *inside* the ISO. Repeating preparation does not blindly reimport an earlier backup: importing requires an explicit `--transfer` on the new invocation. The guest hook still stores and selects the two persistent configurations.

**No persistent host state file or recovery mode is needed.** Existing JSON files may retain the historical `state_file` and `transfer_default` keys, but the simplified controller ignores them. The old `/var/lib/opnsense-spare/state.json` is not read or modified; it is no longer authoritative. A transient lock under `/run/lock` prevents concurrent *controller* invocations. Use an explicit `--transfer` path when an import or export is wanted. During export, SSH authentication or host-key verification failures abort immediately to avoid triggering intrusion-prevention lockouts; temporary connection failures during guest startup are retried until the configured timeout. The export uses an SSH agent (`"ssh_identity": null`) or configured identity file, with strict host-key verification. Optional integer ports `maintenance_ssh_port` and `emergency_ssh_port` (1–65535, or `null`/omitted) can be set independently. The controller currently only uses SSH for maintenance failback export, so `maintenance_ssh_port` explicitly overrides the OpenSSH client default, while `emergency_ssh_port` is reserved for possible future Emergency-mode SSH operations and has no effect today. If unset, OpenSSH's own port configuration applies. For example, set `"maintenance_ssh_port": 22` if the PVE host normally uses port 3000.

## Safety notes

- **Never run the physical firewall and a network-connected emergency VM simultaneously** with overlapping production IP/MAC addresses.
- Emergency switching is manual by design. `onboot=0` is enforced.
- Validate the PVE bridge wiring, VLAN tagging/untagging, WAN connectivity (DHCP or PPPoE), gateway, routing, DHCP, VPN, and firewall behavior for *your* topology.
- The mode ISO may contain a complete firewall configuration, **including secrets**. The controller creates the ISO with permission `0600`; protect PVE backups, snapshots, transfer files, and any old ISO images accordingly.
- The guest hook fails closed on absent/invalid media, invalid mapping, source-name conflicts, unexpected boot-hook failures, or an interrupted profile transition. A transition interrupted after configuration activation requires manual investigation using the OPNsense console; the marker is intentionally not cleared automatically. This may halt the VM and require console recovery.
- An older EFI loader can fail before OPNsense hooks run. Verify loader compatibility after major upgrades.

## Tests and status

```sh
python3 -m unittest -v test_host test_networks test_guest
python3 -m py_compile opnsense-spare test_host.py test_networks.py test_guest.py
sh -n 90-opnsense-spare
```

The automated tests exercise stopped/running workflows, manual starts, declined starts, explicit imports/exports, interrupted `qm set`, hardware read-back, ISO generation, VLAN validation, and two-/three-NIC configurations. They are **not** a substitute for the complete live cutover and failback test on your own topology.

### Project files

| File | Purpose |
|---|---|
| `opnsense-spare` | Proxmox host controller (Python 3) |
| `90-opnsense-spare` | Single OPNsense guest import hook (FreeBSD `/bin/sh`) |
| `opnsense-spare.json.example` | Example two-NIC configuration |
| `opnsense-spare-three-nic.json.example` | Example three-NIC topology |
| `test_host.py`, `test_networks.py`, `test_guest.py` | Unit, regression, and simulated guest boot tests |
| `CHANGELOG.md` | Development history |
