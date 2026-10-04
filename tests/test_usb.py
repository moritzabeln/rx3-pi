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
from rx3tool.usb import LIBRARIES, SOURCES, SLOTS, STRAY, UsbWatcher, as_user, candidates, copy_library, migrate, stick_folders

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
        w.release = lambda slot: launcher.sent.append(('release', slot))
        return w, launcher

    @staticmethod
    def fake_attach(attached):
        def attach(self, slot, uuid, device, fstype):
            attached.append((slot, uuid))
            self.slots[slot] = uuid
            self.preferred[uuid] = slot
            return True
        return attach

    def test_candidates_are_hotplugged_fat_with_safe_names(self):
        listing = json.dumps({'blockdevices': [
            {'path': '/dev/mmcblk0', 'fstype': None, 'uuid': None, 'hotplug': False, 'children': [
                {'path': '/dev/mmcblk0p1', 'fstype': 'vfat', 'uuid': 'AAAA-BBBB', 'hotplug': False}]},
            {'path': '/dev/sda', 'fstype': None, 'uuid': None, 'hotplug': True, 'children': [
                {'path': '/dev/sda1', 'fstype': 'vfat', 'uuid': A},      # flag only on the disk
                {'path': '/dev/sda2', 'fstype': 'ext4', 'uuid': 'deadbeef-0000', 'hotplug': True},
                {'path': '/dev/sda3', 'fstype': 'exfat', 'uuid': '../../x', 'hotplug': True}]},
            {'path': '/dev/sdb1', 'fstype': 'exfat', 'uuid': B, 'hotplug': '1'}]})
        self.assertEqual(candidates(listing), {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'exfat')})
        self.assertEqual(candidates('not json'), {})
        # A stick named in [usb] uuid is used even when its disk is not flagged as hot-pluggable.
        listing = json.dumps({'blockdevices': [{'path': '/dev/sdc1', 'fstype': 'vfat', 'uuid': A, 'hotplug': False}]})
        self.assertEqual(candidates(listing), {})
        self.assertEqual(candidates(listing, A.lower()), {A: ('/dev/sdc1', 'vfat')})
        self.assertEqual(candidates(listing, B), {})

    def test_usb_stick_is_found_by_transport_and_sd_card_is_not(self):
        # Raspberry Pi: the stick has HOTPLUG=0 but TRAN=usb; the SD card reports HOTPLUG=1.
        listing = json.dumps({'blockdevices': [
            {'path': '/dev/sda', 'fstype': None, 'uuid': None, 'hotplug': False, 'rm': True, 'tran': 'usb',
             'mountpoints': [None], 'children': [
                {'path': '/dev/sda1', 'fstype': 'vfat', 'uuid': A, 'hotplug': False, 'rm': True,
                 'tran': None, 'mountpoints': [None]}]},
            {'path': '/dev/mmcblk0', 'fstype': None, 'uuid': None, 'hotplug': True, 'rm': False,
             'tran': 'mmc', 'mountpoints': [None], 'children': [
                {'path': '/dev/mmcblk0p1', 'fstype': 'vfat', 'uuid': 'E4BA-E95D', 'hotplug': True,
                 'tran': None, 'mountpoints': ['/boot/firmware']}]}]})
        self.assertEqual(candidates(listing), {A: ('/dev/sda1', 'vfat')})
        self.assertEqual(candidates(listing, 'E4BA-E95D'), {A: ('/dev/sda1', 'vfat')})

    def test_plug_unplug_and_replug(self):
        present = {A: ('/dev/sda1', 'vfat')}
        w, launcher = self.watcher(present)
        attached = []
        with patch.object(UsbWatcher, 'attach', self.fake_attach(attached)):
            w.poll()
            self.assertEqual((attached, w.slots), ([(1, A)], {1: A}))
            w.scan = lambda: {}
            w.poll()
            self.assertEqual(w.slots, {})
            self.assertEqual(launcher.sent, [('proc/udev_usb1', 'umount /media/usb1/sda1'), ('release', 1)])
            w.scan = lambda: dict(present)
            w.poll()
            self.assertEqual(attached, [(1, A), (1, A)])

    def test_two_sticks_get_their_own_slots_and_a_third_waits(self):
        C = 'ABCD-0001'
        present = {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'vfat')}
        w, launcher = self.watcher(present)
        attached = []
        with patch.object(UsbWatcher, 'attach', self.fake_attach(attached)):
            w.poll()
            self.assertEqual(w.slots, {1: B, 2: A})
            # Unplugging one stick leaves the other in its slot.
            w.scan = lambda: {A: present[A]}
            w.poll()
            self.assertEqual(w.slots, {2: A})
            self.assertEqual(launcher.sent, [('proc/udev_usb1', 'umount /media/usb1/sda1'), ('release', 1)])
            w.scan = lambda: {A: present[A], C: ('/dev/sdc1', 'vfat')}
            w.poll()
            self.assertEqual(w.slots, {1: C, 2: A})
            # B finds both slots in use; it is shown once a slot frees, back in its old slot if free.
            w.scan = lambda: {A: present[A], B: present[B], C: ('/dev/sdc1', 'vfat')}
            w.poll()
            self.assertEqual((w.slots, w.waiting), ({1: C, 2: A}, {B}))
            w.scan = lambda: {A: present[A], B: present[B]}
            w.poll()
            self.assertEqual(w.slots, {1: B, 2: A})

    def test_replugged_stick_returns_to_its_slot(self):
        present = {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'vfat')}
        w, _ = self.watcher(present)
        with patch.object(UsbWatcher, 'attach', self.fake_attach([])):
            w.poll()
            w.scan = lambda: {A: present[A]}
            w.poll()
            w.scan = lambda: {}
            w.poll()
            w.scan = lambda: {A: present[A]}
            w.poll()
            self.assertEqual(w.slots, {2: A})

    def test_eject_releases_only_that_slot_and_waits_for_unplug(self):
        present = {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'vfat')}
        w, launcher = self.watcher(present)
        w.slots = {1: A, 2: B}
        w.eject({1})
        # The slot is reported removed after release, as when a stopped stick is pulled out.
        self.assertEqual(launcher.sent, [('release', 1), ('proc/udev_usb1', 'umount /media/usb1/sda1')])
        self.assertEqual(w.slots, {2: B})
        with patch.object(UsbWatcher, 'attach') as attach:
            w.poll()
            attach.assert_not_called()           # still plugged in after EJECT
            w.scan = lambda: {B: present[B]}
            w.poll()
            w.scan = lambda: dict(present)
            w.poll()
            attach.assert_called_once_with(1, A, '/dev/sda1', 'vfat')

    def test_unusable_stick_is_skipped_until_replugged_and_uuid_setting_filters(self):
        present = {A: ('/dev/sda1', 'vfat'), B: ('/dev/sdb1', 'vfat')}
        w, _ = self.watcher(present)
        with patch.object(UsbWatcher, 'attach', return_value=False) as attach:
            w.poll()
            w.poll()
            self.assertEqual([c.args[1] for c in attach.call_args_list], [B, A])
        restricted = config.load(self.conf, [f'usb.uuid={A.lower()}'], env={})
        w, _ = self.watcher(present, restricted)
        with patch.object(UsbWatcher, 'attach', return_value=True) as attach:
            w.poll()
            attach.assert_called_once_with(1, A, '/dev/sda1', 'vfat')

    def test_stick_without_rekordbox_export_is_released(self):
        w, launcher = self.watcher({})
        source = self.runtime / SOURCES / '2'
        with patch.object(UsbWatcher, 'mount_stick'), patch.object(UsbWatcher, 'library') as library:
            self.assertFalse(w.attach(2, A, '/dev/sda1', 'vfat'))
            library.assert_not_called()
        self.assertEqual(launcher.sent, [('release', 2)])
        (source / 'PIONEER/rekordbox').mkdir(parents=True)
        (source / 'PIONEER/rekordbox/export.pdb').write_bytes(b'db')
        with patch.object(UsbWatcher, 'mount_stick'), patch.object(UsbWatcher, 'library'):
            self.assertTrue(w.attach(2, A, '/dev/sda1', 'vfat'))
        self.assertEqual([m for _, m in launcher.sent[1:]], ['mount /media/usb2/sdb1'])
        self.assertEqual(w.slots, {2: A})

    def test_eject_requests_name_slots(self):
        w, _ = self.watcher({})
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.set_blocking(read, False)
        w.fifo = read
        self.assertEqual(w.requests(0), set())
        os.write(write, b'eject /media/usb1/sda1\neject /media/usb2/sdb1\nbogus /etc\n')
        self.assertEqual(w.requests(0), set(SLOTS))
        os.write(write, b'eject /media/usb2/sdb1\n')
        self.assertEqual(w.requests(0), {2})

    def test_existing_library_copy_moves_to_its_stick(self):
        dst = self.runtime / USB2
        (dst / 'PIONEER/rekordbox').mkdir(parents=True)
        (dst / 'PIONEER/rekordbox/export.pdb').write_bytes(b'edited')
        (dst / '.rx3-usb-uuid').write_text(A + '\n')
        with Tree(self.runtime) as tree:
            migrate(tree, USB2, dst)
        self.assertEqual(list(dst.iterdir()), [])
        self.assertEqual((self.runtime / LIBRARIES / A.lower() / 'PIONEER/rekordbox/export.pdb').read_bytes(), b'edited')
        with Tree(self.runtime) as tree:
            migrate(tree, USB2, dst)             # nothing left to move
        (dst / 'stray').write_text('x')
        with Tree(self.runtime) as tree:
            migrate(tree, USB2, dst)
        self.assertEqual(list(dst.iterdir()), [])
        moved = list((self.runtime / STRAY).iterdir())
        self.assertEqual([(p / 'stray').read_text() for p in moved], ['x'])

    def test_library_copy_never_overwrites_edits(self):
        src = self.runtime / USB1
        for rel, data in (('PIONEER/rekordbox/export.pdb', b'usb'), ('PIONEER/DEVSETTING.DAT', b'dev'),
                          ('PIONEER/USBANLZ/P01/0001/ANLZ0000.DAT', b'usb-anlz')):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        for folder in ('Contents', 'My Tracks', '.Trashes', 'PIONEER/Artwork'):
            (src / folder).mkdir()
        folders = stick_folders(src)
        self.assertEqual(folders, ['Contents', 'My Tracks', 'PIONEER/Artwork'])
        local = self.runtime / USB2 / 'PIONEER/USBANLZ/P01/0001'
        local.mkdir(parents=True)
        (local / 'ANLZ0000.DAT').write_bytes(b'edited cues')
        self.assertEqual(copy_library(self.runtime, src, USB2, folders), 2)
        self.assertEqual((local / 'ANLZ0000.DAT').read_bytes(), b'edited cues')
        self.assertEqual((self.runtime / USB2 / 'PIONEER/rekordbox/export.pdb').read_bytes(), b'usb')
        for folder in folders:
            self.assertTrue((self.runtime / USB2 / folder).is_dir())

    def test_helper_command_keeps_overrides_and_is_recognised(self):
        c = config.load(self.conf, [f'usb.uuid={A}'], env={})
        launcher = Launcher(c)
        inner = [str(a) for a in launcher.usb_command()]
        self.assertIn(f'usb.uuid={A}', inner)
        self.assertIn('-B', inner[:3])
        self.assertEqual(inner[-3:], ['usb-watch', '--runtime', str(c.runtime)])
        # ./rx3 start runs it through sudo, like the player; the root child is not ours to match.
        argv = ['sudo', '-n', '--'] + inner
        with patch('rx3tool.launch.processes', return_value=[(77, os.getuid(), argv, 'sudo'),
                                                              (78, 0, inner, 'python3')]), \
                patch('rx3tool.launch.player_pids', return_value=[]):
            self.assertEqual(launcher.helpers()['usb'], [77])
        with patch.object(launcher, 'ensure_sudo'), patch.object(launcher, 'spawn') as spawn:
            launcher.start_usb()
        self.assertEqual(spawn.call_args[0][0], 'usb')
        self.assertEqual([str(a) for a in spawn.call_args[0][1]], argv)

    def test_as_user_is_a_no_op_for_an_unprivileged_helper(self):
        with patch('rx3tool.usb.os.geteuid', return_value=1000), patch('rx3tool.usb.os.seteuid') as seteuid:
            with as_user(1000, 1000):
                pass
        seteuid.assert_not_called()
        calls = []
        with patch('rx3tool.usb.os.geteuid', return_value=0), \
                patch('rx3tool.usb.os.setegid', side_effect=lambda g: calls.append(('g', g))), \
                patch('rx3tool.usb.os.seteuid', side_effect=lambda u: calls.append(('u', u))):
            with as_user(1000, 44):
                calls.append('inside')
        self.assertEqual(calls, [('g', 44), ('u', 1000), 'inside', ('u', 0), ('g', 0)])


if __name__ == '__main__':
    unittest.main()
