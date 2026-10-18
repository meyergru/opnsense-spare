"""V6-specific regression tests (generic NICs and early guest mapping)."""
import importlib.machinery
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
loader=importlib.machinery.SourceFileLoader('spare_v6', str(HERE/'opnsense-spare'))
spec=importlib.util.spec_from_loader(loader.name,loader)
spare=importlib.util.module_from_spec(spec)
loader.exec_module(spare)

class UniversalTopologyTest(unittest.TestCase):
    def make_cfg(self, dirname):
        cfg=json.loads((HERE/'opnsense-spare-three-nic.json.example').read_text())
        cfg['maintenance_ssh_host']='192.168.10.15'
        cfg['iso_dir']=str(dirname)
        return cfg

    def test_three_nic_layout_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=self.make_cfg(td)
            spare.validate_config(cfg)
            self.assertEqual(spare.nic_keys(cfg),['net0','net1','net2'])
            self.assertEqual(spare.ifmap_bytes(cfg), b'vtnet0=igc0\nvtnet1=igc1\nvtnet2=igc3\n')

    def test_three_nic_iso_is_self_contained_and_hashed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=self.make_cfg(td)
            self.assertEqual(len(spare.ifmap_bytes(cfg)), 36)
            calls=[]
            def fake_run(*args, **kw):
                stage=Path(args[-1])
                data=(stage/'interfaces.conf').read_bytes()
                request=(stage/'request.conf').read_text()
                self.assertIn(f'ifmap_sha256={hashlib.sha256(data).hexdigest()}',request)
                self.assertIn('import=0',request)
                calls.append(data)
                Path(args[args.index('-o')+1]).write_bytes(b'testiso')
            with patch.object(spare,'run',side_effect=fake_run):
                spare.make_iso(cfg,'maintenance',None)
                spare.make_iso(cfg,'emergency',None)
            self.assertEqual(calls,[spare.ifmap_bytes(cfg)]*2)

    def test_config_rejects_missing_map_collision_or_client_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            base=self.make_cfg(td)
            mutations=[
                lambda c:c['interface_names'].pop('vtnet2'),
                lambda c:c['interface_names'].update({'vtnet2':'igc0'}),
                lambda c:c['interface_names'].update({'vtnet0':'lo0'}),
                lambda c:c['networks']['emergency'].pop('net2'),
            ]
            import copy
            for mutation in mutations[:4]:
                c=copy.deepcopy(base);mutation(c)
                with self.assertRaises(RuntimeError):spare.validate_config(c)
            c=copy.deepcopy(base)
            c['networks']['maintenance']['net1']=c['networks']['maintenance']['net1'].replace(',link_down=1','')
            with self.assertRaisesRegex(RuntimeError,'link_down'):spare.validate_config(c)
            c=copy.deepcopy(base)
            c['networks']['maintenance']['net0']=c['networks']['maintenance']['net0'].replace('02:00:00:00:10:01','02:00:00:00:20:01')
            with self.assertRaisesRegex(RuntimeError,'MAC'):spare.validate_config(c)

    def test_qemu_updates_every_network_slot(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=self.make_cfg(td)
            commands=[]
            with patch.object(spare,'run',side_effect=lambda *args,**kw: commands.append(args)):
                spare.qemu_net(cfg, cfg['networks']['emergency'])
            self.assertEqual([x[3] for x in commands],['--net0','--net1','--net2','--onboot'])

    def test_prepared_state_detects_third_nic_change(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=self.make_cfg(td)
            iso=spare.iso_name(cfg,'emergency')
            (Path(td)/iso).write_bytes(b'valid-iso')
            nets={k:spare.set_link_down(v,True) for k,v in cfg['networks']['emergency'].items()}
            prepared={'networks':nets,'iso':iso,'sha256':spare.iso_digest(Path(td)/iso)}
            actual=''.join(f'{k}: {v}\n' for k,v in nets.items()) + f'ide2: local:iso/{iso},media=cdrom\n'
            spare.prepared_matches(cfg,actual,prepared)
            with self.assertRaisesRegex(RuntimeError,'net2'):
                spare.prepared_matches(cfg,actual.replace('vmbrWAN','vmbrOTHER'),prepared)

    def test_three_nic_emergency_controller_prepares_without_start(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            cfg=self.make_cfg(root)
            cfg["state_file"]=str(root/'state.json')
            cfgpath=root/'config.json'
            cfgpath.write_text(json.dumps(cfg))
            spare.save_state(root/'state.json',{'mode':'maintenance','pending_export':False})
            current={k:cfg['networks']['maintenance'][k] for k in spare.nic_keys(cfg)}
            iso={'name':'local:iso/preexisting.iso'}
            commands=[]
            def fake_run(*argv,**kwargs):
                if argv[:2]==('qm','status'):
                    return b'status: stopped\n'
                if argv[:2]==('qm','config'):
                    return (''.join(f'{k}: {v}\n' for k,v in current.items())+
                            f'ide2: {iso["name"]},media=cdrom\n').encode()
                if argv[:2]==('qm','set'):
                    commands.append(argv[3])
                    if argv[3] in ('--net0','--net1','--net2'):
                        current[argv[3][2:]]=argv[4]
                    if argv[3]=='--ide2':iso['name']=argv[4].split(',')[0]
                    return b''
                raise AssertionError(argv)
            def fake_make(cfg,mode,src):
                name=spare.iso_name(cfg,mode)
                path=root/name
                path.write_bytes(b'simulated-v6-iso')
                return ('f'*32,name,path)
            argv=['opnsense-spare','emergency','--link-down','--config',str(cfgpath)]
            with patch.object(spare.sys,'argv',argv), patch.object(spare.os,'geteuid',return_value=0), \
                 patch.object(spare,'run',side_effect=fake_run), \
                 patch.object(spare,'make_iso',side_effect=fake_make), \
                 patch.object(spare.shutil,'which',return_value='/usr/bin/xorriso'), \
                 patch('builtins.input',side_effect=['n','n']):
                spare.main()
            self.assertEqual(commands,['--net0','--net1','--net2','--onboot','--ide2'])
            self.assertEqual(spare.load_state(root/'state.json')['mode'],'maintenance')
            self.assertEqual(len(spare.load_state(root/'state.json')['prepared']['networks']),3)
            for net in current.values():self.assertIn('link_down=1',net)

    def test_v5_converter_preserves_important_fields(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('migrate_v5',HERE/'migrate-v5-config.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        old=json.loads((HERE/'../opnsense-spare-v5-stub.json').read_text()) if (HERE/'../opnsense-spare-v5-stub.json').exists() else {
            'vmid':999,'ssh_identity':None,'net0_maintenance':'A','net1_maintenance':'B',
            'net0_emergency':'C','net1_emergency':'D', 'transfer_default':'/tmp/test'}
        cfg=mod.convert(old,{'vtnet0':'ix1','vtnet1':'igc3'})
        self.assertEqual(cfg['networks']['emergency']['net1'],'D')
        self.assertEqual(cfg['transfer_default'],'/tmp/test')
        self.assertEqual(cfg['interface_names']['vtnet0'],'ix1')

    def test_guest_hook_shell_syntax_and_boot_order_guards(self):
        path=HERE/'90-opnsense-spare'
        subprocess.run(['sh','-n',str(path)],check=True)
        s=path.read_text()
        self.assertIn('ifmap_sha256',s)
        self.assertIn('SHA256 mismatch',s)
        self.assertIn('/sbin/ifconfig "$SRC" name "$DST"',s)
        self.assertLess(s.index('network interfaces renamed'), s.index('The request ID makes booting repeatedly'))

if __name__=='__main__':
    unittest.main()
