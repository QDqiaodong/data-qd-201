#!/usr/bin/env python3
"""Finite private-display verification. No credentials, host display or network."""
from __future__ import annotations
import hashlib
import errno
import json
import os
from pathlib import Path
import pwd
import select
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
import unicodedata

OUT = Path('/verification')
PROJECT = Path('/workspace/project')
RECORDING_SCOPE = 'codex-independent-product-verification'
WINDOW_TITLE = 'Product verification'
DISPLAY_CATCHUP_SECONDS = 120


def visible_rows(text, width=116):
    """Render every character without allowing product output to erase evidence."""
    row, columns = '', 0
    for char in text.expandtabs(4):
        if char == '\n':
            yield row
            row, columns = '', 0
            continue
        token = char if char.isprintable() or unicodedata.combining(char) else ascii(char)[1:-1]
        size = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in token)
        if columns + size > width:
            yield row
            row, columns = '', 0
        row += token
        columns += size
    if row:
        yield row


def display_transcript(source, acknowledgement):
    """Mirror the live stream while commands run; never replay a completed test."""
    with Path(source).open('r', encoding='utf-8', errors='backslashreplace', newline='') as handle:
        while True:
            text = handle.read(2048)
            if not text:
                time.sleep(0.05)
                continue
            rows = list(visible_rows(text))
            for offset in range(0, len(rows), 20):
                sys.stdout.write('\n'.join(rows[offset:offset + 20]) + '\n')
                sys.stdout.flush()
                time.sleep(1.5)  # A real screenful remains visible, including fast failures.
            temporary = Path(str(acknowledgement) + '.tmp')
            temporary.write_text(str(handle.tell()))
            temporary.replace(acknowledgement)


def wait_for_display(terminal, encoder, acknowledgement, target, timeout=DISPLAY_CATCHUP_SECONDS):
    deadline = time.monotonic() + timeout
    while True:
        if terminal.poll() is not None or encoder.poll() is not None:
            raise RuntimeError('live verification display or recording stopped')
        if acknowledgement.exists() and int(acknowledgement.read_text()) >= target:
            return time.time()
        if time.monotonic() >= deadline:
            raise RuntimeError('live verification output did not finish displaying; preserve diagnostics')
        time.sleep(0.05)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(names=None):
    files = {}
    paths = sorted(PROJECT.rglob('*')) if names is None else [PROJECT / name for name in names]
    for path in paths:
        if names is not None and any(parent.is_symlink() for parent in (path, *path.parents) if parent == PROJECT or PROJECT in parent.parents):
            raise ValueError('tracked artifact path was replaced by a symlink')
        if path.is_symlink():
            raise ValueError('symlink appeared in verification workspace')
        if path.is_file():
            files[str(path.relative_to(PROJECT))] = {'sha256': sha(path), 'executable': bool(path.stat().st_mode & 0o111)}
    return files



def expose_python_dependencies(home):
    """Keep prepared imports available when product subprocesses set PYTHONPATH.

    This is an isolated user-site bridge, not a product installation. Packaging
    acceptance must still run the actual offline install command.
    """
    cache = home / 'python-public-dependencies'
    if cache.is_dir():
        scheme = (sysconfig.get_preferred_scheme('user') if hasattr(sysconfig, 'get_preferred_scheme')
                  else 'osx_framework_user' if sys.platform == 'darwin' and getattr(sys, '_framework', '')
                  else 'posix_user')
        user_site = Path(sysconfig.get_path('purelib', scheme=scheme,
                                         vars={'userbase': str(home / '.local')}))
        user_site.mkdir(parents=True, exist_ok=True)
        (user_site / 'prepared-public-dependencies.pth').write_text(str(cache) + '\n')


def prepare_public_dependencies(uid, gid):
    """Reuse the existing engine's public-cache bridge, isolated to this container."""
    home = Path('/tmp/verification-home')
    home.mkdir(mode=0o700)
    prepared = Path('/opt/solo-dependencies')
    targets = []
    for name, target in [('m2', home / '.m2/repository'),
                         ('python', home / 'python-public-dependencies'),
                         ('frontend-node_modules', PROJECT / 'frontend/node_modules')]:
        source = prepared / name
        if not source.is_dir():
            continue
        if source.is_symlink() or target.exists() or target.is_symlink():
            raise ValueError('public dependency cache cannot replace an existing artifact path')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=True)
        targets.append(target)
    expose_python_dependencies(home)
    for base in [home, *targets]:
        for path in (base, *base.rglob('*')):
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if base not in resolved.parents and resolved != base:
                    raise ValueError('public dependency symlink escapes its isolated cache')
                continue
            if not path.is_dir() and not path.is_file():
                raise ValueError('nonregular public cache entry')
            os.chown(path, uid, gid)
            mode = path.stat().st_mode & 0o777
            path.chmod((mode | 0o700) if path.is_dir() else (mode | 0o600))
    return home


def stop(proc, interrupt=False):
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT if interrupt else signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def clear_group(pgid):
    remaining = group_exists(pgid)
    if remaining:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return remaining, not group_exists(pgid)


