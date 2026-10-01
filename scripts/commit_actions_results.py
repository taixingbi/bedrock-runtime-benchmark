"""Commit one Actions job's saved results to the benchmark-results branch."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

BRANCH = 'benchmark-results'


def git(*args, cwd=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def publish(results, run_id, attempt, job_id):
    for value in (run_id, attempt, job_id):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
            raise ValueError('Invalid Actions run, attempt or job identifier')
    results = Path(results).resolve()
    if not results.is_dir() or not any(p.is_file() for p in results.rglob('*')):
        print('No saved results to commit')
        return
    # Use a separate worktree so the benchmark checkout and artifacts stay intact.
    repo = Path(git('rev-parse', '--show-toplevel').stdout.strip())
    ref = f'refs/heads/{BRANCH}'
    remote_ref = f'refs/remotes/origin/{BRANCH}'
    exists = bool(git('ls-remote', '--heads', 'origin', ref).stdout.strip())
    if exists:
        git('fetch', 'origin', f'{ref}:{remote_ref}')
    with tempfile.TemporaryDirectory(prefix='benchmark-results-') as temporary:
        tree = Path(temporary) / 'checkout'
        git('worktree', 'add', '--detach', str(tree), remote_ref if exists else 'HEAD')
        try:
            destination = Path('results/actions') / run_id / attempt / job_id
            if (tree / destination).exists():
                raise ValueError(f'Results already exist at {destination}; rerun with a new attempt')
            shutil.copytree(results, tree / destination)
            git('add', '--force', '--', str(destination), cwd=tree)
            git('-c', 'user.name=github-actions[bot]',
                '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                'commit', '-m', f'Save benchmark results: run {run_id}, attempt {attempt}, job {job_id}', cwd=tree)
            for retry in range(3):
                pushed = git('push', 'origin', f'HEAD:{ref}', cwd=tree, check=False)
                if pushed.returncode == 0:
                    print(f'Committed results to {BRANCH}/{destination}')
                    return
                if retry == 2:
                    raise RuntimeError(pushed.stderr)
                # Preserve another writer's commits; never force-push result history.
                git('fetch', 'origin', f'{ref}:{remote_ref}', cwd=tree)
                git('-c', 'user.name=github-actions[bot]',
                    '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                    'rebase', remote_ref, cwd=tree)
        finally:
            git('worktree', 'remove', '--force', str(tree), cwd=repo)


if __name__ == '__main__':
    publish(os.environ['RESULTS_DIR'], os.environ['GITHUB_RUN_ID'],
            os.environ['GITHUB_RUN_ATTEMPT'], os.environ['RESULTS_JOB_ID'])
