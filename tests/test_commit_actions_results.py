"""Exercise result publishing against a local bare Git remote."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/commit_actions_results.py'


def git(cwd, *args):
    return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


@pytest.fixture
def repository(tmp_path):
    remote, repo = tmp_path / 'remote.git', tmp_path / 'repo'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.com')
    (repo / '.gitignore').write_text('results/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'Initial')
    git(repo, 'remote', 'add', 'origin', str(remote))
    git(repo, 'push', '-u', 'origin', 'main')
    return repo, remote


def publish(repo, job, attempt='1'):
    env = dict(os.environ, RESULTS_DIR='results', GITHUB_RUN_ID='123',
               GITHUB_RUN_ATTEMPT=attempt, RESULTS_JOB_ID=job)
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env,
                          capture_output=True, text=True)


def test_each_job_appends_saved_results_without_changing_main(repository):
    repo, remote = repository
    original = git(remote, 'rev-parse', 'refs/heads/main')
    results = repo / 'results'
    results.mkdir()
    (results / 'checkpoint.yaml').write_text('status: observed\n')
    for job in ('stress-1', 'stress-2'):
        result = publish(repo, job)
        assert result.returncode == 0, result.stderr
        assert git(remote, 'show', f'benchmark-results:results/actions/123/1/{job}/checkpoint.yaml') == 'status: observed'
    assert git(remote, 'rev-parse', 'refs/heads/main') == original
    assert git(repo, 'rev-parse', 'HEAD') == original
    assert git(remote, 'rev-list', '--count', 'main..benchmark-results') == '2'
    assert (results / 'checkpoint.yaml').is_file()
    duplicate = publish(repo, 'stress-1')
    assert duplicate.returncode != 0
    assert 'already exist' in duplicate.stderr
    assert publish(repo, 'stress-1', attempt='2').returncode == 0


def test_no_results_and_invalid_ids_do_not_publish(repository):
    repo, remote = repository
    result = publish(repo, 'stress-1')
    assert result.returncode == 0
    assert 'No saved results' in result.stdout
    assert not git(remote, 'branch', '--list', 'benchmark-results')
    assert publish(repo, '../escape').returncode != 0
