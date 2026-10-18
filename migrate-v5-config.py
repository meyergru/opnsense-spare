#!/usr/bin/env python3
"""Convert a legacy two-interface host JSON to the universal schema; does not mutate input or Proxmox VM.

Example: ./migrate-v5-config.py /etc/opnsense-spare.json \
  --name vtnet0=ix1 --name vtnet1=igc3 -o /etc/opnsense-spare-new.json
"""
import argparse
import json
import re
from pathlib import Path


def convert(old, names):
    if 'networks' in old:
        raise ValueError('Input already appears to use the universal schema')
    slots = []
    for k in old:
        m = re.fullmatch(r'net(\d+)_(?:maintenance|emergency)', k)
        if m:
            slots.append(int(m.group(1)))
    slots = sorted(set(slots))
    if not slots or slots != list(range(len(slots))):
        raise ValueError('Expected contiguous legacy net0_*, net1_* ...')
    if set(names) != {f'vtnet{i}' for i in slots}:
        raise ValueError('Supply exactly one --name vtnetN=TARGET for every netN slot')
    for i in slots:
        for mode in ('maintenance', 'emergency'):
            if f'net{i}_{mode}' not in old:
                raise ValueError(f'Missing net{i}_{mode}')
    result = {k: v for k, v in old.items() if not re.fullmatch(r'net\d+_(?:maintenance|emergency)', k)}
    result['interface_names'] = names
    result['maintenance_client_nic'] = 'net0'
    result['networks'] = {mode: {f'net{i}': old[f'net{i}_{mode}'] for i in slots}
                          for mode in ('maintenance', 'emergency')}
    result['emergency_instructions'] = ('Power off the physical OPNsense and '
                                        'move the WAN connection to the spare.')
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('old_json')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--name', action='append', required=True, metavar='vtnetN=TARGET')
    args = ap.parse_args()
    names = {}
    for item in args.name:
        if '=' not in item:
            ap.error(f'Invalid --name {item!r}')
        src, dst = item.split('=', 1)
        if src in names:
            ap.error(f'Duplicate {src}')
        names[src] = dst
    src_path, dst_path = Path(args.old_json), Path(args.output)
    if src_path.resolve() == dst_path.resolve():
        ap.error('Input and output must differ. Review migrated JSON before replacing live config.')
    if dst_path.exists():
        ap.error(f'Output already exists: {dst_path}')
    cfg = convert(json.loads(src_path.read_text()), names)
    with dst_path.open('x') as fd:
        fd.write(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    dst_path.chmod(0o600)
    print(f'Created {dst_path}; review profile and install the current guest hook before starting VM.')

if __name__ == '__main__':
    main()
