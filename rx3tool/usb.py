"""USB hot-plug and eject while the player runs (`./rx3 usb-watch`, started by `./rx3 start`).

One rekordbox USB at a time is shown to the player twice: read-only as USB1
(media/usb1/sda1) and as USB2 (media/usb2/sdb1), a writable local copy of the
rekordbox database/analysis with the music folders bound read-only from the stick.
Local copies are kept per stick UUID, so several sticks never mix databases.

The player's USB STOP (touch EJECT) ends in umount(); the shim (usb-eject.c) turns
that into a line on dev/rx3-usb-eject. Unplugging without eject is reported to the
player as removal and the mounts are detached lazily.
"""
import json
import os
import re
import select
import stat
import subprocess
import time
from contextlib import contextmanager

from .launch import LIBRARY_PARTS, SUPPORTED_USB, USB1, USB2, USB_EJECT_FIFO as EJECT_FIFO, Launcher
from .safefs import Tree
from .system import mount_at, mounts, runtime_mounts
from .ui import Failure, info, ok, say, warn

LIBRARIES = 'media/usb2/.rx3-libraries'
STAMP = '.rx3-usb-uuid'
SLOTS = {'/media/usb1/sda1': (USB1, 'proc/udev_usb1'), '/media/usb2/sdb1': (USB2, 'proc/udev_usb2')}
UUID_NAME = re.compile(r'^[0-9A-Fa-f][0-9A-Fa-f-]{3,35}$')
DEVICE = re.compile(r'^/dev/[A-Za-z0-9_]+$')


def candidates(listing=None, wanted=''):
    """{uuid: (device, fstype)} for FAT32/exFAT filesystems on hot-plugged disks.

    A filesystem whose UUID is `wanted` (the [usb] uuid setting) is accepted even if the
    system does not flag its disk as hot-pluggable.
    """
    if listing is None:
        result = subprocess.run(['lsblk', '-J', '-o', 'PATH,FSTYPE,UUID,HOTPLUG'],
                                capture_output=True, text=True)
        if result.returncode:
            return {}
        listing = result.stdout
    found = {}
    def walk(items, hotplug=False):
        for item in items or []:
            # lsblk flags the disk; its partitions may not carry the flag themselves.
            flagged = hotplug or item.get('hotplug') in (True, 1, '1')
            yield item, flagged
            yield from walk(item.get('children'), flagged)
    try:
        devices = list(walk(json.loads(listing).get('blockdevices')))
    except ValueError:
        return {}
    for item, hotplug in devices:
        uuid, device, fstype = item.get('uuid'), item.get('path'), item.get('fstype')
        chosen = bool(wanted) and bool(uuid) and uuid.lower() == wanted.lower()
        if (hotplug or chosen) and fstype in SUPPORTED_USB and uuid and UUID_NAME.match(uuid) \
                and device and DEVICE.match(device):
            found[uuid] = (device, fstype)
    return found


@contextmanager
def as_user(uid, gid):
    """Create files as the player's user while the helper runs as root (the player saves edits)."""
    if os.geteuid() != 0:
        yield
        return
    os.setegid(gid)
    os.seteuid(uid)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)