def stream_process(process, log, transcript, timeout_seconds):
    """Wait finitely; a closed output pipe must not become a ready-FD busy loop."""
    deadline = time.monotonic() + timeout_seconds
    eof = False
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            return True
        if eof:
            try:
                process.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                pass
        else:
            ready, _, _ = select.select([process.stdout], [], [], min(0.2, remaining))
            if ready:
                data = os.read(process.stdout.fileno(), 65536)
                if data:
                    log.write(data); log.flush(); transcript.write(data)
                else:
                    eof = True
    return False


def start_product_process(argv, log, transcript, **options):
    """Keep real exec failures in the same live capture, without a fictitious process."""
    try:
        return subprocess.Popen(argv, **options), None
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.EACCES, errno.ENOEXEC):
            raise  # Resource/recorder failures retain the infrastructure failure path.
        error = {'type': type(exc).__name__, 'errno': exc.errno, 'message': str(exc)}
        diagnostic = ('\n[command launch failed; process not started]\n'
                      + error['type'] + ': ' + error['message'] + '\n').encode()
        log.write(diagnostic); log.flush(); transcript.write(diagnostic)
        return None, error


def write_json(path, data):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def main():
    os.umask(0o077)
    OUT.mkdir(mode=0o700, exist_ok=True)
    plan = json.loads(Path('/verification-input/plan.json').read_text())
    before = snapshot()
    # Evidence lives on the container filesystem: host bind UID mapping is not trusted.
    uid = pwd.getpwnam('nobody').pw_uid
    gid = pwd.getpwnam('nobody').pw_gid
    for path in (PROJECT, *PROJECT.rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in source snapshot')
        if path.is_dir():
            path.chmod(0o755)
        os.chown(path, uid, gid)
    home = prepare_public_dependencies(uid, gid)
    OUT.chmod(0o700)
    transcript = OUT / 'transcript.log'
    transcript.touch(mode=0o600)
    started = time.time()
    capture_start = None
    capture_end = None
    xvfb = terminal = encoder = None
    commands = []
    result = {'ok': False, 'commands': commands, 'started_at': started,
              'recording_scope': RECORDING_SCOPE, 'display_protocol': 'live-paced-transcript-v1',
              'presentation_policy': 'product-only-v1'}
    transcript_handle = transcript.open('ab', buffering=0)
    try:
        env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'DISPLAY': ':99',
               'HOME': str(home), 'LANG': 'C.UTF-8', 'TERM': 'xterm-256color',
               'PYTHONUSERBASE': str(home / '.local'),
               'PYTHONPATH': str(home / 'python-public-dependencies') + ':/usr/lib/python3/dist-packages',
               'MAVEN_OPTS': '-Duser.home=' + str(home), 'MAVEN_CONFIG': str(home / '.m2'),
               'npm_config_cache': str(home / '.npm'), 'LIBGL_ALWAYS_SOFTWARE': '1'}
        xvfb = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x720x24', '-nolisten', 'tcp', '-ac'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        deadline = time.monotonic() + 15
        while not Path('/tmp/.X11-unix/X99').exists():
            if xvfb.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('private display did not become ready')
            time.sleep(0.1)
        acknowledgement = OUT / 'display-offset.txt'
        terminal = subprocess.Popen(['xterm', '-geometry', '120x32+0+0', '-fa', 'DejaVu Sans Mono', '-fs', '12',
                                     '-bg', '#10131a', '-fg', '#e6e6e6', '-title', WINDOW_TITLE,
                                     '-e', sys.executable, '-B', str(Path(__file__).resolve()), '--display-transcript',
                                     str(transcript), str(acknowledgement)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while True:
            window = subprocess.run(['xdotool', 'search', '--name', '^' + WINDOW_TITLE + '$'], env=env,
                                    capture_output=True, text=True)
            if window.returncode == 0:
                break
            if terminal.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('verification terminal did not become ready')
            time.sleep(0.1)
        window_id = window.stdout.strip().splitlines()[0]
        measured = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', window_id], env=env,
                                  capture_output=True, text=True, check=True)
        geometry = {key: int(value) for line in measured.stdout.splitlines() if '=' in line
                    for key, value in [line.split('=', 1)] if key in ('X', 'Y', 'WIDTH', 'HEIGHT')}
        if (set(geometry) != {'X', 'Y', 'WIDTH', 'HEIGHT'} or min(geometry.values()) < 0
                or geometry['WIDTH'] <= 0 or geometry['HEIGHT'] <= 0
                or geometry['X'] + geometry['WIDTH'] > 1280 or geometry['Y'] + geometry['HEIGHT'] > 720):
            raise RuntimeError('verification terminal exceeds the private recording viewport')
        result['display_geometry'] = geometry
        video = OUT / 'recording.mp4'
        progress = OUT / 'capture-progress.txt'
        encoder_log = (OUT / 'encoder.log').open('wb')
        encoder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
                                    '-f', 'x11grab', '-framerate', '10', '-video_size', '1280x720', '-i', ':99.0',
                                    '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24', '-pix_fmt', 'yuv420p',
                                    '-progress', str(progress), str(video)], env=env,
                                   stdin=subprocess.DEVNULL, stdout=encoder_log, stderr=encoder_log)
        deadline = time.monotonic() + 20
        while True:
            value = progress.read_text() if progress.exists() else ''
            frames = [int(line.split('=', 1)[1]) for line in value.splitlines() if line.startswith('frame=')]
            if frames and max(frames) > 0:
                capture_start = time.time()
                break
            if encoder.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('no actual private-display recording frame')
            time.sleep(0.1)
        for index, command in enumerate(plan['commands']):
            label = command['id']
            argv = command['argv']
            log_name = f'command-{index + 1:03d}.log'
            log_path = OUT / log_name
            argv_sha = hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode()).hexdigest()
            # Framework labels and paths belong in sidecar receipts, not the product recording.
            transcript_handle.write((f'\n=== Verification step {index + 1} ===\nExecutable: '
                                     + Path(argv[0]).name + '\n').encode())
            wait_for_display(terminal, encoder, acknowledgement, transcript.stat().st_size)
            began = time.time()
            timed_out = False
            with log_path.open('xb') as log:
                process, launch_error = start_product_process(argv, log, transcript_handle,
                                           cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                                           user=uid, group=gid, extra_groups=[])
                if launch_error is not None:
                    # Conventional runner status, explicitly not a product process exit.
                    code = 127 if launch_error['errno'] == errno.ENOENT else 126
                    remaining, group_cleared = False, True
                else:
                    timed_out = stream_process(process, log, transcript_handle, command.get('timeout_seconds', 300))
                    code = process.wait(timeout=5)
                    remaining, group_cleared = clear_group(process.pid)
                    # Drain the pipe after exit while preserving exact command output.
                    while True:
                        ready, _, _ = select.select([process.stdout], [], [], 0)
                        if not ready:
                            break
                        data = os.read(process.stdout.fileno(), 65536)
                        if not data:
                            break
                        log.write(data); transcript_handle.write(data)
                    process.stdout.close()
                log.flush()
                ended = time.time()
                commands.append({'id': label, 'argv': argv, 'argv_sha256': argv_sha, 'requirements': command['requirements'],
                                 'started_at': began, 'ended_at': ended, 'exit_code': code, 'timed_out': timed_out,
                                 'background_processes_detected': remaining and not timed_out,
                                 'process_group_cleanup_needed': remaining, 'process_group_cleared': group_cleared,
                                 'log_name': log_name, 'log_sha256': sha(log_path)})
                if launch_error is not None:
                    commands[-1].update(process_started=False, launch_error=launch_error,
                                        exit_code_source='runner-launch-error')
                    transcript_handle.write((f'\n[process_not_started; runner_exit_code={code}]\n').encode())
                else:
                    transcript_handle.write((f'\n[exit_code={code}; timed_out={timed_out}]\n').encode())
                if remaining or not group_cleared:
                    transcript_handle.write((f'[residual_process_group={remaining}; cleared={group_cleared}]\n').encode())
            if encoder.poll() is not None:
                raise RuntimeError('recording stopped while commands were executing')
            commands[-1]['display_completed_at'] = wait_for_display(
                terminal, encoder, acknowledgement, transcript.stat().st_size)
        transcript_handle.write(b'\n=== Verification complete; failures are retained above. ===\n')
        display_completed = wait_for_display(terminal, encoder, acknowledgement, transcript.stat().st_size)
        time.sleep(0.3)
        capture_end = time.time()
        stop(encoder, interrupt=True)
        encoder_log.close()
        if encoder.returncode not in (0, 255) or not video.is_file() or video.stat().st_size == 0:
            raise RuntimeError('private recording did not finalize')
        after = snapshot(before)
        changed = [name for name, value in before.items() if after.get(name) != value]
        groups_clean = all(c['process_group_cleared'] and not c['background_processes_detected'] for c in commands)
        result.update(ok=not changed and groups_clean, baseline_unchanged=not changed, changed_tracked_files=changed,
                      process_groups_clean=groups_clean,
                      capture_started_at=capture_start, capture_ended_at=capture_end,
                      display_completed_at=display_completed, display_bytes=transcript.stat().st_size,
                      ended_at=time.time(), video_name='recording.mp4', video_sha256=sha(video))
    except Exception as exc:
        # No key or inherited host environment is present; preserve private diagnostics.
        result.update(error=type(exc).__name__ + ': ' + str(exc), ended_at=time.time(),
                      capture_started_at=capture_start, capture_ended_at=capture_end)
    finally:
        stop(encoder, interrupt=True); stop(terminal); stop(xvfb)
        transcript_handle.close()
        result['commands'] = commands
        result['transcript_sha256'] = sha(transcript)
        write_json(OUT / 'result.json', result)
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--display-transcript':
        display_transcript(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(main())
