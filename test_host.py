import importlib.machinery
import importlib.util
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

S = Path(__file__).with_name('opnsense-spare')
loader = importlib.machinery.SourceFileLoader('spare', str(S))
spec = importlib.util.spec_from_loader(loader.name, loader)
spare = importlib.util.module_from_spec(spec)
loader.exec_module(spare)

EXAMPLE = b'<?xml version="1.0"?><opnsense><interfaces/></opnsense>'

class TestHost(unittest.TestCase):
    def test_import_validation(self):
        self.assertEqual(spare.validate_xml(EXAMPLE, 'test'), EXAMPLE)
        for value in (b'<bad/>', b'<opnsense>', b'not xml'):
            with self.assertRaises(RuntimeError):
                spare.validate_xml(value, 'test')

    def test_atomic_state(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sub' / 'state.json'
            spare.save_state(p, {'mode':'emergency', 'pending_export':True})
            self.assertEqual(spare.load_state(p)['mode'], 'emergency')
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_iso_contains_import_checksum_and_request(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            backup = dest / 'backup.xml'
            backup.write_bytes(EXAMPLE)
            fake_cfg = dict(vmid=123, iso_dir=str(dest), interface_names={'vtnet0':'ix1', 'vtnet1':'igc3'})
            def fake_xorriso(*args, **kwargs):
                self.assertEqual(args[0], 'xorriso')
                target = Path(args[args.index('-o') + 1])
                stage = Path(args[-1])
                request = (stage/'request.conf').read_text()
                self.assertIn('mode=emergency', request)
                self.assertIn('import=1', request)
                self.assertIn('sha256=', request)
                self.assertIn('ifmap_sha256=', request)
                self.assertEqual((stage/'interfaces.conf').read_text(), 'vtnet0=ix1\nvtnet1=igc3\n')
                self.assertEqual((stage/'config.xml').read_bytes(), EXAMPLE)
                target.write_bytes(b'fake ISO')
            with patch.object(spare, 'run', side_effect=fake_xorriso):
                ident, name, iso = spare.make_iso(fake_cfg, 'emergency', backup)
                self.assertEqual(len(ident), 32)
                self.assertTrue(iso.exists())
                self.assertEqual(iso.stat().st_mode & 0o777, 0o600)

    def test_fixed_iso_names_and_unique_embedded_requests(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            backup = dest / 'backup.xml'
            backup.write_bytes(EXAMPLE)
            cfg = dict(vmid=999, iso_dir=str(dest), interface_names={'vtnet0':'ix1', 'vtnet1':'igc3'})
            seen_requests = []
            def fake_xorriso(*args, **kwargs):
                candidate = Path(args[args.index('-o') + 1])
                stage = Path(args[-1])
                contents = (stage/'request.conf').read_text()
                seen_requests.append(contents)
                candidate.write_bytes(contents.encode())
            with patch.object(spare, 'run', side_effect=fake_xorriso):
                ident1, name1, p1 = spare.make_iso(cfg, 'emergency', backup)
                ident2, name2, p2 = spare.make_iso(cfg, 'emergency', None)
                ident3, name3, p3 = spare.make_iso(cfg, 'maintenance', None)
            self.assertEqual(name1, 'opnsense-spare-999-emergency.iso')
            self.assertEqual(name1, name2)
            self.assertEqual(p1, p2)
            self.assertEqual(name3, 'opnsense-spare-999-maintenance.iso')
            self.assertNotEqual(ident1, ident2)
            self.assertNotEqual(ident2, ident3)
            self.assertIn(f'request_id={ident2}', p2.read_text())
            self.assertIn('import=0', p2.read_text())
            self.assertIn('ifmap_sha256=', p2.read_text())
            self.assertEqual(set(x.name for x in dest.glob('opnsense-spare-*.iso')),
                             {name1, name3})
            self.assertEqual(p2.stat().st_mode & 0o777, 0o600)

    def test_link_down_override_both_directions(self):
        source='virtio=02:00:00:00:20:01,bridge=vmbr0,tag=4094,trunks=1;10,link_down=1,firewall=0'
        self.assertEqual(spare.set_link_down(source, True).count('link_down='), 1)
        self.assertEqual(spare.parse_net_profile('down',spare.set_link_down(source, True))['link_down'],'1')
        self.assertEqual(spare.parse_net_profile('up',spare.set_link_down(source, False))['link_down'],'0')
        self.assertEqual(spare.set_link_down(spare.set_link_down(source, False), True), spare.set_link_down(source, True))

    def test_vlan_preflight_rejects_invalid_trunk_before_qm(self):
        valid = "virtio=02:00:00:00:20:01,bridge=vmbr0,tag=4094,trunks=1;4;50-56;666,link_down=1"
        parsed = spare.parse_net_profile('net0_emergency', valid)
        self.assertEqual(parsed['trunks'], '1;4;50-56;666')
        for broken in (
            valid.replace('50-56', 'i53'),
            valid.replace('666', '4095'),
            valid.replace('50-56', '56-50'),
            valid.replace('1;4', '1;;4'),
            valid.replace('tag=4094', 'tag=5000'),
            valid.replace('link_down=1', 'link_down=true'),
        ):
            with self.subTest(value=broken), self.assertRaises(RuntimeError):
                spare.parse_net_profile('net0_emergency', broken)

    def test_confirmations_accept_simple_j_and_n(self):
        with patch('builtins.input', return_value='j'):
            self.assertTrue(spare.ask('Test', False))
            spare.exact_confirm()
        with patch('builtins.input', return_value='n'):
            self.assertFalse(spare.ask('Test', True))
            with self.assertRaises(RuntimeError):
                spare.exact_confirm()
        with patch('builtins.input', side_effect=['maybe', 'j']):
            self.assertTrue(spare.ask('Test', False))

    def test_export_fetches_correct_emergency_profile(self):
        with tempfile.TemporaryDirectory() as d:
            ident = Path(d) / 'ssh_key'
            ident.write_text('test')
            target = Path(d) / 'export.xml'
            config = dict(ssh_identity=str(ident), maintenance_ssh_user='root',
                          maintenance_ssh_host='192.168.10.99', ssh_timeout_seconds=10)
            proc = MagicMock(returncode=0, stdout=EXAMPLE, stderr=b'')
            with patch.object(spare.subprocess, 'run', return_value=proc) as ssh:
                spare.maintenance_export(config, target)
                args = ssh.call_args.args[0]
                self.assertEqual(args[-1], 'cat /conf/spare/emergency.xml')
                self.assertEqual(target.read_bytes(), EXAMPLE)

    def test_agent_export_without_identity_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'export.xml'
            config = dict(maintenance_ssh_user='root',
                          maintenance_ssh_host='192.168.10.99',
                          ssh_timeout_seconds=10, ssh_identity=None)
            proc = MagicMock(returncode=0, stdout=EXAMPLE, stderr=b'')
            with patch.object(spare.subprocess, 'run', return_value=proc) as ssh:
                spare.maintenance_export(config, target)
                args = ssh.call_args.args[0]
                self.assertNotIn('-i', args)
                self.assertIn('BatchMode=yes', args)
                self.assertIn('StrictHostKeyChecking=yes', args)
                self.assertEqual(args[-1], 'cat /conf/spare/emergency.xml')
                self.assertEqual(target.read_bytes(), EXAMPLE)

    def test_omitted_ssh_identity_is_valid_and_explicit_key_still_checked(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(Path(__file__).with_name('opnsense-spare.json.example').read_text())
            cfg['iso_dir'] = d
            cfg['maintenance_ssh_host'] = '192.168.10.99'
            del cfg['ssh_identity']
            spare.validate_config(cfg)
            cfg['ssh_identity'] = None
            spare.validate_config(cfg)
            cfg['ssh_identity'] = 123
            with self.assertRaises(RuntimeError):
                spare.validate_config(cfg)
            with self.assertRaises(RuntimeError):
                spare.maintenance_export(dict(maintenance_ssh_user='root',
                          maintenance_ssh_host='192.168.10.99',
                          ssh_timeout_seconds=10,
                          ssh_identity='/nonexistent/opnsense_spare_private_key'), Path(d)/'export.xml')

    def test_recover_incomplete_only_if_old_nics_and_iso_remain(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            td = Path(d)
            cfg = json.loads(Path(__file__).with_name('opnsense-spare.json.example').read_text())
            cfg.update(iso_dir=d, maintenance_ssh_host='192.168.10.99',
                       state_file=str(td/'state.json'))
            cfg_path = td/'config.json'
            cfg_path.write_text(json.dumps(cfg))
            ident = 'a'*32
            state_path = td/'state.json'
            spare.save_state(state_path, {'mode':'maintenance', 'pending_export':False,
                                          'last_request':ident,
                                          'incomplete':{'target':'emergency'}})
            iso = f'opnsense-spare-999-{ident}.iso'
            good_vm = (f"net0: {cfg['networks']['maintenance']['net0']}\n"
                       f"net1: {cfg['networks']['maintenance']['net1']}\n"
                       f"ide2: local:iso/{iso},media=cdrom\n")
            def make_run(vm_config):
                def _run(*argv, **kwargs):
                    if argv[:2] == ('qm','status'):
                        return b'status: stopped\n'
                    if argv[:2] == ('qm','config'):
                        return vm_config.encode()
                    raise AssertionError(f'Unexpected command: {argv}')
                return _run
            argv = ['opnsense-spare','maintenance','--config',str(cfg_path), '--recover-incomplete']
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=make_run(good_vm)), patch('builtins.input',return_value='j'):
                spare.main()
            self.assertNotIn('incomplete',spare.load_state(state_path))

            # A partial change must not allow recovery to clear the marker.
            spare.save_state(state_path, {'mode':'maintenance', 'pending_export':False,
                                          'last_request':ident,
                                          'incomplete':{'target':'emergency'}})
            modified_vm=good_vm.replace('tag=10','tag=4094')
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=make_run(modified_vm)):
                with self.assertRaises(RuntimeError):
                    spare.main()
            self.assertIn('incomplete',spare.load_state(state_path))

# Regression tests for the V4 bug: 'n' to boot must NOT be an incomplete failure.
class TestV5HostWorkflow(unittest.TestCase):
    def _cfg(self, root):
        import json
        cfg = json.loads(Path(__file__).with_name('opnsense-spare.json.example').read_text())
        cfg.update(iso_dir=str(root), state_file=str(root/'state.json'),
                   maintenance_ssh_host='192.168.10.99', ssh_identity=None)
        path=root/'cfg.json'
        path.write_text(json.dumps(cfg))
        return cfg, path

    def test_no_start_is_prepared_then_restart_can_switch_profile(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            cfg,cfg_path=self._cfg(root)
            spare.save_state(root/'state.json', {'mode':'maintenance','pending_export':False})
            vnets={'net0':cfg['networks']['maintenance']['net0'], 'net1':cfg['networks']['maintenance']['net1']}
            iso={'name':'local:iso/example.iso'}
            status={'value':'stopped'}
            count={'iso':0,'start':0}
            def fake_run(*args,**kw):
                if args[:2] == ('qm','status'): return f'status: {status["value"]}\n'.encode()
                if args[:2] == ('qm','config'):
                    return (f'net0: {vnets["net0"]}\nnet1: {vnets["net1"]}\n'
                            f'ide2: {iso["name"]},media=cdrom\n').encode()
                if args[:2] == ('qm','set'):
                    if args[3] in ('--net0','--net1'):
                        vnets[args[3][2:]]=args[4]
                    if args[3] == '--ide2': iso['name']=args[4].split(',')[0]
                    return b''
                if args[:2] == ('qm','start'):
                    count['start']+=1
                    status['value']='running'
                    return b''
                raise AssertionError(f'unexpected command {args}')
            def fake_make(cfg,mode,src):
                count['iso']+=1
                name=spare.iso_name(cfg,mode)
                path=root/name
                path.write_bytes(f'iso-{count["iso"]}'.encode())
                return ('f'*32,name,path)
            argv=['opnsense-spare','emergency','--link-down','--config',str(cfg_path)]
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=fake_run), \
                 patch.object(spare,'make_iso',side_effect=fake_make), \
                 patch.object(spare.shutil,'which',return_value='/usr/bin/xorriso'), \
                 patch('builtins.input',side_effect=['n','n']):
                spare.main()     # First n = import, second n = do not boot.
            st=spare.load_state(root/'state.json')
            self.assertEqual(st['mode'],'maintenance')
            self.assertEqual(st['prepared']['mode'],'emergency')
            self.assertIn('net0',st['prepared']['networks'])
            self.assertNotIn('incomplete',st)
            self.assertEqual(count['start'],0)
            # Another call can rebuild prepared emergency and actually boot.
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=fake_run), \
                 patch.object(spare,'make_iso',side_effect=fake_make), \
                 patch.object(spare.shutil,'which',return_value='/usr/bin/xorriso'), \
                 patch('builtins.input',side_effect=['j','n','j']):
                spare.main()   # prepared check yes, import no, start yes
            st=spare.load_state(root/'state.json')
            self.assertEqual(st['mode'],'emergency')
            self.assertNotIn('prepared',st)
            self.assertNotIn('incomplete',st)
            self.assertEqual(count['start'],1)

    def test_v4_no_start_recovered_as_prepared_and_does_not_commit_guest_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            cfg,cfg_path=self._cfg(root)
            spare.save_state(root/'state.json', {'mode':'maintenance','pending_export':False,
                                                'incomplete':{'target':'emergency'}})
            net0=spare.set_link_down(cfg['networks']['emergency']['net0'],True)
            net1=spare.set_link_down(cfg['networks']['emergency']['net1'],True)
            name=spare.iso_name(cfg,'emergency')
            (root/name).write_bytes(b'previous V4 ISO')
            actual=(f'net0: {net0}\nnet1: {net1}\n'
                    f'ide2: local:iso/{name},media=cdrom\n')
            def fake_run(*args,**kw):
                if args[:2] == ('qm','status'): return b'status: stopped\n'
                if args[:2] == ('qm','config'): return actual.encode()
                raise AssertionError(args)
            argv=['opnsense-spare','emergency','--config',str(cfg_path),'--recover-incomplete']
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=fake_run), patch('builtins.input',return_value='j'):
                spare.main()
            st=spare.load_state(root/'state.json')
            self.assertNotIn('incomplete',st)
            self.assertEqual(st['prepared']['mode'],'emergency')
            self.assertIn('net0',st['prepared']['networks'])
            self.assertEqual(st['mode'],'maintenance')

    def test_pending_export_can_be_explicitly_forgone_in_stopped_vm(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            cfg,cfg_path=self._cfg(root)
            spare.save_state(root/'state.json', {'mode':'maintenance','pending_export':True})
            def fake_run(*args,**kw):
                if args[:2] == ('qm','status'): return b'status: stopped\n'
                if args[:2] == ('qm','config'):
                    return (f"net0: {cfg['networks']['maintenance']['net0']}\nnet1: {cfg['networks']['maintenance']['net1']}\n"
                            'ide2: local:iso/something.iso,media=cdrom\n').encode()
                raise AssertionError(args)
            argv=['opnsense-spare','emergency','--link-down','--config',str(cfg_path)]
            # refuse to skip export -- no mutation
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=fake_run), patch('builtins.input',return_value='n'):
                with self.assertRaises(RuntimeError): spare.main()
            self.assertTrue(spare.load_state(root/'state.json')['pending_export'])
            # accept skipping export, decline import and stop at dry-run? Instead mock
            # xorriso missing after asking to prove pending has been acknowledged.
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=fake_run), \
                 patch.object(spare.shutil,'which',return_value=None), \
                 patch('builtins.input',side_effect=['j','n']):
                with self.assertRaisesRegex(RuntimeError,'xorriso is missing'): spare.main()
            self.assertFalse(spare.load_state(root/'state.json')['pending_export'])


if __name__ == '__main__':
    unittest.main()
