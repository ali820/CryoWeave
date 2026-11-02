#!/usr/bin/env python3
"""Run every plugin worker self-test with its local or CI Python."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def python_for(plugin: Path) -> str:
    if override := os.environ.get('CRYO_PYTHON'):
        return override
    candidates = (
        plugin / '.venv' / 'Scripts' / 'python.exe',
        plugin / '.venv' / 'bin' / 'python',
    )
    return next((str(path) for path in candidates if path.is_file()), sys.executable)


def main() -> None:
    plugins = sorted(path for path in (ROOT / 'plugins').iterdir() if path.is_dir())
    if not plugins:
        raise RuntimeError('No plugins found')
    for plugin in plugins:
        workers = sorted(plugin.glob('*_worker.py'))
        if len(workers) != 1:
            raise RuntimeError(f'{plugin.name}: expected exactly one worker, found {len(workers)}')
        python = python_for(plugin)
        print(f'=== worker self-test: {plugin.name} ({python}) ===', flush=True)
        subprocess.run(
            [python, str(workers[0]), '--self-test'],
            cwd=ROOT,
            check=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1'},
        )
    print(f'All {len(plugins)} worker self-tests passed.')


if __name__ == '__main__':
    main()
