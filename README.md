# OPNsense Cold Spare for Proxmox VE

A **cold standby OPNsense virtual machine** that can temporarily replace a physical OPNsense firewall, while normally operating as an independent maintenance client. The project is **not** a CARP/pfsync high-availability cluster and does not provide automatic failover.

> **Development status:** Python unit tests and shell syntax checks have passed, but **the current implementation has not yet undergone a complete OPNsense/FreeBSD and Proxmox integration test**. In particular, the consolidated import hook's NIC renaming and the end-to-end WAN and failback operations must be verified on the target system before relying on it for disaster recovery. Keep a working backup and out-of-band console access.

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
- **CPU microcode:** The physical OPNsense may have an `os-cpu-microcode-*` plugin. The cold-spare import copies **XML only**: it does not itself transfer microcode binaries, install the plugin, or change the physical CPU. The imported configuration may still record installed plugins; do not subsequently synchronize/install a microcode plugin inside the guest. Inside a VM, microcode updates belong on the **Proxmox host**, not in the guest. Installing or forcing early microcode loading in the VM is unnecessary and can cause boot problems; do not copy hardware-specific loader settings blindly.

**Boot dependency:** The hook intentionally fails closed if its valid mode ISO is missing or the mapping is invalid. Keep Proxmox console access and a VM backup available.

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
- The mode ISO may contain a complete firewall configuration, **including secrets**. The controller creates the ISO with permission `0600`; protect PVE backups, snapshots, transfer files, and any old ISO images accordingly.
- The guest hook fails closed on absent/invalid media, invalid mapping, source-name conflicts, or an interrupted profile transition. This may halt the VM and require console recovery.
- An older EFI loader can fail before OPNsense hooks run. Verify loader compatibility after major upgrades.

## Tests and status

```sh
python3 -m unittest -v test_host test_networks
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
| `test_host.py`, `test_networks.py` | Unit and regression tests |
| `CHANGELOG.md` | Development history |