class UsbWatcher:
    def __init__(self, config, launcher=None, scan=None, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.launcher = launcher or Launcher(config)
        self.runtime = config.runtime
        self.scan = scan or (lambda: candidates(wanted=self.config.get('usb', 'uuid')))
        self.clock = clock
        self.sleep = sleep
        self.current = None      # uuid shown to the player
        self.skip = set()        # ejected or unusable sticks, until they are unplugged
        self.fifo = None

    # -- player notifications -------------------------------------------------
    def notify(self, slots, word):
        for path in slots:
            try:
                self.launcher.notify(SLOTS[path][1], f'{word} {path}'.encode())
            except (OSError, Failure) as error:
                warn(f'Could not tell the player "{word} {path}": {error}')

    # -- mounts -----------------------------------------------------------------
    def release(self):
        """Unmount everything in both USB slots, deepest first; lazily if still busy or gone."""
        slots = [str(self.launcher.rt(USB1)), str(self.launcher.rt(USB2))]
        for target in runtime_mounts(self.runtime):
            if not any(target == s or target.startswith(s + '/') for s in slots):
                continue
            result = self.launcher.sudo(['umount', target], check=False, capture=True)
            if result.returncode:
                self.launcher.sudo(['umount', '-l', target], check=False, capture=True)

    def attach(self, uuid, device, fstype):
        """Mount the stick in both slots and tell the player; False if it is not usable."""
        try:
            self.mount_stick(device, fstype)
            if not (self.launcher.rt(USB1) / 'PIONEER/rekordbox/export.pdb').is_file():
                warn(f'USB {uuid} has no rekordbox export (PIONEER/rekordbox/export.pdb); ignored')
                self.release()
                return False
            self.library(uuid)
        except (Failure, OSError, subprocess.SubprocessError) as error:
            warn(f'USB {uuid} could not be prepared: {error}')
            self.release()
            return False
        self.current = uuid
        self.notify(SLOTS, 'mount')
        ok(f'USB {uuid} ready as USB1 (read-only) and USB2 (local library)')
        return True

    def mount_stick(self, device, fstype):
        launcher = self.launcher
        target = launcher.check_target(USB1)
        if mount_at(target):
            raise Failure(f'{target} is already mounted; left unchanged')
        host = [m for m in mounts() if m['root'] == '/' and not
                (m['target'] == str(self.runtime) or m['target'].startswith(str(self.runtime) + '/'))
                and os.path.realpath(m['source']) == os.path.realpath(device)]
        if host:
            options = host[0]['options']
            if fstype == 'vfat' and not ({'utf8', 'utf8=1', 'iocharset=utf8'} & options):
                raise Failure(f'it is mounted at {host[0]["target"]} without UTF-8 file names; '
                              'eject it in the desktop and plug it in again')
            launcher.bind(host[0]['target'], USB1, readonly=True)
        else:
            uid, gid = self.config.uid(), self.config.gid()
            charset = 'utf8=1' if fstype == 'vfat' else 'iocharset=utf8'
            launcher.sudo(['mount', '-t', fstype, '-o',
                           f'ro,uid={uid},gid={gid},{charset},nosuid,nodev,noexec', device, target])

    def library(self, uuid):
        """Bind this stick's local library copy at USB2 and add files that are new on the stick."""
        launcher = self.launcher
        dst = launcher.check_target(USB2)
        if mount_at(dst):
            raise Failure(f'{dst} is already mounted; left unchanged')
        store = f'{LIBRARIES}/{uuid.lower()}'
        with as_user(self.config.uid(), self.config.gid()), Tree(self.runtime) as tree:
            migrate(tree, dst)
            tree.mkdir(LIBRARIES, 0o700)
            tree.mkdir(store, 0o755)
            if tree.lstat(f'{store}/{STAMP}') is None:
                tree.write(f'{store}/{STAMP}', (uuid.lower() + '\n').encode(), 0o600)
        launcher.bind(launcher.rt(store), USB2)
        with as_user(self.config.uid(), self.config.gid()):
            copied = copy_library(self.runtime, launcher.rt(USB1), USB2)
        for part in LIBRARY_PARTS:
            if (launcher.rt(USB1) / part).is_dir():
                launcher.bind(launcher.rt(USB1) / part, f'{USB2}/{part}', readonly=True)
        info(f'Local library for {uuid}: {copied} new files copied; existing edits kept')

    # -- events -------------------------------------------------------------------
    def adopt(self):
        """After a helper restart, take over a stick that is still mounted."""
        current = mount_at(self.launcher.rt(USB1))
        if not current:
            self.release()
            return
        uuid = subprocess.run(['findmnt', '-n', '-o', 'UUID', '-M', str(self.launcher.rt(USB1))],
                              capture_output=True, text=True).stdout.strip()
        self.current = uuid or None
        if self.current:
            info(f'USB {uuid} is already mounted; watching it')

    def poll(self):
        present = self.scan()
        self.skip &= set(present)
        if self.current and self.current not in present:
            warn(f'USB {self.current} was unplugged without EJECT; releasing it')
            self.notify(SLOTS, 'umount')
            self.release()
            self.current = None
        if self.current:
            return
        wanted = self.config.get('usb', 'uuid').lower()
        for uuid, (device, fstype) in sorted(present.items()):
            if uuid in self.skip or (wanted and uuid.lower() != wanted):
                continue
            if not self.attach(uuid, device, fstype):
                self.skip.add(uuid)
            return

    def eject(self, requested):
        """The player stopped `requested` slots; release the stick from both."""
        if not self.current:
            return
        others = [path for path in SLOTS if path not in requested]
        if others:
            self.notify(others, 'umount')
        self.release()
        ok(f'USB {self.current} ejected; it can be unplugged now')
        self.skip.add(self.current)
        self.current = None

    def requests(self, timeout):
        """Slot paths from eject lines; after the first, wait briefly for the other slot."""
        if self.fifo is None:
            return set()
        found, deadline = set(), None
        while True:
            wait = timeout if deadline is None else max(0., deadline - self.clock())
            ready, _, _ = select.select([self.fifo], [], [], wait)
            if ready:
                try:
                    data = os.read(self.fifo, 4096)
                except BlockingIOError:
                    data = b''
                for line in data.decode(errors='replace').splitlines():
                    word, _, path = line.partition(' ')
                    if word == 'eject' and path in SLOTS:
                        found.add(path)
            if not found or len(found) == len(SLOTS) or (deadline and self.clock() >= deadline):
                return found
            if deadline is None:
                deadline = self.clock() + 2

    def open_fifo(self):
        path = self.launcher.check_target(EJECT_FIFO)
        if not stat.S_ISFIFO(os.lstat(path).st_mode):
            raise Failure(f'{EJECT_FIFO} in the runtime is not a FIFO')
        self.fifo = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)

    def run(self, running=lambda: True, interval=1.):
        try:
            self.launcher.ensure_sudo()
        except Failure as error:
            warn(f'USB hot-plug and eject are disabled: {error}',
                 'They mount with sudo; allow sudo without a password prompt for your user.')
            return 1
        self.open_fifo()
        self.adopt()
        wanted = self.config.get('usb', 'uuid')
        say('Watching for rekordbox USBs (FAT32/exFAT with PIONEER/rekordbox/export.pdb)'
            + (f', only {wanted}' if wanted else ''))
        info('USB filesystems visible now: ' + (', '.join(sorted(self.scan())) or 'none'))
        while running():
            requested = self.requests(interval)
            if requested:
                self.eject(requested)
            self.poll()
        return 0


