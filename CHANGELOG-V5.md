# V5 — Host state-machine fixes

- Answering `n` to the VM startup prompt leaves `prepared` rather than `incomplete`. A successfully configured but not booted profile must not be recorded as running.
- Subsequent invocations verify the prepared NIC settings and ISO SHA-256 before reconfiguring. A manually started VM cannot be reconciled automatically.
- Pending failback exports no longer block emergency mode unconditionally. Operators can explicitly decline the pending export after confirmation.
- `--recover-incomplete` can classify the V4 declined-start failure as prepared if all NIC profiles and the ISO match. Recovery from unknown partial failures remains conservative.
- Guest hook and host JSON format are unchanged from V4.

**Migration caution:** A previously hand-edited `state.json` cannot be reconstructed automatically. Check the true guest/host mode before another production boot.
