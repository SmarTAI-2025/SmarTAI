"""Regression checks for inert normal discovery and fail-closed explicit entry."""
from audit_guard import require_probe_run
require_probe_run()

from pathlib import Path
import subprocess
import sys

import pytest

PROBES = Path(__file__).resolve().parent


def clean_env(tmp_path):
    return {'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin',
            'HOME': str(tmp_path), 'TMPDIR': str(tmp_path), 'LANG': 'en_US.UTF-8',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}


@pytest.mark.parametrize('scope', ['root', 'directory'])
def test_ordinary_discovery_ignores_opt_in_probes(tmp_path, scope):
    (tmp_path / 'test_sentinel.py').write_text('def test_sentinel():\n    assert True\n')
    (tmp_path / 'u3').symlink_to(PROBES.parent, target_is_directory=True)
    args = [str(PROBES)] if scope == 'directory' else []
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
        '-p', 'no:cacheprovider', *args], cwd=tmp_path, env=clean_env(tmp_path),
        capture_output=True, text=True, timeout=15)
    output = result.stdout + result.stderr
    assert result.returncode == (5 if scope == 'directory' else 0), output
    assert ('no tests collected' if scope == 'directory' else '1 test collected') in output
    assert 'ERROR' not in output


def test_explicit_pytest_file_cannot_bypass_launcher(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
        '-p', 'no:cacheprovider', str(PROBES / 'test_mapping.py')], cwd=tmp_path,
        env=clean_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'Use run_offline.py; direct execution is disabled' in result.stdout + result.stderr


def test_every_probe_rejects_direct_import_before_backend(tmp_path):
    script = '''
import builtins, pathlib, runpy, sys
path = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(path.parent))
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'backend' or name.startswith('backend.'):
        raise AssertionError('backend imported before isolation guard')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
try:
    runpy.run_path(str(path))
except RuntimeError as exc:
    assert str(exc) == 'Use run_offline.py; direct execution is disabled', exc
    print('rejected-before-backend-import')
else:
    raise AssertionError('unguarded probe')
'''
    modules = sorted(PROBES.glob('test_*.py'))
    assert len(modules) >= 5
    for module in modules:
        result = subprocess.run([sys.executable, '-c', script, str(module)], cwd=tmp_path,
            env=clean_env(tmp_path), capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, (module.name, result.stdout, result.stderr)
        assert result.stdout.strip() == 'rejected-before-backend-import'
