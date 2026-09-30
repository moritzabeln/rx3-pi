"""Fetch a pinned public controller mapping, or validate a user-supplied XML."""
import hashlib
import importlib.util
import urllib.request
from . import REPO
from .safefs import Tree
from .ui import Failure, ok, say

# [controller] model -> (pinned URL, SHA-256)
MAPPINGS = {
    'DDJ-FLX6': ('https://raw.githubusercontent.com/xsploit/bitedj/'
                 'ee55091a697c1c3f18a07920098eb613f2ccd1d7/res/controllers/Pioneer-DDJ-FLX6.midi.xml',
                 'e58ec496995c3203f7d5d35de7cdcd24dc553e880912e669faf8b9c1e456444b'),
    'DDJ-400': ('https://raw.githubusercontent.com/mixxxdj/mixxx/'
                'a92b7763cd087d60dbfe3a616bb57f22e816f980/res/controllers/Pioneer-DDJ-400.midi.xml',
                '97eae1632fb273c0d818901cb6ec10b094268d595eb31ebc8c31050bc3513ac7'),
}


def ensure(config, offline=False, dry_run=False):
    model = config.get('controller', 'model')
    url, sha256 = MAPPINGS[model]
    target = config.path('controller', 'mapping')
    if not target.exists():
        if offline:
            raise Failure(f'Controller mapping missing: {target}',
                          'Run ./rx3 mapping with network access, or set [controller] mapping to your XML.')
        if dry_run:
            say(f'  [dry-run] would fetch {url} to {target}')
            return
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(1024 * 1024)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise Failure(f'Downloaded {model} mapping checksum does not match; nothing installed')
        with Tree(target.parent, create=True) as tree:
            tree.write(target.name, data, 0o644, replace=False)
    spec = importlib.util.spec_from_file_location('rx3_mapping_check', REPO / 'flx6-rx3.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        bridge = module.Bridge(str(target), lambda *a: None, model=model)
    except Exception as error:
        raise Failure(f'Cannot load controller mapping {target}: {error}')
    if not bridge.mapping:
        raise Failure(f'No supported {model} bindings found in {target}',
                      f'Check that [controller] model = {model} matches the mapping file.')
    ok(f'Controller mapping ({model}): {len(bridge.mapping)} bindings from {target}')
