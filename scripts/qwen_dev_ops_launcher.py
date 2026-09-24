#!/usr/bin/env python3
"""School wrapper: load protected operator configuration, then run the versioned tool."""
import json
import os
import sys
from pathlib import Path

ROOT = Path('/mnt/local/babylm26_g2/projects/linguistic-oj')
config_path = ROOT / 'operations/config/qwen-development.json'
if config_path.stat().st_mode & 0o077:
    raise SystemExit('Operator configuration must be owner-only.')
config = json.loads(config_path.read_text(encoding='utf-8'))
env = dict(os.environ)
env['PYTHONPATH'] = os.pathsep.join(config['python_paths'])
command = [config['python'], str(ROOT / 'operations/bin/qwen-dev-ops.py'), *sys.argv[1:],
           '--project-root', str(ROOT), '--instance-dir', config['instance_dir']]
os.execve(config['python'], command, env)
