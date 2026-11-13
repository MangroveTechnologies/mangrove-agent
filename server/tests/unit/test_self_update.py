"""scripts/update.sh, the setup.sh auto-update hook and the mangrove-agent command.

Every test uses throwaway git repositories under tmp_path: a bare "origin", a
user clone, and a maintainer clone that publishes new commits. No network.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ['update.sh', 'mangrove-agent', 'setup.sh', 'setup_support.py']

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git is required')


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    # Hermetic git: no user/system config, fixed identity; restored after the test.
    for key, value in {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                       'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
                       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(key, value)
    for key in ['MANGROVE_AGENT_NO_UPDATE', 'MANGROVE_AGENT_UPDATED', 'GIT_DIR', 'GIT_WORK_TREE',
                'BASE_URL', 'MANGROVE_AGENT_HOME']:
        monkeypatch.delenv(key, raising=False)
    env = dict(os.environ)
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', '-q', '-b', 'main', str(origin)], check=True)
    maint = tmp_path / 'maintainer'
    subprocess.run(['git', 'clone', '-q', str(origin), str(maint)], check=True, capture_output=True)
    git(maint, 'checkout', '-q', '-b', 'main')
    (maint / 'scripts').mkdir()
    for name in SCRIPTS:
        shutil.copy2(ROOT / 'scripts' / name, maint / 'scripts' / name)
    (maint / 'SAGE.md').write_text('project context\n')
    (maint / 'app.txt').write_text('v1\n')
    git(maint, 'add', '-A')
    git(maint, 'commit', '-q', '-m', 'initial')
    git(maint, 'push', '-q', '-u', 'origin', 'main')
    user = tmp_path / 'user clone'
    subprocess.run(['git', 'clone', '-q', str(origin), str(user)], check=True, capture_output=True)
    return maint, user, env


def publish(maint, name='app.txt', text='v2\n', message='change'):
    (maint / name).write_text(text)
    git(maint, 'add', '-A')
    git(maint, 'commit', '-q', '-m', message)
    git(maint, 'push', '-q')
    return git(maint, 'rev-parse', 'HEAD')


def update(user, env, *args):
    return subprocess.run(['bash', str(user / 'scripts/update.sh'), *args], cwd=user, env=env,
                          capture_output=True, text=True, timeout=30)


def test_up_to_date_is_a_quiet_success(repos):
    _, user, env = repos
    result = update(user, env)
    assert result.returncode == 0, result.stderr
    assert 'already up to date' in result.stdout


def test_fast_forwards_and_lists_new_commits(repos):
    maint, user, env = repos
    head = publish(maint, message='ship the new thing')
    result = update(user, env)
    assert result.returncode == 0, result.stderr
    assert git(user, 'rev-parse', 'HEAD') == head
    assert (user / 'app.txt').read_text() == 'v2\n'
    assert 'ship the new thing' in result.stdout


def test_keeps_unrelated_local_edits(repos):
    maint, user, env = repos
    (user / 'SAGE.md').write_text('my personalised context\n')
    head = publish(maint)
    result = update(user, env)
    assert result.returncode == 0, result.stderr
    assert git(user, 'rev-parse', 'HEAD') == head
    assert (user / 'SAGE.md').read_text() == 'my personalised context\n'


def test_refuses_when_local_edits_collide(repos):
    maint, user, env = repos
    before = git(user, 'rev-parse', 'HEAD')
    (user / 'app.txt').write_text('my local hack\n')
    publish(maint)
    result = update(user, env)
    assert result.returncode == 3
    assert 'app.txt' in result.stderr
    assert git(user, 'rev-parse', 'HEAD') == before
    assert (user / 'app.txt').read_text() == 'my local hack\n'


def test_untracked_file_in_the_way_is_never_overwritten(repos):
    maint, user, env = repos
    before = git(user, 'rev-parse', 'HEAD')
    (user / 'new.txt').write_text('mine\n')
    publish(maint, name='new.txt', text='theirs\n')
    result = update(user, env)
    assert result.returncode == 1
    assert git(user, 'rev-parse', 'HEAD') == before
    assert (user / 'new.txt').read_text() == 'mine\n'


def test_diverged_history_is_left_alone(repos):
    maint, user, env = repos
    (user / 'local.txt').write_text('x\n')
    git(user, 'add', '-A')
    git(user, 'commit', '-q', '-m', 'local commit')
    before = git(user, 'rev-parse', 'HEAD')
    publish(maint)
    result = update(user, env)
    assert result.returncode == 3
    assert 'not updating automatically' in result.stderr
    assert git(user, 'rev-parse', 'HEAD') == before


def test_feature_branch_is_never_moved(repos):
    maint, user, env = repos
    git(user, 'checkout', '-q', '-b', 'feature', '--track', 'origin/main')
    before = git(user, 'rev-parse', 'HEAD')
    publish(maint)
    result = update(user, env)
    assert result.returncode == 3
    assert "only follow 'main'" in result.stderr
    assert git(user, 'rev-parse', 'HEAD') == before


def test_unreachable_remote_warns_without_failing_hard(repos):
    _, user, env = repos
    git(user, 'remote', 'set-url', 'origin', str(user.parent / 'missing.git'))
    result = update(user, env)
    assert result.returncode == 1
    assert 'continuing with the installed version' in result.stderr


def test_opt_out(repos):
    maint, user, env = repos
    before = git(user, 'rev-parse', 'HEAD')
    publish(maint)
    result = update(user, dict(env, MANGROVE_AGENT_NO_UPDATE='1'))
    assert result.returncode == 3
    assert git(user, 'rev-parse', 'HEAD') == before


def test_copy_nested_in_another_repository_is_not_managed(repos, tmp_path):
    _, user, env = repos
    outer = tmp_path / 'outer'
    outer.mkdir()
    git(outer, 'init', '-q')
    nested = outer / 'vendored'
    shutil.copytree(user / 'scripts', nested / 'scripts')
    result = update(nested, env)
    assert result.returncode == 3
    assert 'not a git clone of mangrove-agent' in result.stdout


def test_setup_updates_then_restarts_on_new_code_with_original_args(repos):
    maint, user, env = repos
    # The published setup.sh is replaced by a probe that records how it was run.
    probe = '#!/usr/bin/env bash\nprintf "%s|%s|%s\\n" "$MANGROVE_AGENT_UPDATED" "${BASE_URL:-unset}" "$*" > ran.txt\n'
    publish(maint, name='scripts/setup.sh', text=probe, message='new setup')
    env = dict(env, BARE_PORT='9099')
    env.pop('BASE_URL', None)
    result = subprocess.run(['bash', str(user / 'scripts/setup.sh'), '--no-mcp', '--auth', 'x402'],
                            cwd=user, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (user / 'ran.txt').read_text() == '1|unset|--no-mcp --auth x402\n'


@pytest.mark.parametrize('args, updated', [((), True), (('--no-update',), False)])
def test_setup_updates_before_taking_the_setup_lock(repos, args, updated):
    maint, user, env = repos
    before = git(user, 'rev-parse', 'HEAD')
    head = publish(maint)
    # A held lock stops setup right after the update step, before any configuration.
    (user / 'agent-data/.setup.lock').mkdir(parents=True)
    result = subprocess.run(['bash', str(user / 'scripts/setup.sh'), '--no-mcp', *args],
                            cwd=user, env=dict(env, BARE_PORT='9099'), capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert 'Another setup is active' in result.stderr
    assert git(user, 'rev-parse', 'HEAD') == (head if updated else before)


# -- mangrove-agent command ----------------------------------------------------

def cli(path, *args, env=None, cwd=None):
    return subprocess.run(['bash', str(path), *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=30)


def test_cli_resolves_its_clone_through_a_symlink(repos, tmp_path):
    _, user, env = repos
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    link = bin_dir / 'mangrove-agent'
    link.symlink_to(user / 'scripts/mangrove-agent')
    result = subprocess.run([str(link), 'where'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == user.resolve()


def test_cli_update_delegates_to_update_script(repos, tmp_path):
    maint, user, env = repos
    head = publish(maint)
    result = cli(user / 'scripts/mangrove-agent', 'update', env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert git(user, 'rev-parse', 'HEAD') == head


def test_cli_start_runs_setup_with_passthrough_args(repos, tmp_path):
    _, user, env = repos
    (user / 'scripts/setup.sh').write_text('#!/usr/bin/env bash\nprintf "%s" "$*" > "$(dirname "$0")/../args.txt"\n')
    result = cli(user / 'scripts/mangrove-agent', 'start', '--docker', '--no-mcp', env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (user / 'args.txt').read_text() == '--docker --no-mcp'


def test_cli_status_and_stop_when_nothing_runs(repos, tmp_path):
    _, user, env = repos
    env = dict(env, PATH=f"{tmp_path / 'nodocker'}:/usr/bin:/bin")
    status = cli(user / 'scripts/mangrove-agent', 'status', env=env)
    assert status.returncode == 1
    assert 'not running' in status.stdout
    assert 'main@' in status.stdout
    stop = cli(user / 'scripts/mangrove-agent', 'stop', env=env)
    assert stop.returncode == 0
    assert 'not running' in stop.stdout


def test_cli_stop_never_signals_an_unrelated_process(repos):
    _, user, env = repos
    env = dict(env, PATH='/usr/bin:/bin')
    other = subprocess.Popen(['sleep', '30'])
    try:
        (user / 'agent-data').mkdir()
        (user / 'agent-data/bare.pid').write_text(f'{other.pid}\n')
        result = cli(user / 'scripts/mangrove-agent', 'stop', env=env)
        assert result.returncode == 1
        assert 'not identifiable' in result.stderr
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_cli_unknown_command_is_a_usage_error(repos):
    _, user, env = repos
    result = cli(user / 'scripts/mangrove-agent', 'frobnicate', env=env)
    assert result.returncode == 2
    assert 'unknown command' in result.stderr
