"""USB hot-plug, eject and per-stick library copies. No real devices, mounts or sudo."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rx3tool import config
from rx3tool.launch import Launcher, USB1, USB2
from rx3tool.safefs import Tree
from rx3tool.ui import Failure
from rx3tool.usb import LIBRARIES, SLOTS, UsbWatcher, candidates, copy_library, migrate

A, B = 'CA98-D2DE', '1234-ABCD'


class FakeLauncher:
    def __init__(self, runtime):
        self.runtime = runtime
        self.sent = []

    def rt(self, relative):
        return self.runtime / relative

    def notify(self, relative, message):
        self.sent.append((relative, message.decode()))


class UsbTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='rx3 usb ')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        conf = self.root / 'rx3.conf'
        conf.write_text('[paths]\nruntime = runtime\nstate = state\nwork = work\n')
        self.conf = conf
        self.cfg = config.load(conf, env={})
        self.runtime = self.cfg.runtime
        (self.runtime / USB1).mkdir(parents=True)
        (self.runtime / USB2).mkdir(parents=True)

    def watcher(self, present, cfg=None):
        launcher = FakeLauncher(self.runtime)
        w = UsbWatcher(cfg or self.cfg, launcher=launcher, scan=lambda: dict(present))
        w.release = lambda: launcher.sent.append(('release', ''))
        return w, launcher

    def test_candidates_are_hotplugged_fat_with_safe_names(self):
        listing = json.dumps({'blockdevices': [
            {'path': '/dev/mmcblk0', 'fstype': None, 'uuid': None, 'hotplug': False, 'children': [
                {'path': '/dev/mmcblk0p1', 'fstype': 'vfat', 'uuid': 'AAAA-BBBB', 'hotplug': False}]},
            {'path': '/dev/sda', 'fstype': None, 'uuid': None, 'hotplug': True, 'children': [
                {'path': '/dev/sda1', 'fstype': 'vfat', 'uuid': A, 'hotplug': True},
                {'path': '/dev/sda2', 'fstype': 'ext4', 'uuid': 'deadbeef-0000', 'hotplug': True},
                {'path': '/dev/sda3', 'fstype': 'exfat', 'uuid': '../../x', 'hotplug': True}]},
            {'path': '/dev/sdb1', 'fstype': 'exfat', 'uuid': B, 'hotplug': '1'}]})
        self.assertEqual(candidates(listing), {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'exfat')})
        self.assertEqual(candidates('not json'), {})

    def test_plug_unplug_and_replug(self):
        present = {A: ('/dev/sda1', 'vfat')}
        w, launcher = self.watcher(present)
        attached = []
        with patch.object(UsbWatcher, 'attach', lambda self, u, d, f: attached.append(u) or setattr(self, 'current', u) or True):
            w.poll()
            self.assertEqual((attached, w.current), ([A], A))
            w.scan = lambda: {}
            w.poll()
            self.assertIsNone(w.current)
            self.assertEqual(launcher.sent, [('proc/udev_usb1', 'umount /media/usb1/sda1'),
                                             ('proc/udev_usb2', 'umount /media/usb2/sdb1'), ('release', '')])
            w.scan = lambda: dict(present)
            w.poll()
            self.assertEqual(attached, [A, A])

    def test_eject_releases_both_slots_and_waits_for_unplug(self):
        present = {A: ('/dev/sda1', 'vfat')}
        w, launcher = self.watcher(present)
        w.current = A
        w.eject({'/media/usb1/sda1'})
        # The player stopped USB1 itself; USB2 is reported removed so both slots stay consistent.
        self.assertEqual(launcher.sent, [('proc/udev_usb2', 'umount /media/usb2/sdb1'), ('release', '')])
        self.assertIsNone(w.current)
        with patch.object(UsbWatcher, 'attach') as attach:
            w.poll()
            attach.assert_not_called()           # still plugged in after EJECT
            w.scan = lambda: {}
            w.poll()
            w.scan = lambda: dict(present)
            w.poll()
            attach.assert_called_once_with(A, '/dev/sda1', 'vfat')

    def test_unusable_stick_is_skipped_until_replugged_and_uuid_setting_filters(self):
        present = {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'vfat')}
        w, _ = self.watcher(present)
        with patch.object(UsbWatcher, 'attach', return_value=False) as attach:
            w.poll()
            w.poll()
            self.assertEqual([c.args[0] for c in attach.call_args_list], [B, A])
        restricted = config.load(self.conf, [f'usb.uuid={A.lower()}'], env={})
        w, _ = self.watcher(present, restricted)
        with patch.object(UsbWatcher, 'attach', return_value=True) as attach:
            w.poll()
            attach.assert_called_once_with(A, '/dev/sda1', 'vfat')

    def test_stick_without_rekordbox_export_is_released(self):
        w, launcher = self.watcher({})
        with patch.object(UsbWatcher, 'mount_stick'), patch.object(UsbWatcher, 'library') as library:
            self.assertFalse(w.attach(A, '/dev/sda1', 'vfat'))
            library.assert_not_called()
        self.assertEqual(launcher.sent, [('release', '')])
        (self.runtime / USB1 / 'PIONEER/rekordbox').mkdir(parents=True)
        (self.runtime / USB1 / 'PIONEER/rekordbox/export.pdb').write_bytes(b'db')
        with patch.object(UsbWatcher, 'mount_stick'), patch.object(UsbWatcher, 'library'):
            self.assertTrue(w.attach(A, '/dev/sda1', 'vfat'))
        self.assertEqual([m for _, m in launcher.sent[1:]], ['mount /media/usb1/sda1', 'mount /media/usb2/sdb1'])

    def test_eject_requests_collect_both_slots(self):
        w, _ = self.watcher({})
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.set_blocking(read, False)
        w.fifo = read
        self.assertEqual(w.requests(0), set())
        os.write(write, b'eject /media/usb1/sda1\neject /media/usb2/sdb1\nbogus /etc\n')
        self.assertEqual(w.requests(0), set(SLOTS))
        clock = iter([0., 2., 3.])
        w.clock = lambda: next(clock)
        os.write(write, b'eject /media/usb2/sdb1\n')
        self.assertEqual(w.requests(0), {'/media/usb2/sdb1'})

    def test_existing_library_copy_moves_to_its_stick(self):
        dst = self.runtime / USB2
        (dst / 'PIONEER/rekordbox').mkdir(parents=True)
        (dst / 'PIONEER/rekordbox/export.pdb').write_bytes(b'edited')
        (dst / '.rx3-usb-uuid').write_text(A + '\n')
        with Tree(self.runtime) as tree:
            migrate(tree, dst)
        self.assertEqual(list(dst.iterdir()), [])
        self.assertEqual((self.runtime / LIBRARIES / A / 'PIONEER/rekordbox/export.pdb').read_bytes(), b'edited')
        with Tree(self.runtime) as tree:
            migrate(tree, dst)                   # nothing left to move
        (dst / 'stray').write_text('x')
        with Tree(self.runtime) as tree, self.assertRaisesRegex(Failure, 'without a USB UUID'):
            migrate(tree, dst)

    def test_library_copy_never_overwrites_edits(self):
        src = self.runtime / USB1
        for rel, data in (('PIONEER/rekordbox/export.pdb', b'usb'), ('PIONEER/DEVSETTING.DAT', b'dev'),
                          ('PIONEER/USBANLZ/P01/0001/ANLZ0000.DAT', b'usb-anlz')):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        (src / 'Contents').mkdir()
        local = self.runtime / USB2 / 'PIONEER/USBANLZ/P01/0001'
        local.mkdir(parents=True)
        (local / 'ANLZ0000.DAT').write_bytes(b'edited cues')
        self.assertEqual(copy_library(self.runtime, src, USB2), 2)
        self.assertEqual((local / 'ANLZ0000.DAT').read_bytes(), b'edited cues')
        self.assertEqual((self.runtime / USB2 / 'PIONEER/rekordbox/export.pdb').read_bytes(), b'usb')
        self.assertTrue((self.runtime / USB2 / 'Contents').is_dir())

    def test_helper_command_keeps_overrides_and_is_recognised(self):
        c = config.load(self.conf, [f'usb.uuid={A}'], env={})
        launcher = Launcher(c)
        argv = [str(a) for a in launcher.usb_command()]
        self.assertIn(f'usb.uuid={A}', argv)
        self.assertEqual(argv[-3:], ['usb-watch', '--runtime', str(c.runtime)])
        with patch('rx3tool.launch.processes', return_value=[(77, os.getuid(), argv, 'python3')]), \
                patch('rx3tool.launch.player_pids', return_value=[]):
            self.assertEqual(launcher.helpers()['usb'], [77])


if __name__ == '__main__':
    unittest.main()
