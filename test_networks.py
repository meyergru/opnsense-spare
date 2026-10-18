"""Regression tests for generic two- and three-NIC topologies."""
import importlib.machinery
import importlib.util
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
loader=importlib.machinery.SourceFileLoader('spare_networks',str(HERE/'opnsense-spare'))
spec=importlib.util.spec_from_loader(loader.name,loader)
spare=importlib.util.module_from_spec(spec)
loader.exec_module(spare)

class UniversalTopologyTest(unittest.TestCase):
    def cfg(self, directory):
        c=json.loads((HERE/'opnsense-spare-three-nic.json.example').read_text())
        c.update(iso_dir=str(directory),maintenance_ssh_host='192.168.10.15')
        return c

    def test_three_nic_layout_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.cfg(td);spare.validate_config(c)
            self.assertEqual(spare.nic_keys(c),['net0','net1','net2'])
            self.assertEqual(spare.ifmap_bytes(c),b'vtnet0=igc0\nvtnet1=igc1\nvtnet2=igc3\n')

    def test_three_nic_iso_self_contained(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.cfg(td);calls=[]
            def xorriso(*argv,**kw):
                src=Path(argv[-1]);data=(src/'interfaces.conf').read_bytes()
                self.assertIn('ifmap_sha256='+hashlib.sha256(data).hexdigest(),(src/'request.conf').read_text())
                calls.append(data)
                Path(argv[argv.index('-o')+1]).write_bytes(b'testiso')
            with patch.object(spare,'run',side_effect=xorriso):
                spare.make_iso(c,'maintenance',None)
                spare.make_iso(c,'emergency',None)
            self.assertEqual(calls,[spare.ifmap_bytes(c)]*2)

    def test_reject_missing_mappings_collisions_or_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            original=self.cfg(td)
            for change in (lambda c:c['interface_names'].pop('vtnet2'),
                           lambda c:c['interface_names'].update({'vtnet2':'igc0'}),
                           lambda c:c['interface_names'].update({'vtnet0':'lo0'}),
                           lambda c:c['networks']['emergency'].pop('net2')):
                c=copy.deepcopy(original);change(c)
                with self.assertRaises(RuntimeError):spare.validate_config(c)
            c=copy.deepcopy(original)
            c['networks']['maintenance']['net1']=c['networks']['maintenance']['net1'].replace(',link_down=1','')
            with self.assertRaisesRegex(RuntimeError,'link_down'):spare.validate_config(c)
            c=copy.deepcopy(original)
            c['networks']['maintenance']['net0']=c['networks']['maintenance']['net0'].replace('02:00:00:00:10:01','02:00:00:00:20:01')
            with self.assertRaisesRegex(RuntimeError,'MAC'):spare.validate_config(c)

    def test_onboot_disabled_before_any_nic_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.cfg(td);commands=[]
            with patch.object(spare,'run',side_effect=lambda *a,**kw:commands.append(a)):
                spare.qemu_net(c,c['networks']['emergency'])
            self.assertEqual([a[3] for a in commands],['--onboot','--net0','--net1','--net2'])

    def test_three_nic_hardware_verification(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.cfg(td);iso=spare.iso_name(c,'emergency')
            nets={k:spare.set_link_down(v,True) for k,v in c['networks']['emergency'].items()}
            hardware=''.join(f'{k}: {v}\n' for k,v in nets.items())+f'ide2: local:iso/{iso},media=cdrom\nonboot: 0\n'
            spare.verify_hardware(c,hardware,nets,iso)
            with self.assertRaisesRegex(RuntimeError,'net2'):
                spare.verify_hardware(c,hardware.replace('vmbrWAN','vmbrWRONG'),nets,iso)
            with self.assertRaisesRegex(RuntimeError,'automatic startup'):
                spare.verify_hardware(c,hardware.replace('onboot: 0','onboot: 1'),nets,iso)
            with self.assertRaisesRegex(RuntimeError,'ISO'):
                spare.verify_hardware(c,hardware.replace('iso/'+iso,'iso/old.iso'),nets,iso)

    def test_optional_off_flags_may_be_omitted_by_pve(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.cfg(td);iso=spare.iso_name(c,'emergency')
            nets={k:spare.set_link_down(v,False) for k,v in c['networks']['emergency'].items()}
            actual=''.join(f'{k}: {v.replace(",link_down=0", "").replace(",firewall=0", "")}\n'
                           for k,v in nets.items())+f'ide2: local:iso/{iso},media=cdrom\n'
            spare.verify_hardware(c,actual,nets,iso)

    def test_guest_script_syntax_and_early_mapping(self):
        path=HERE/'90-opnsense-spare'
        subprocess.run(['sh','-n',str(path)],check=True)
        text=path.read_text()
        self.assertIn('ifmap_sha256',text)
        self.assertIn('SHA256 mismatch',text)
        self.assertIn('/sbin/ifconfig "$SRC" name "$DST"',text)
        self.assertLess(text.index('network interfaces renamed'),text.index('The request ID makes booting repeatedly'))

if __name__ == '__main__':unittest.main()
