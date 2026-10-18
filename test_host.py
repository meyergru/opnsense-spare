"""Host-controller regression tests for stateless, manually operable cold spare."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader('spare', str(HERE / 'opnsense-spare'))
spec = importlib.util.spec_from_loader(loader.name, loader)
spare = importlib.util.module_from_spec(spec)
loader.exec_module(spare)
EXAMPLE = b'<?xml version="1.0"?><opnsense><interfaces/></opnsense>'


class Harness:
    def __init__(self, case, root, three=False):
        self.case = case
        example = 'opnsense-spare-three-nic.json.example' if three else 'opnsense-spare.json.example'
        self.cfg = json.loads((HERE / example).read_text())
        self.cfg.update(iso_dir=str(root), maintenance_ssh_host='192.168.10.99')
        self.path = root / 'config.json'
        self.path.write_text(json.dumps(self.cfg))
        self.mode = 'maintenance'
        self.status = 'stopped'
        self.nets = dict(self.cfg['networks']['maintenance'])
        self.iso = 'local:iso/old.iso'
        self.onboot = '0'
        self.commands = []
        self.errors = {}
        self.root = root
        self.isos = 0

    def run(self, *cmd, **kw):
        self.commands.append(cmd)
        if cmd[:2] == ('pvesm', 'path'):
            return str(self.root / cmd[2].split('/')[-1]).encode()
        if cmd[:2] == ('qm', 'status'):
            return f'status: {self.status}\n'.encode()
        if cmd[:2] == ('qm', 'config'):
            return (''.join(f'{k}: {v}\n' for k,v in self.nets.items())+
                    f'ide2: {self.iso},media=cdrom\nonboot: {self.onboot}\n').encode()
        if cmd[:2] == ('qm', 'set'):
            if cmd[3] in self.errors:
                raise RuntimeError(self.errors[cmd[3]])
            if cmd[3].startswith('--net'):
                self.nets[cmd[3][2:]] = cmd[4]
            if cmd[3] == '--onboot':
                self.onboot = cmd[4]
            if cmd[3] == '--ide2':
                self.iso = cmd[4].split(',')[0]
            return b''
        if cmd[:2] == ('qm', 'start'):
            if 'start' in self.errors:
                raise RuntimeError(self.errors['start'])
            self.status = 'running'
            return b''
        raise AssertionError(cmd)

    def make_iso(self, cfg, mode, transfer):
        self.isos += 1
        assert transfer is None or Path(transfer).is_file()
        name = spare.iso_name(cfg, mode)
        path = self.root / name
        path.write_bytes(f'fake-iso-{self.isos}'.encode())
        return ('f'*32, name, path)

    def invoke(self, mode, flags=(), answers=(), error=None):
        argv=['opnsense-spare',mode,'--config',str(self.path),*flags]
        with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
             patch.object(spare,'run',side_effect=self.run), \
             patch.object(spare,'make_iso',side_effect=self.make_iso), \
             patch.object(spare.shutil,'which',return_value='/usr/bin/xorriso'), \
             patch('builtins.input',side_effect=answers):
            if error:
                with self.case.assertRaisesRegex(RuntimeError,error):
                    spare.main()
            else:
                spare.main()

    def count(self, command):
        return sum(1 for args in self.commands if args[:len(command)] == command)


class StatelessControllerTests(unittest.TestCase):
    def test_xml_and_vlan_validation(self):
        self.assertEqual(spare.validate_xml(EXAMPLE,'test'),EXAMPLE)
        for s in (b'<bad/>',b'<opnsense>',b'not xml'):
            with self.assertRaises(RuntimeError):spare.validate_xml(s,'test')
        valid='virtio=02:00:00:00:20:01,bridge=vmbr0,tag=4094,trunks=1;4;50-56;666,link_down=1'
        for s in (valid.replace('50-56','i53'),valid.replace('666','4095'),
                  valid.replace('50-56','56-50'),valid.replace('1;4','1;;4')):
            with self.assertRaises(RuntimeError):spare.parse_net_profile('net0',s)

    def test_iso_contains_checksums_and_new_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); imported=root/'source.xml'; imported.write_bytes(EXAMPLE)
            cfg={'vmid':999,'iso_dir':td,'interface_names':{'vtnet0':'ix1','vtnet1':'igc3'}}
            requests=[]
            def xorriso(*argv,**kw):
                src=Path(argv[-1]); req=(src/'request.conf').read_text()
                requests.append(req)
                self.assertIn('ifmap_sha256=',req)
                self.assertIn('vtnet0=ix1\nvtnet1=igc3\n',(src/'interfaces.conf').read_text())
                if len(requests)==1:
                    self.assertIn('import=1',req)
                    self.assertEqual((src/'config.xml').read_bytes(),EXAMPLE)
                Path(argv[argv.index('-o')+1]).write_bytes(req.encode())
            with patch.object(spare,'run',side_effect=xorriso):
                i1,n1,p1=spare.make_iso(cfg,'emergency',imported)
                i2,n2,p2=spare.make_iso(cfg,'emergency',None)
            self.assertEqual(n1,n2)
            self.assertNotEqual(i1,i2)
            self.assertIn('import=0',p2.read_text())
            self.assertEqual(p2.stat().st_mode & 0o777,0o600)

    def test_guest_config_is_unchanged(self):
        guest=(HERE/'90-opnsense-spare').read_text()
        self.assertIn('ifmap_sha256',guest)
        self.assertIn('/sbin/ifconfig "$SRC" name "$DST"',guest)

    def test_prepared_no_start_manual_start_then_maintenance(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            source=Path(td)/'physical.xml'; source.write_bytes(EXAMPLE)
            h.invoke('emergency',['--link-down','--transfer',str(source)],['n'])
            self.assertEqual(h.count(('qm','start')),0)
            self.assertIn('emergency.iso',h.iso)
            self.assertFalse((Path(td)/'state.json').exists())
            h.status='running'  # Manually qm start 999 (outside controller)
            h.status='stopped'  # Subsequently qm stop 999
            h.invoke('maintenance',answers=['n'])
            self.assertIn('maintenance.iso',h.iso)
            self.assertEqual(h.count(('qm','start')),0)

    def test_live_emergency_confirmation_refused_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            h.invoke('emergency',['--link-up'],['n'],error='Not confirmed')
            self.assertEqual(h.count(('qm','set')),0)
            self.assertEqual(h.isos,0)

    def test_live_emergency_mac_and_vlan_and_boot(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td),three=True)
            source=Path(td)/'physical.xml'; source.write_bytes(EXAMPLE)
            h.invoke('emergency',['--link-up','--transfer',str(source)],['y','y'])
            self.assertEqual(h.status,'running')
            self.assertEqual(h.count(('qm','start')),1)
            self.assertEqual(h.count(('qm','set','999','--net2')),1)
            self.assertEqual(h.count(('qm','set','999','--onboot')),1)
            for value in h.nets.values():
                self.assertIn('link_down=0',value)
            self.assertEqual(h.onboot,'0')

    def test_failed_second_nic_never_starts(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            h.errors['--net1']='Simulated qm set failure'
            h.invoke('emergency',['--link-down'],error='Simulated qm set failure')
            self.assertEqual(h.count(('qm','start')),0)
            self.assertEqual(h.onboot,'0')
            self.assertEqual(h.nets['net1'],h.cfg['networks']['maintenance']['net1'])
            h.errors.clear()
            h.invoke('maintenance',answers=['n'])
            self.assertIn('maintenance.iso',h.iso)

    def test_silent_pve_nic_mismatch_prevents_start(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            real_run=h.run
            def ignored_net1(*args,**kw):
                if args[:4]==('qm','set','999','--net1'):
                    h.commands.append(args); return b''
                return real_run(*args,**kw)
            with patch.object(h,'run',side_effect=ignored_net1):
                h.invoke('emergency',['--link-down'],error='did not apply net1')
            self.assertEqual(h.count(('qm','start')),0)

    def test_iso_mismatch_prevents_start(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            real_run=h.run
            def ignored_iso(*args,**kw):
                if args[:4]==('qm','set','999','--ide2'):
                    h.commands.append(args); return b''
                return real_run(*args,**kw)
            with patch.object(h,'run',side_effect=ignored_iso):
                h.invoke('emergency',['--link-down'],error='did not attach')
            self.assertEqual(h.count(('qm','start')),0)

    def test_export_rejects_directory_without_ssh_or_vm_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            h = Harness(self, Path(td))
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                spare.validate_export_destination(Path(td))
            with patch.object(spare.subprocess, 'run') as ssh:
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    spare.maintenance_export(h.cfg, Path(td))
                ssh.assert_not_called()
            h.invoke('maintenance', ['--transfer', td], error='regular file')
            self.assertEqual(h.count(('qm', 'set')), 0)

    def test_iso_storage_resolves_to_generated_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = {'iso_dir': td, 'iso_storage': 'local'}
            iso = 'opnsense-spare-999-maintenance.iso'
            with patch.object(spare, 'run', return_value=(td+'/'+iso+'\\n').encode()):
                self.assertEqual(spare.verify_iso_storage(cfg, iso), 'local:iso/'+iso)
            with patch.object(spare, 'run', return_value=b'/wrong/location.iso\\n'):
                with self.assertRaisesRegex(RuntimeError, 'ISO storage mismatch'):
                    spare.verify_iso_storage(cfg, iso)

    def test_non_cdrom_ide2_rejected(self):
        spare.verify_optical_slot('ide2: local:iso/test.iso,media=cdrom\\n')
        spare.verify_optical_slot('net0: virtio=02:00:00:00:00:01\\n')
        with self.assertRaisesRegex(RuntimeError, 'non-CD-ROM'):
            spare.verify_optical_slot('ide2: local-lvm:vm-999-disk-0,size=8G\\n')

    def test_export_replacement_preserves_old_file_during_staging_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dest = root / 'out.xml'
            dest.write_bytes(EXAMPLE)
            bad = b'<?xml version="1.0"?><opnsense><other/></opnsense>'
            cfg = Harness(self, root).cfg
            def fake_ssh(*args, **kwargs):
                p = MagicMock()
                p.returncode = 0
                p.stdout = bad
                return p
            with patch.object(spare.subprocess, 'run', side_effect=fake_ssh), \\
                 patch.object(spare.os, 'fdopen', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    spare.maintenance_export(cfg, dest)
            self.assertEqual(dest.read_bytes(), EXAMPLE)

    def test_export_without_transfer_is_never_automatic(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            with patch.object(spare,'maintenance_export') as export:
                h.invoke('maintenance',answers=['y'])
                export.assert_not_called()
            self.assertEqual(h.status,'running')

    def test_running_maintenance_explicit_export_only(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            h.invoke('maintenance',answers=['y'])
            target=Path(td)/'backup.xml'
            with patch.object(spare,'maintenance_export') as export:
                h.invoke('maintenance',['--transfer',str(target)])
                export.assert_called_once_with(h.cfg,target)
            self.assertEqual(h.count(('qm','set')),4) # only first prepare, no second mutation

    def test_running_emergency_cannot_be_reconfigured(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            h.invoke('emergency',['--link-down'],['y'])
            n=h.count(('qm','set'))
            h.invoke('maintenance',error='VM is running')
            self.assertEqual(h.count(('qm','set')),n)

    def test_maintenance_export_deferred_when_start_declined(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            dest=Path(td)/'export.xml'
            with patch.object(spare,'maintenance_export') as export:
                h.invoke('maintenance',['--transfer',str(dest)],['n'])
                export.assert_not_called()
                h.status='running'  # manual start
                h.invoke('maintenance',['--transfer',str(dest)])
                export.assert_called_once_with(h.cfg,dest)

    def test_no_implicit_transfer_default(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            # The legacy sample contains transfer_default; no transfer requested.
            h.invoke('emergency',['--link-down'],['n'])
            self.assertEqual(h.count(('qm','start')),0)
            self.assertEqual(h.isos,1)

    def test_forged_running_maintenance_profile_refused(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            h.invoke('emergency',['--link-down'],['n'])
            h.status='running'
            with patch.object(spare,'maintenance_export') as export:
                h.invoke('maintenance',['--transfer',str(Path(td)/'x.xml')],error='did not apply net0')
                export.assert_not_called()

    def test_invalid_import_fails_before_qm_set(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            bad=Path(td)/'bad.xml';bad.write_text('<not-opnsense/>')
            h.invoke('emergency',['--link-down','--transfer',str(bad)],error='root element')
            self.assertEqual(h.count(('qm','set')),0)

    def test_dry_run_makes_no_changes_and_no_import_prompts(self):
        with tempfile.TemporaryDirectory() as td:
            h=Harness(self,Path(td))
            h.invoke('emergency',['--dry-run'])
            self.assertEqual(h.count(('qm','set')),0)
            self.assertEqual(h.isos,0)


if __name__=='__main__':
    unittest.main()
