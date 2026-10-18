"""Behavioral guest-hook regression tests using an isolated filesystem and fake devices.

The FreeBSD absolute paths and system utilities are substituted in-memory; the
deployed hook is never changed. These are boot-flow tests under POSIX /bin/sh,
NOT a substitute for a real OPNsense/FreeBSD integration test.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
HOOK = (HERE / "90-opnsense-spare").read_text()


class GuestBootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "conf/spare"
        self.active = self.root / "conf/config.xml"
        self.mount = self.root / "mnt/iso"
        self.bin = self.root / "bin"
        self.dev = self.root / "dev/cd0"
        self.console = self.root / "console"
        self.trace = self.root / "trace"
        self.blocked = self.root / "blocked"
        self.store.mkdir(parents=True)
        self.active.write_bytes(b"<opnsense><mode>maintenance</mode></opnsense>")
        self.mount.mkdir(parents=True)
        self.bin.mkdir()
        self.dev.parent.mkdir()
        self.dev.touch()
        self.console.touch()
        self.trace.touch()

        self.command("mount", "exit 0")
        self.command("umount", '[ "$FAIL_UMOUNT" != 1 ]')
        self.command("logger", "exit 0")
        self.command("halt", 'echo halt >> "$TRACE"; exit 1')
        self.command("sha256", 'sha256sum "$2" | cut -d " " -f 1')
        self.command("ifconfig", """if [ "$2" = name ]; then
  echo "rename:$1:$3" >> "$TRACE"
  exit 0
fi
case "$1" in vtnet0|vtnet1) exit 0;; esac
exit 1""")
        self.command("mv", """for value in "$@"; do
  case "$value" in
    */last-request)
      if [ "$FAIL_COMMIT" = 1 ]; then exit 51; fi ;;
  esac
done
exec /usr/bin/mv "$@" """)
        self.command("chmod", """for value in "$@"; do
  case "$value" in
    */maintenance.xml.new)
      if [ "$FAIL_SNAPSHOT_CHMOD" = 1 ]; then exit 52; fi ;;
  esac
done
exec /usr/bin/chmod "$@" """)
        self.command("rm", """for value in "$@"; do
  case "$value" in
    */transition)
      if [ "$FAIL_CLEAR_MARKER" = 1 ]; then exit 53; fi ;;
  esac
