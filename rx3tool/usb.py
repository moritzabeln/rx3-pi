"""USB hot-plug and eject while the player runs (`./rx3 usb-watch`, started by `./rx3 start`).

Up to two rekordbox USBs are shown to the player as USB1 and USB2, like the RX3's two ports.
Each stick is mounted read-only at a private source path (media/.rx3-usb/<slot>). Its slot
shows a writable local copy of the rekordbox database/analysis, kept per stick UUID so
sticks never mix databases, with the stick's other folders bound read-only from the stick.

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

from .launch import SUPPORTED_USB, USB1, USB2, USB_EJECT_FIFO as EJECT_FIFO, Launcher
from .safefs import Tree
from .system import mount_at, mounts, runtime_mounts
from .ui import Failure, info, ok, say, warn

LIBRARIES = 'media/usb2/.rx3-libraries'
SOURCES = 'media/.rx3-usb'
STRAY = 'media/.rx3-stray'
STAMP = '.rx3-usb-uuid'
# slot: (runtime path, path the player knows, its notification FIFO)
SLOTS = {1: (USB1, '/media/usb1/sda1', 'proc/udev_usb1'),
         2: (USB2, '/media/usb2/sdb1', 'proc/udev_usb2')}
UUID_NAME = re.compile(r'^[0-9A-Fa-f][0-9A-Fa-f-]{3,35}$')
DEVICE = re.compile(r'^/dev/[A-Za-z0-9_]+$')


SYSTEM_MOUNTS = ('/', '/boot', '/efi', '/home', '/var', '/usr')


def system_mounted(item):
    points = item.get('mountpoints') or [item.get('mountpoint')]
    return any(p and (p in SYSTEM_MOUNTS or p.startswith('/boot/')) for p in points)


def candidates(listing=None, wanted=''):
    """{uuid: (device, fstype)} for FAT32/exFAT filesystems on removable or USB disks.

    Built-in SD cards (mmc) and filesystems mounted as part of the system are never offered.
    A filesystem whose UUID is `wanted` (the [usb] uuid setting) is accepted even if the
    system does not flag its disk as removable.
    """
    if listing is None:
        result = subprocess.run(['lsblk', '-J', '-o', 'PATH,FSTYPE,UUID,HOTPLUG,RM,TRAN,MOUNTPOINTS'],
                                capture_output=True, text=True)
        if result.returncode:
            return {}
        listing = result.stdout
    found = {}
    def walk(items, parent=(False, '')):
        for item in items or []:
            # lsblk reports flags and transport on the disk; partitions may not carry them.
            flagged = parent[0] or any(item.get(k) in (True, 1, '1') for k in ('hotplug', 'rm'))
            tran = item.get('tran') or parent[1]
            yield item, flagged or tran == 'usb', tran
            yield from walk(item.get('children'), (flagged, tran))
    try:
        devices = list(walk(json.loads(listing).get('blockdevices')))
    except ValueError:
        return {}
    for item, removable, tran in devices:
        uuid, device, fstype = item.get('uuid'), item.get('path'), item.get('fstype')
        chosen = bool(wanted) and bool(uuid) and uuid.lower() == wanted.lower()
        if (removable or chosen) and tran != 'mmc' and not system_mounted(item) \
                and fstype in SUPPORTED_USB and uuid and UUID_NAME.match(uuid) \
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
    def __init__(self, config, launcher=None, scan=None):
        self.config = config
        self.launcher = launcher or Launcher(config)
        self.runtime = config.runtime
        self.scan = scan or (lambda: candidates(wanted=self.config.get('usb', 'uuid')))
        self.slots = {}          # slot -> uuid shown to the player
        self.preferred = {}      # uuid -> slot it last used, so a replugged stick returns there
        self.skip = set()        # ejected or unusable sticks, until they are unplugged
        self.waiting = set()     # sticks reported as having no free slot
        self.fifo = None

    # -- player notifications -------------------------------------------------
    def notify(self, slot, word):
        _, path, fifo = SLOTS[slot]
        try:
            self.launcher.notify(fifo, f'{word} {path}'.encode())
        except (OSError, Failure) as error:
            warn(f'Could not tell the player "{word} {path}": {error}')

    # -- mounts -----------------------------------------------------------------
    def release(self, slot):
        """Unmount the slot's library view and its stick, deepest first; lazily if busy or gone."""
        roots = [str(self.launcher.rt(SLOTS[slot][0])), str(self.launcher.rt(f'{SOURCES}/{slot}'))]
        for target in runtime_mounts(self.runtime):
            if not any(target == r or target.startswith(r + '/') for r in roots):
                continue
            result = self.launcher.sudo(['umount', target], check=False, capture=True)
            if result.returncode:
                self.launcher.sudo(['umount', '-l', target], check=False, capture=True)

    def attach(self, slot, uuid, device, fstype):
        """Mount the stick into `slot` and tell the player; False if it is not usable."""
        source = f'{SOURCES}/{slot}'
        try:
            self.mount_stick(source, device, fstype)
            if not (self.launcher.rt(source) / 'PIONEER/rekordbox/export.pdb').is_file():
                warn(f'USB {uuid} has no rekordbox export (PIONEER/rekordbox/export.pdb); ignored')
                self.release(slot)
                return False
            self.library(slot, uuid)
        except (Failure, OSError, subprocess.SubprocessError) as error:
            warn(f'USB {uuid} could not be prepared: {error}')
            self.release(slot)
            return False
        self.slots[slot] = uuid
        self.preferred[uuid] = slot
        self.notify(slot, 'mount')
        ok(f'USB {uuid} ready as USB{slot}')
        return True

    def mount_stick(self, source, device, fstype):
        launcher = self.launcher
        with as_user(self.config.uid(), self.config.gid()), Tree(self.runtime) as tree:
            tree.mkdir(source, 0o755)
        target = launcher.check_target(source)
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
            launcher.bind(host[0]['target'], source, readonly=True)
        else:
            uid, gid = self.config.uid(), self.config.gid()
            charset = 'utf8=1' if fstype == 'vfat' else 'iocharset=utf8'
            launcher.sudo(['mount', '-t', fstype, '-o',
                           f'ro,uid={uid},gid={gid},{charset},nosuid,nodev,noexec', device, target])

    def library(self, slot, uuid):
        """Bind this stick's local library copy at the slot and add files that are new on the stick."""
        launcher = self.launcher
        rel = SLOTS[slot][0]
        dst = launcher.check_target(rel)
        if mount_at(dst):
            raise Failure(f'{dst} is already mounted; left unchanged')
        src = launcher.rt(f'{SOURCES}/{slot}')
        parts = stick_folders(src)
        store = f'{LIBRARIES}/{uuid.lower()}'
        with as_user(self.config.uid(), self.config.gid()), Tree(self.runtime) as tree:
            migrate(tree, rel, dst)
            tree.mkdir(LIBRARIES, 0o700)
            tree.mkdir(store, 0o755)
            if tree.lstat(f'{store}/{STAMP}') is None:
                tree.write(f'{store}/{STAMP}', (uuid.lower() + '\n').encode(), 0o600)
        launcher.bind(launcher.rt(store), rel)
        with as_user(self.config.uid(), self.config.gid()):
            copied = copy_library(self.runtime, src, rel, parts)
        for part in parts:
            launcher.bind(src / part, f'{rel}/{part}', readonly=True)
        info(f'Local library for {uuid}: {copied} new files copied; existing edits kept')

    # -- events -------------------------------------------------------------------
    def adopt(self):
        """After a helper restart, keep sticks that are still shown and clear anything else."""
        for slot in SLOTS:
            source = self.launcher.rt(f'{SOURCES}/{slot}')
            uuid = ''
            if mount_at(source) and mount_at(self.launcher.rt(SLOTS[slot][0])):
                uuid = subprocess.run(['findmnt', '-n', '-o', 'UUID', '-M', str(source)],
                                      capture_output=True, text=True).stdout.strip()
            if uuid:
                self.slots[slot] = uuid
                self.preferred[uuid] = slot
                info(f'USB {uuid} is already mounted as USB{slot}; watching it')
            else:
                self.release(slot)

    def free_slot(self, uuid):
        free = [slot for slot in SLOTS if slot not in self.slots]
        if not free:
            return None
        return self.preferred[uuid] if self.preferred.get(uuid) in free else free[0]

    def poll(self):
        present = self.scan()
        self.skip &= set(present)
        self.waiting &= set(present)
        for slot, uuid in sorted(self.slots.items()):
            if uuid not in present:
                warn(f'USB {uuid} was unplugged from USB{slot} without EJECT; releasing it')
                self.notify(slot, 'umount')
                self.release(slot)
                del self.slots[slot]
        shown = set(self.slots.values())
        wanted = self.config.get('usb', 'uuid').lower()
        for uuid, (device, fstype) in sorted(present.items()):
            if uuid in shown or uuid in self.skip or (wanted and uuid.lower() != wanted):
                continue
            slot = self.free_slot(uuid)
            if slot is None:
                if uuid not in self.waiting:
                    info(f'USB {uuid} waits: both RX3 slots are in use; eject one to show it')
                    self.waiting.add(uuid)
                continue
            self.waiting.discard(uuid)
            if not self.attach(slot, uuid, device, fstype):
                self.skip.add(uuid)

    def eject(self, requested):
        """The player stopped `requested` slots; release their sticks until they are unplugged."""
        for slot in sorted(requested):
            uuid = self.slots.pop(slot, None)
            if uuid:
                self.release(slot)
                # The player keeps a stopped slot until it sees the removal; a later mount is ignored otherwise.
                self.notify(slot, 'umount')
                self.skip.add(uuid)
                ok(f'USB {uuid} ejected from USB{slot}; it can be unplugged now')

    def requests(self, timeout):
        """Slots named in eject lines from the player."""
        if self.fifo is None:
            return set()
        ready, _, _ = select.select([self.fifo], [], [], timeout)
        if not ready:
            return set()
        try:
            data = os.read(self.fifo, 4096)
        except BlockingIOError:
            return set()
        paths = {path: slot for slot, (_, path, _) in SLOTS.items()}
        found = set()
        for line in data.decode(errors='replace').splitlines():
            word, _, path = line.partition(' ')
            if word == 'eject' and path in paths:
                found.add(paths[path])
        return found

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


