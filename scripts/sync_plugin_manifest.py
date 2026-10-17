#!/usr/bin/env python3
"""Generate the legacy Codex projection from the authoritative portable manifest."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'plugin.json').read_text())
compat = {key: manifest[key] for key in ('name', 'version', 'description')}
compat['skills'] = './skills/'
compat.update(manifest['extensions']['com.openai'])
(root / '.codex-plugin/plugin.json').write_text(json.dumps(compat, indent=2) + '\n')