done
exec /usr/bin/rm "$@" """)
        script = HOOK
        for orig, new in (
            ("STORE=/conf/spare", f"STORE={self.store}"),
            ("ACTIVE=/conf/config.xml", f"ACTIVE={self.active}"),
            ("MOUNT=/mnt/opnsense-spare", f"MOUNT={self.mount}"),
            ("for dev in /dev/cd[0-9]*; do", f"for dev in {self.dev}; do"),
            ('[ -c "$dev" ] || continue', '[ -e "$dev" ] || continue'),
            ("/sbin/ifconfig", str(self.bin / "ifconfig")),
            ("/sbin/halt -p", str(self.bin / "halt") + " -p"),
            (">/dev/console", f">{self.console}"),
            ("while :; do sleep 3600 || :; done",
             f"echo BLOCKED >> {self.blocked}; exit 97"),
        ):
            self.assertIn(orig, script, orig)
            script = script.replace(orig, new)
        self.script = self.root / "hook.sh"
        self.script.write_text(script)
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + ":" + self.env["PATH"],
                        TRACE=str(self.trace), FAIL_COMMIT="0",
                        FAIL_SNAPSHOT_CHMOD="0", FAIL_CLEAR_MARKER="0",
                        FAIL_UMOUNT="0")

    def command(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)

    def run_hook(self, *args, **options):
        env = dict(self.env)
        env.update({k: str(v) for k, v in options.items()})
        result = subprocess.run(["/bin/sh", str(self.script), *args],
                                env=env, capture_output=True, text=True,
                                timeout=5)
        self.assertIn(result.returncode, (0, 97), (result.stdout, result.stderr))
        return result

    def request(self, mode, ident, xml=None):
        mapping = b"vtnet0=igc0\nvtnet1=igc1\n"
        (self.mount / "interfaces.conf").write_bytes(mapping)
        lines = ["magic=OPNSENSE_SPARE_V1", "mode=" + mode, "request_id=" + ident,
                 "ifmap_sha256=" + hashlib.sha256(mapping).hexdigest()]
        if xml is not None:
            (self.mount / "config.xml").write_bytes(xml)
            lines += ["import=1", "sha256=" + hashlib.sha256(xml).hexdigest()]
        else:
            (self.mount / "config.xml").unlink(missing_ok=True)
            lines += ["import=0", "sha256=none"]
        (self.mount / "request.conf").write_text("\n".join(lines) + "\n")

    def initialize(self):
        r = self.run_hook("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.store / "active-mode").read_text(), "maintenance\n")

    def assert_blocked(self, result):
        self.assertEqual(result.returncode, 97, (result.stdout, result.stderr))
        self.assertTrue(self.blocked.exists())
        self.assertIn("halt", self.trace.read_text())

    def test_maintenance_first_and_repeat_boot(self):
        self.initialize()
        ident = "1" * 32
        self.request("maintenance", ident)
        self.assertEqual(self.run_hook().returncode, 0)
        before = self.active.read_bytes()
        self.assertEqual((self.store / "last-request").read_text(), ident + "\n")
        second = self.run_hook()
        self.assertEqual(second.returncode, 0)
        self.assertIn("Repeated boot", second.stdout)
        self.assertEqual(self.active.read_bytes(), before)
        self.assertEqual(self.trace.read_text().count("rename:"), 4)
        self.assertFalse((self.store / "transition").exists())

    def test_emergency_import_repeat_failback_and_reuse(self):
        self.initialize()
        first_xml = b"<opnsense><mode>emergency-one</mode></opnsense>"
        self.request("emergency", "2" * 32, first_xml)
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.active.read_bytes(), first_xml)
        self.assertEqual((self.store / "maintenance.xml").read_bytes(),
                         b"<opnsense><mode>maintenance</mode></opnsense>")
        self.assertFalse((self.store / "transition").exists())
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.active.read_bytes(), first_xml)
        self.request("maintenance", "3" * 32)
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertIn(b"maintenance", self.active.read_bytes())
        self.request("emergency", "4" * 32)
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.active.read_bytes(), first_xml)

    def test_second_import_preserves_first_emergency_archive_across_reboot(self):
        self.initialize()
        one = b"<opnsense><first/></opnsense>"
        two = b"<opnsense><second/></opnsense>"
        self.request("emergency", "a" * 32, one)
        self.assertEqual(self.run_hook().returncode, 0)
        self.request("emergency", "b" * 32, two)
        self.assertEqual(self.run_hook().returncode, 0)
        archive = self.store / "archive" / ("emergency-before-" + "b" * 32 + ".xml")
        self.assertEqual(archive.read_bytes(), one)
        self.assertEqual(self.active.read_bytes(), two)
        self.assertEqual(self.run_hook().returncode, 0)
        self.assertEqual(archive.read_bytes(), one)
        self.assertEqual(self.active.read_bytes(), two)

    def test_unexpected_snapshot_failure_fails_closed(self):
        self.initialize()
        self.request("emergency", "a" * 32, b"<opnsense><emergency/></opnsense>")
        result = self.run_hook(FAIL_SNAPSHOT_CHMOD="1")
        self.assert_blocked(result)
        self.assertTrue((self.store / "transition").exists())
        self.assertIn(b"maintenance", self.active.read_bytes())

    def test_request_commit_failure_preserves_transition_marker(self):
        self.initialize()
        self.request("emergency", "a" * 32, b"<opnsense><emergency/></opnsense>")
        result = self.run_hook(FAIL_COMMIT="1")
        self.assert_blocked(result)
        self.assertTrue((self.store / "transition").exists())
        self.assertEqual((self.store / "active-mode").read_text(), "emergency\n")
        self.assert_blocked(self.run_hook())

    def test_marker_cleanup_failure_fails_closed(self):
        self.initialize()
        self.request("emergency", "a" * 32, b"<opnsense><emergency/></opnsense>")
        result = self.run_hook(FAIL_CLEAR_MARKER="1")
        self.assert_blocked(result)
        self.assertTrue((self.store / "transition").exists())

    def test_missing_media_or_unmount_failure_never_continues_boot(self):
        self.initialize()
        self.assert_blocked(self.run_hook())
        self.request("maintenance", "a" * 32)
        self.assert_blocked(self.run_hook(FAIL_UMOUNT="1"))


if __name__ == "__main__":
    unittest.main()