def migrate(tree, rel, dst):
    """Move a pre-hotplug library copy (the slot itself, with a UUID stamp) into its per-UUID store."""
    if tree.lstat(f'{rel}/{STAMP}') is None:
        if any(dst.iterdir()):
            # Not a library copy: written into the unmounted slot, e.g. by the player after a release.
            stray = f'{STRAY}/{rel.replace("/", "-")}-{time.strftime("%Y%m%d-%H%M%S")}'
            tree.mkdir(STRAY, 0o700)
            tree.rename(rel, stray)
            tree.mkdir(rel, 0o755)
            warn(f'{dst} held files without a USB UUID record; moved them to {stray}')
        return
    with tree.open_read(f'{rel}/{STAMP}') as handle:
        uuid = handle.read().decode().strip().lower()
    if not UUID_NAME.match(uuid):
        raise Failure(f'{dst} has an invalid USB UUID record; left unchanged')
    tree.mkdir(LIBRARIES, 0o700)
    if tree.lstat(f'{LIBRARIES}/{uuid}') is not None:
        raise Failure(f'Two local library copies exist for USB {uuid}; left unchanged')
    tree.rename(rel, f'{LIBRARIES}/{uuid}')
    tree.mkdir(rel, 0o755)
    info(f'Moved the existing local library of USB {uuid} to {LIBRARIES}/{uuid}')


def stick_folders(src):
    """Folders bound read-only from the stick: all but the rekordbox data (tracks may live anywhere)."""
    folders = sorted(item.name for item in src.iterdir()
                     if item.is_dir() and not item.is_symlink() and item.name != 'PIONEER'
                     and not item.name.startswith('.') and '\n' not in item.name)
    if (src / 'PIONEER/Artwork').is_dir():
        folders.append('PIONEER/Artwork')
    return folders


def copy_library(runtime, src, dst_rel, folders):
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
        for part in folders:
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
        for slot in sorted(watcher.slots):
            watcher.notify(slot, 'umount')
            watcher.release(slot)