def migrate(tree, dst):
    """Move a pre-hotplug library copy (USB2 itself, with a UUID stamp) into its per-UUID store."""
    if tree.lstat(f'{USB2}/{STAMP}') is None:
        if any(dst.iterdir()):
            raise Failure(f'{dst} holds files without a USB UUID record; left unchanged')
        return
    with tree.open_read(f'{USB2}/{STAMP}') as handle:
        uuid = handle.read().decode().strip().lower()
    if not UUID_NAME.match(uuid):
        raise Failure(f'{dst} has an invalid USB UUID record; left unchanged')
    tree.mkdir(LIBRARIES, 0o700)
    if tree.lstat(f'{LIBRARIES}/{uuid}') is not None:
        raise Failure(f'Two local library copies exist for USB {uuid}; left unchanged')
    tree.rename(USB2, f'{LIBRARIES}/{uuid}')
    tree.mkdir(USB2, 0o755)
    info(f'Moved the existing local library of USB {uuid} to {LIBRARIES}/{uuid}')


def copy_library(runtime, src, dst_rel):
    """Copy rekordbox database/analysis files that do not exist locally yet; never overwrite."""
    copied = 0
    with Tree(runtime) as tree:
        for folder in ('PIONEER/rekordbox', 'PIONEER'):
            if (src / folder).is_dir():
                for item in sorted((src / folder).iterdir()):
                    rel = f'{dst_rel}/{folder}/{item.name}'
                    if item.is_file() and not item.is_symlink() and tree.lstat(rel) is None:
                        with item.open('rb') as handle:
                            tree.write(rel, handle, 0o644)
                        copied += 1
        for part in LIBRARY_PARTS:
            if (src / part).is_dir():
                tree.mkdir(f'{dst_rel}/{part}', 0o755)
        analysis = src / 'PIONEER' / 'USBANLZ'
        tree.mkdir(f'{dst_rel}/PIONEER/USBANLZ', 0o755)
        if analysis.is_dir():
            for folder, dirs, files in os.walk(analysis):
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(folder, d)))
                rel_folder = os.path.relpath(folder, analysis)
                base = f'{dst_rel}/PIONEER/USBANLZ' + ('' if rel_folder == '.' else f'/{rel_folder}')
                tree.mkdir(base, 0o755)
                for name in sorted(files):
                    path = os.path.join(folder, name)
                    # The player saves cue/grid edits into these copies.
                    if not os.path.islink(path) and tree.lstat(f'{base}/{name}') is None:
                        with open(path, 'rb') as handle:
                            tree.write(f'{base}/{name}', handle, 0o644)
                        copied += 1
                        if copied % 500 == 0:
                            info(f'{copied} library files copied ...')
    return copied


def main(config, running=lambda: True):
    watcher = UsbWatcher(config)
    try:
        return watcher.run(running)
    except Failure as error:
        warn(str(error), error.hint)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if watcher.current:
            watcher.notify(SLOTS, 'umount')
            watcher.release()
