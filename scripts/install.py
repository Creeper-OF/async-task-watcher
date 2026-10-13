#!/usr/bin/env python3
"""Install the standalone skill without replacing user configuration or skills."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

NAME = 'async-task-watcher'
SOURCE = Path(__file__).resolve().parents[1] / 'src' / NAME
FILES = ('SKILL.md', 'agents/openai.yaml', 'references/design.md', 'scripts/supervisor.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, help='full destination directory ending in async-task-watcher')
    args = parser.parse_args()
    codex_dir = Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')).expanduser()
    target = (args.target or codex_dir / 'skills' / NAME).expanduser().absolute()
    if target.name != NAME:
        print(json.dumps({'error': 'invalid_target', 'message': f'Target directory must be named {NAME}.'}))
        return 2
    if target.exists() or target.is_symlink():
        print(json.dumps({'error': 'target_exists', 'message': 'Target already exists; no files changed.', 'target': str(target)}))
        return 2
    try:
        for relative in FILES:
            source_file = SOURCE / relative
            if not source_file.is_file() or source_file.is_symlink():
                raise OSError(f'Missing or unsafe package file: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()  # Exclusive reservation also prevents concurrent overwrites.
        for relative in FILES:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, destination)
    except FileExistsError:
        print(json.dumps({'error': 'target_exists', 'message': 'Target was created concurrently; existing files not overwritten.', 'target': str(target)}))
        return 2
    except OSError as exc:
        # Never recursively remove a directory a user might have started editing.
        print(json.dumps({'error': 'io_error', 'message': str(exc), 'target': str(target),
                          'partial_install_may_remain': target.exists()}))
        return 2
    print(json.dumps({'installed': str(target), 'files': list(FILES),
                      'configuration_changed': False, 'agents_file_changed': False}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
