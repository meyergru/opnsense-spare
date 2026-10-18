# Migrating a legacy two-interface cold spare to the universal schema

This document applies to **existing installations only**. New installations should follow the main README. The current implementation has not yet been end-to-end validated on an OPNsense/Proxmox system.

1. Back up the VM, PVE controller, JSON, state file, guest hooks and `/conf/spare/` directory. Confirm that the VM is stopped and the actual guest mode is known.
2. Convert the JSON into a **different** output file:

   ```sh
   python3 migrate-v5-config.py /etc/opnsense-spare.json \
     --name vtnet0=ix1 --name vtnet1=igc3 \
     -o /etc/opnsense-spare-new.json
   ```

3. Review `interface_names`, `maintenance_client_nic`, MAC addresses, NIC inventory, `networks` profiles, bridges, VLANs, SSH settings and transfer path. Back up the old JSON before replacing it.
4. Replace the guest's `import/90-opnsense-spare` hook and remove the old `early/10-spare-interface-names` hook; they must not both rename interfaces.
5. Replace the PVE host controller. With the VM stopped, run `opnsense-spare maintenance` to attach a **new mode ISO** containing `interfaces.conf`; do not use the current guest hook with an old mode ISO lacking `interfaces.conf`.
6. Boot **maintenance first** and check NIC renaming and IP/gateway/SSH from the PVE console. Reboot again. Only then test `emergency --link-down`.

If the old state is `prepared` or `incomplete`, resolve the last actual guest mode before migrating. Never manually mark a transition successful on the basis of a configured (but not booted) VM.
