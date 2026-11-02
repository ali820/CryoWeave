#!/usr/bin/env python3
"""Dependency-free repository structure and syntax checks."""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.scratch', 'dist', 'coverage'}
checked_json = 0
checked_python = 0

for path in ROOT.rglob('*'):
    if not path.is_file() or any(part in SKIP for part in path.parts):
        continue
    if path.suffix == '.json':
        json.loads(path.read_text(encoding='utf-8'))
        checked_json += 1
    elif path.suffix == '.py':
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        checked_python += 1

for package in (ROOT / 'plugins').glob('*/package.json'):
    manifest = json.loads(package.read_text(encoding='utf-8'))
    patch = manifest.get('dsh', {}).get('bundle', {}).get('patch')
    assert patch, f'{package}: missing dsh.bundle.patch'
    assert (package.parent / patch).is_file(), f'{package}: missing bundle patch {patch}'
    assert (package.parent / 'index.js').is_file(), f'{package}: missing index.js'
    assert (package.parent / 'icon.svg').is_file(), f'{package}: missing icon.svg'
    assert (package.parent / 'tests' / 'host_smoke.mjs').is_file(), f'{package}: missing portable Host smoke test'
    for test_path in (package.parent / 'tests').glob('*.mjs'):
        test_source = test_path.read_text(encoding='utf-8')
        for machine_path in ('C:/Users/', 'E:/', 'CryoWeave-state'):
            assert machine_path not in test_source, f'{test_path}: machine-specific path {machine_path}'
    for locale in ('en', 'zh'):
        locale_path = package.parent / 'locale' / f'{locale}.json'
        data = json.loads(locale_path.read_text(encoding='utf-8'))
        assert data.get('meta', {}).get('title'), f'{locale_path}: missing meta.title'
        assert data.get('meta', {}).get('description'), f'{locale_path}: missing meta.description'

print(f'Repository checks passed: {checked_json} JSON files, {checked_python} Python files')
