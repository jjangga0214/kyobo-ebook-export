#!/usr/bin/env python3
"""Search and export locally owned Kyobo books, with verified cleanup.

Python standard library plus Apple's local compilers/frameworks and Poppler.
No network requests, original-app launches, purchases, or original-data writes.
"""
import argparse
import datetime
import errno
import fcntl
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import quote

from formats import epub_members, package_epub, sha, validate_pdf

HERE = Path(__file__).resolve().parent
BUNDLE = 'kr.co.kyobobook.iPadB2C'
SCHEMA = 1
LSREGISTER = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'


class Blocked(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


def dump(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def normalize(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value).casefold())


def filename(title, fmt):
    name = unicodedata.normalize('NFC', title)
    name = re.sub(r'^\[epub3\.0\]\s*', '', name, flags=re.I)
    name = re.sub(r'[/\\:\x00-\x1f]', ' - ', name).strip(' .')
    name = re.sub(r' +', ' ', name) or 'book'
    while len(name.encode('utf-8')) > 200:
        name = name[:-1]
    return name + '.' + fmt


def discover_data(explicit=None):
    candidates = [Path(explicit).expanduser()] if explicit else list((Path.home() / 'Library/Containers').glob('*/Data/Library/Kyobo/data/books.db'))
    if not explicit:
        candidates = [p.parents[3] for p in candidates]
    candidates = sorted(set(p.resolve() for p in candidates if (p / 'Library/Kyobo/data/books.db').is_file()))
    if not explicit:
        identified = []
        for p in candidates:
            try:
                metadata = plistlib.loads((p.parent / '.com.apple.containermanagerd.metadata.plist').read_bytes())
                if metadata.get('MCMMetadataIdentifier') == BUNDLE:
                    identified.append(p)
            except (OSError, plistlib.InvalidFileException):
                continue
        if identified:
            candidates = identified
    if len(candidates) > 1 and not explicit:
        populated = []
        for p in candidates:
            try:
                library = read_library(p)
                if any(r['owned'] and r['downloaded'] for r in library):
                    populated.append(p)
            except sqlite3.Error:
                continue
        if len(populated) == 1:
            candidates = populated
    if len(candidates) != 1:
        raise Blocked('data_selection_required', 'Choose the Kyobo data container with --data.', candidates=[str(p) for p in candidates])
    return candidates[0]


def discover_app(explicit=None):
    if explicit:
        candidate = Path(explicit).expanduser()
        if (candidate / 'Wrapper/iPadB2C.app').is_dir():
            candidate /= 'Wrapper/iPadB2C.app'
        candidates = [candidate]
    else:
        candidates = [p / 'Wrapper/iPadB2C.app' for parent in (Path('/Applications'), Path.home() / 'Applications') for p in parent.glob('*eBook.app')]
    valid = []
    for p in candidates:
        try:
            info = plistlib.loads((p / 'Info.plist').read_bytes())
            if info.get('CFBundleIdentifier') == BUNDLE and (p / 'iPadB2C').is_file():
                valid.append(p.resolve())
        except (OSError, plistlib.InvalidFileException):
            continue
    if len(set(valid)) != 1:
        raise Blocked('app_selection_required', 'Choose the installed Kyobo app with --app.', candidates=[str(p) for p in valid])
    return valid[0]


def read_library(data):
    db = sqlite3.connect(f'file:{data}/Library/Kyobo/data/books.db?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in db.execute('SELECT barcode,title,author,book_type,root_path,expire_date,service_type,status FROM books')]
    finally:
        db.close()
    for row in rows:
        row['format'] = {'1': 'pdf', '5': 'epub'}.get(str(row['book_type']), 'unsupported')
        row['id'] = row['barcode'] + ':' + row['format']
        row['owned'] = row['expire_date'] == '299912312359' and row['service_type'] == 1
        path = Path(row['root_path']) if row['root_path'] else None
        row['downloaded'] = bool(path and path.is_dir() and path.resolve().is_relative_to((data / 'Library/Kyobo/books').resolve()))
        if row['downloaded']:
            assert path is not None
            row['downloaded'] = (path / ('book.dat' if row['format'] == 'pdf' else 'META-INF/container.xml')).is_file()
    return rows


def public_book(row):
    return {k: row[k] for k in ('id', 'barcode', 'title', 'author', 'format', 'owned', 'downloaded')}


def matches(row, query):
    needle = normalize(query)
    return bool(needle) and (query in (row['barcode'], row['id']) or needle in normalize(row['title']) or all(normalize(t) in normalize(row['title']) for t in query.split()))


def select_books(rows, queries, all_books=False, fmt=None):
    pool = [r for r in rows if not fmt or r['format'] == fmt]
    if all_books:
        pool = [r for r in pool if r['owned']]
        # Ignore a non-downloaded duplicate registration if the title has a local edition.
        selected = [r for r in pool if r['downloaded'] or not any(s['barcode'] == r['barcode'] and s['downloaded'] for s in pool)]
    else:
        selected = []
        for query in queries:
            found = [r for r in pool if matches(r, query)]
            local = [r for r in found if r['downloaded']]
            if len({r['barcode'] for r in found}) == 1 and local:
                found = local
            if len(found) != 1:
                raise Blocked('book_selection_required', 'A book selector must match exactly one edition.', query=query, candidates=[public_book(r) for r in found])
            selected += found
    selected = list({r['id']: r for r in selected}.values())
    if not selected:
        raise Blocked('no_books', 'No matching books are stored in this library.')
    return selected


def adapter_for(app):
    digest = sha(app / 'iPadB2C')
    for path in HERE.glob('adapter-*.json'):
        adapter = json.loads(path.read_text())
        main = next(e for e in adapter['executables'] if e['path'] == 'iPadB2C')
        if main['carrier_sha256'] == digest:
            return adapter
    raise Blocked('unsupported_app_build', 'The installed executable does not match a tested adapter. Do not apply old byte patches.', executable_sha256=digest)


def preflight(app, data):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise Blocked('unsupported_host', 'This adapter requires Apple Silicon macOS.')
    missing = [tool for tool in ('xcrun', 'codesign', 'sandbox-exec', 'pdfinfo', 'pdftotext') if not shutil.which(tool)]
    if missing:
        raise Blocked('missing_dependencies', 'Required local tools are missing; nothing was installed.', tools=missing)
    adapter = adapter_for(app)
    for item in adapter['executables']:
        p = app / item['path']
        if not p.is_file() or sha(p) != item['carrier_sha256']:
            raise Blocked('unsupported_framework_build', 'An embedded framework differs from the tested build.', path=item['path'])
    if not (data / 'Library/Preferences' / (BUNDLE + '.plist')).is_file():
        raise Blocked('missing_preferences', 'The original local preferences are unavailable.')
    return adapter


OS_PREFERENCE_LINKS = {'com.apple.security.plist', 'com.apple.security_common.plist', '.GlobalPreferences.plist'}


def source_files(root, os_preferences=False):
    root = Path(root)
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            if os_preferences and p.name in OS_PREFERENCE_LINKS:
                continue
            raise Blocked('unsafe_source_link', 'A source-data symlink requires explicit review.', path=str(p))
        if p.is_file():
            yield p


def state_manifest(data):
    return {str(p.relative_to(data)): sha(p) for rel in ('Library/Kyobo/data', 'Library/Preferences', 'Library/Fasoo') for p in source_files(data / rel, os_preferences=rel == 'Library/Preferences')}


def arm64_slice(data):
    if data[:4] == b'\xcf\xfa\xed\xfe':
        return data
    if data[:4] not in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
        raise Blocked('unsupported_macho', 'Unsupported Mach-O carrier.')
    wide = data[3] == 0xbf
    for i in range(struct.unpack_from('>I', data, 4)[0]):
        cpu, subtype, start, size, align = struct.unpack_from('>IIQQI' if wide else '>IIIII', data, 8 + i * (32 if wide else 20))
        if cpu == 0x100000c:
            if start + size > len(data):
                raise Blocked('invalid_macho', 'Mach-O slice exceeds its carrier.')
            return data[start:start + size]
    raise Blocked('unsupported_macho', 'No arm64 slice was found.')


def execute(args, logs, label, timeout=60, env=None, retain_stdout=True):
    logs = Path(logs)
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        stdout, stderr = p.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise
    if retain_stdout:
        (logs / (label + '.stdout')).write_bytes(stdout)
    (logs / (label + '.stderr')).write_bytes(stderr)
    return subprocess.CompletedProcess(args, p.returncode, stdout, stderr)


def checked(args, logs, label, timeout=60, env=None, retain_stdout=True):
    result = execute(args, logs, label, timeout, env, retain_stdout)
    if result.returncode:
        raise Blocked('command_failed', 'A local command failed. Inspect its private log.', command=Path(args[0]).name, exit=result.returncode, log=str(Path(logs) / (label + '.stderr')))
    return result


def patch_clone(app, target, adapter):
    shutil.copytree(app, target, symlinks=True)
    for p in target.rglob('*'):
        if p.is_symlink() and not p.resolve().is_relative_to(target.resolve()):
            raise Blocked('unsafe_app_link', 'An app link points outside the disposable copy.')
    for item in adapter['executables']:
        p = target / item['path']
        original = arm64_slice(p.read_bytes())
        data = bytearray(original)
        import hashlib
        if hashlib.sha256(data).hexdigest() != item['arm64_sha256']:
            raise Blocked('adapter_mismatch', 'The cloned slice does not match its adapter.')
        for edit in item['patches']:
            start = edit['offset']
            before, after = bytes.fromhex(edit['before']), bytes.fromhex(edit['after'])
            if len(before) != len(after) or data[start:start + len(before)] != before:
                raise Blocked('adapter_mismatch', 'Byte-patch precondition failed.')
            data[start:start + len(before)] = after
        # The archived copy was re-signed with a shorter ad-hoc signature. Its
        # signing-size fields are not compatibility edits. Keep this source's
        # intact signature extent until codesign replaces it coherently.
        def signing_fields(image):
            position, result = 32, {}
            for _ in range(struct.unpack_from('<I', image, 16)[0]):
                cmd, size = struct.unpack_from('<II', image, position)
                if cmd == 0x1d:
                    result['signature'] = (position + 8, bytes(image[position + 8:position + 16]))
                if cmd == 0x19 and bytes(image[position + 8:position + 24]).split(b'\0')[0] == b'__LINKEDIT':
                    result['linkedit_vm_size'] = (position + 32, bytes(image[position + 32:position + 40]))
                    result['linkedit_file_size'] = (position + 48, bytes(image[position + 48:position + 56]))
                position += size
            return result
        prior, current = signing_fields(original), signing_fields(data)
        for field, (_, value) in prior.items():
            start = current[field][0]
            data[start:start + len(value)] = value
        if item['path'] == 'iPadB2C':
            old, new = b'@executable_path/ResearchBootstrap.dylib', b'@executable_path/NativeExport.dylib'
            if data.count(old) != 1:
                raise Blocked('adapter_mismatch', 'Expected exactly one test-helper load command.')
            data = data.replace(old, new + b'\0' * (len(old) - len(new)))
        p.write_bytes(data)
    info_path = target / 'Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    info.update(adapter['plist_changes'])
    info.pop('LSEnvironment', None)
    info.update(CFBundleIdentifier='local.kyobo.export.' + uuid.uuid4().hex, CFBundleName='Kyobo Offline Export', CFBundleDisplayName='Kyobo Offline Export')
    info_path.write_bytes(plistlib.dumps(info))
    for name in ('PlugIns', 'ResearchBootstrap.dylib'):
        p = target / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    return info['CFBundleIdentifier']


def stage(run, app, data, adapter, selected):
    scratch = run / 'scratch'
    scratch.mkdir(mode=0o700)
    home, backup = scratch / 'home', run / 'state-backup'
    home.mkdir(mode=0o700)
    backup.mkdir(mode=0o700)
    for rel in ('Library/Kyobo/data', 'Library/Preferences', 'Library/Fasoo'):
        prefs = rel == 'Library/Preferences'
        list(source_files(data / rel, os_preferences=prefs))
        def ignore_links(directory, names):
            return [n for n in names if prefs and n in OS_PREFERENCE_LINKS and (Path(directory) / n).is_symlink()]
        shutil.copytree(data / rel, backup / rel, ignore=ignore_links)
        shutil.copytree(data / rel, home / rel, ignore=ignore_links)
    db = sqlite3.connect(f'file:{data}/Library/Kyobo/data/books.db?mode=ro', uri=True)
    try:
        for p in (backup / 'Library/Kyobo/data/books.db', home / 'Library/Kyobo/data/books.db'):
            with sqlite3.connect(p) as destination:
                db.backup(destination)
    finally:
        db.close()
    (home / 'Documents').mkdir()
    (home / 'tmp').mkdir()
    store = home / 'Library/Fasoo/.fasooStore'
    restored = 0
    for legacy in store.glob('*.cer.corrupt.bak'):
        try:
            repo = plistlib.loads(legacy.read_bytes())
        except plistlib.InvalidFileException:
            continue
        if isinstance(repo, dict) and any(k.endswith('deviceKey') for k in repo):
            shutil.copy2(legacy, legacy.with_name(legacy.name.removesuffix('.corrupt.bak')))
            restored += 1
    if not restored:
        raise Blocked('legacy_repository_unavailable', 'This tested path needs a usable retained local legacy certificate repository; none was found. No original store was changed.')
    jobs = []
    for row in selected:
        src = Path(row['root_path'])
        rel = src.resolve().relative_to(data / 'Library/Kyobo/books')
        dst = home / 'Library/Kyobo/books' / rel
        shutil.copytree(src, dst)
        key = row['barcode'] + '-' + row['format']
        job = {'barcode': row['barcode'], 'book_type': row['book_type'], 'format': row['format'], 'root': str(dst),
               'output': str(scratch / (key + ('.pdf' if row['format'] == 'pdf' else '-epub')))}
        if row['format'] == 'epub':
            job['members'] = epub_members(dst)[0]
        dump(run / (key + '.job.json'), job)
        jobs.append(job)
    clone_db = sqlite3.connect(home / 'Library/Kyobo/data/books.db')
    clone_db.execute('UPDATE books SET root_path=replace(root_path,?,?)', (str(data), str(home)))
    clone_db.commit()
    clone_db.close()
    target = scratch / 'Kyobo Export.app'
    bundle_id = patch_clone(app, target, adapter)
    run_metadata = json.loads((run / 'run.json').read_text())
    run_metadata['test_bundle_id'] = bundle_id
    dump(run / 'run.json', run_metadata)
    entitlements = scratch / 'entitlements.plist'
    entitlements.write_bytes(plistlib.dumps({'com.apple.security.get-task-allow': True, 'com.apple.security.cs.disable-library-validation': True}))
    def quoted(p):
        return json.dumps(str(p))
    (scratch / 'offline.sb').write_text(f'(version 1)\n(allow default)\n(deny network*)\n(deny file-write*)\n(allow file-write* (subpath {quoted(run)}) (literal "/dev/null"))\n(deny file-read* (subpath {quoted(data)}))\n')
    for source, output in (('NativeExport.m', target / 'NativeExport.dylib'), ('SafetyClient.m', scratch / 'SafetyClient.dylib')):
        checked(['xcrun', 'clang', '-target', 'arm64-apple-ios16.0-macabi', '-fobjc-arc', '-Wno-deprecated-declarations', '-dynamiclib', str(HERE / source), '-framework', 'Foundation', '-framework', 'Security', '-lsqlite3', '-o', str(output)], run, 'compile-' + source, timeout=120)
        checked(['codesign', '--force', '--sign', '-', '--timestamp=none', str(output)], run, 'sign-' + source)
    for i, item in enumerate(adapter['executables']):
        if item['path'] != 'iPadB2C':
            checked(['codesign', '--force', '--sign', '-', '--timestamp=none', str((target / item['path']).parent)], run, f'sign-framework-{i}')
    checked(['codesign', '--force', '--sign', '-', '--timestamp=none', '--entitlements', str(entitlements), str(target)], run, 'sign-runner')
    checked(['codesign', '--verify', '--deep', '--strict', str(target)], run, 'verify-runner')
    validator = scratch / 'validate'
    checked(['xcrun', 'swiftc', '-module-cache-path', str(scratch / 'swift-cache'), str(HERE / 'Validate.swift'), '-o', str(validator)], run, 'compile-validator', timeout=120)
    return jobs, target, home, validator


def check_run(run):
    run = Path(run).expanduser()
    if run.is_symlink() or not re.fullmatch(r'run-[0-9a-f]{32}', run.name):
        raise Blocked('unsafe_cleanup_path', 'Cleanup requires an exact tool-created run directory.')
    run = run.resolve()
    marker = run / 'run.json'
    if not marker.is_file() or marker.is_symlink() or run.stat().st_uid != os.getuid():
        raise Blocked('unsafe_cleanup_path', 'Run ownership/marker check failed.')
    data = json.loads(marker.read_text())
    if data.get('schema') != SCHEMA or data.get('run_id') != run.name:
        raise Blocked('unsafe_cleanup_path', 'Run identity check failed.')
    if (run / 'scratch').is_symlink():
        raise Blocked('unsafe_cleanup_path', 'Disposable root cannot be a symlink.')
    return run, data


def telemetry_timestamp_only(before, after):
    """A narrowly identified SDK fetch timestamp, not arbitrary app state."""
    key = 'APMExperimentFetchSuccessTimestamp'
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    return changed == {key} and all(isinstance(value.get(key), (int, float)) for value in (before, after))


def console_validation(validation):
    if validation['format'] == 'pdf':
        apple = validation['pdfkit']
        return {'format': 'pdf', 'pages': validation['pages'], 'rendered_pages': apple['rendered_pages'],
                'encrypted': validation['encrypted'], 'text_characters': validation['text_characters'],
                'replacement_characters': validation['replacement_characters'],
                'pdfkit_replacement_characters': apple['replacement_characters'],
                'poppler_warning_count': len(validation.get('poppler_warnings', []))}
    return {'format': 'epub', 'spine_documents': validation['spine_documents'],
            'manifest_resources': validation['manifest_resources'], 'zip_crc_passed': validation['zip_crc_passed'],
            'images': validation['images'], 'warning_count': len(validation['warnings'])}


def cleanup(run):
    run, metadata = check_run(run)
    scratch = run / 'scratch'
    if not (run / 'baseline.json').exists():
        if scratch.exists():
            raise Blocked('missing_baseline', 'Scratch exists without its preservation baseline; inspect the run before cleanup.')
        result = {'scratch_removed': True, 'original_state_verification': 'not_available', 'ok': False}
        dump(run / 'cleanup.json', result)
        return result
    procs = checked(['ps', '-axo', 'pid=,comm='], run, 'cleanup-processes').stdout.decode()
    if any(str(scratch) in s for s in procs.splitlines()):
        raise Blocked('runner_still_active', 'A disposable process is still active; cleanup did not remove its files.')
    registry = checked([LSREGISTER, '-dump'], run, 'registry-before', retain_stdout=False).stdout.decode()
    paths = [re.sub(r' \(0x[0-9a-fA-F]+\)$', '', s.partition('path:')[2].strip()) for s in registry.splitlines() if 'path:' in s and str(scratch) in s]
    for i, p in enumerate(set(paths)):
        if not Path(p).is_relative_to(scratch):
            raise Blocked('unsafe_registration_path', 'A registration path was outside the disposable root.')
        checked([LSREGISTER, '-u', p], run, f'unregister-{i}')
    bundle_id = metadata.get('test_bundle_id', '')
    global_prefs_removed = False
    if bundle_id:
        if not re.fullmatch(r'local\.kyobo\.export\.[0-9a-f]{32}', bundle_id):
            raise Blocked('unsafe_preference_domain', 'Unexpected test preference domain.')
        prefs = Path.home() / 'Library/Preferences' / (bundle_id + '.plist')
        if prefs.is_symlink():
            raise Blocked('unsafe_preference_path', 'Test preference path is a symlink.')
        if prefs.exists():
            shutil.copy2(prefs, run / 'test-preferences-before-removal.plist')
            checked(['defaults', 'delete', bundle_id], run, 'remove-test-preferences')
            global_prefs_removed = True
    if scratch.exists():
        shutil.rmtree(scratch)
    registry = checked([LSREGISTER, '-dump'], run, 'registry-after', retain_stdout=False).stdout.decode()
    remaining = [s for s in registry.splitlines() if 'path:' in s and str(scratch) in s]
    original = Path(metadata['data'])
    before = json.loads((run / 'baseline.json').read_text())
    now = state_manifest(original)
    changes = [k for k in set(now) | set(before['state']) if now.get(k) != before['state'].get(k)]
    telemetry_changes = []
    telemetry_path = 'Library/Preferences/APMExperimentSuiteName.plist'
    if telemetry_path in changes:
        saved = run / 'state-backup' / telemetry_path
        try:
            if saved.is_file() and sha(saved) == before['state'].get(telemetry_path) and telemetry_timestamp_only(
                    plistlib.loads(saved.read_bytes()), plistlib.loads((original / telemetry_path).read_bytes())):
                telemetry_changes.append({'path': telemetry_path, 'changed_keys': ['APMExperimentFetchSuccessTimestamp'],
                                          'classification': 'analytics_fetch_timestamp', 'cause': 'unattributed'})
        except (OSError, plistlib.InvalidFileException):
            pass
    critical_changes = [p for p in changes if p not in {item['path'] for item in telemetry_changes}]
    book_changes = []
    for p, expected in before['book_files'].items():
        path = original / p
        if not path.is_file() or sha(path) != expected:
            book_changes.append(p)
    signature = execute(['codesign', '--verify', '--ignore-resources', str(Path(metadata['app']))], run, 'original-signature')
    result = {'scratch_removed': not scratch.exists(), 'remaining_registrations': remaining,
              'test_global_preferences_removed': global_prefs_removed,
              'state_files_checked': len(now), 'state_changes': changes, 'book_files_checked': len(before['book_files']),
              'critical_state_changes': critical_changes, 'telemetry_changes': telemetry_changes,
              'book_changes': book_changes, 'original_executable_unchanged': sha(Path(metadata['app']) / 'iPadB2C') == before['executable_sha256'],
              'original_code_signature_exit': signature.returncode}
    result['all_source_state_unchanged'] = not changes and not book_changes
    result['ok'] = not remaining and not critical_changes and not book_changes and result['original_executable_unchanged'] and signature.returncode == 0
    dump(run / 'cleanup.json', result)
    return result


def publish(candidate, destination):
    """Expose only a complete validated inode; never overwrite a concurrent file."""
    candidate, destination = Path(candidate), Path(destination)
    digest = sha(candidate)
    if destination.is_symlink():
        raise Blocked('output_collision', 'Output is a symlink; it was not changed.', path=str(destination))
    try:
        os.link(candidate, destination)
    except FileExistsError:
        if not destination.is_file() or destination.is_symlink() or sha(destination) != digest:
            raise Blocked('output_collision', 'An existing output differs; it was not overwritten.', path=str(destination))
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        temp = destination.parent / ('.kyobo-publish-' + uuid.uuid4().hex)
        try:
            with candidate.open('rb') as src, temp.open('xb') as dst:
                shutil.copyfileobj(src, dst)
                dst.flush(); os.fsync(dst.fileno())
            publish(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    destination.chmod(0o600)
    if sha(destination) != digest:
        raise Blocked('publication_hash_mismatch', 'Published file digest differs.')
    return digest


@contextmanager
def output_lock(output):
    parent = Path(output) / '.kyobo-export'
    if parent.is_symlink():
        raise Blocked('unsafe_work_path', 'The private run directory cannot be a symlink.')
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    fd = os.open(parent / 'export.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('output_busy', 'Another export is using this output directory. Wait for its completion.')
        yield
    finally:
        os.close(fd)


def export(args):
    data, app = discover_data(args.data), discover_app(args.app)
    rows = read_library(data)
    selected = select_books(rows, args.book, args.all, args.format)
    invalid = [public_book(r) for r in selected if not r['owned'] or not r['downloaded'] or r['format'] == 'unsupported']
    if invalid:
        raise Blocked('books_unavailable', 'Selected books must be locally downloaded, permanently owned PDF/EPUB editions. Download missing books in the official app, then retry.', books=invalid)
    adapter = preflight(app, data)
    output = Path(args.out).expanduser().resolve()
    if output.is_relative_to(data) or output.is_relative_to(app):
        raise Blocked('unsafe_output', 'Output must be outside the original app and data container.')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with output_lock(output):
        return export_locked(args, data, app, selected, adapter, output)


def export_locked(args, data, app, selected, adapter, output):
    manifests = output / 'kyobo-export-manifest.json'
    existing = json.loads(manifests.read_text()) if manifests.exists() else {'schema': SCHEMA, 'books': {}}
    if existing.get('schema') != SCHEMA or not isinstance(existing.get('books'), dict):
        raise Blocked('manifest_conflict', 'The output manifest is not a recognized tool manifest.')
    fingerprints = {}
    for row in selected:
        root = Path(row['root_path'])
        fingerprints[row['id']] = {str(p.relative_to(data)): sha(p) for p in source_files(root)}
    reusable, pending = [], []
    for row in selected:
        old = existing['books'].get(row['id'], {})
        dest = output / old.get('file', '__missing__')
        if old.get('source_files') == fingerprints[row['id']] and dest.is_file() and dest.resolve().is_relative_to(output) and sha(dest) == old.get('sha256'):
            reusable.append(old)
            emit('reused', book=public_book(row), file=str(dest))
        else:
            pending.append(row)
    if not pending:
        emit('complete', exported=0, reused=len(reusable), output=str(output), manifest=str(manifests))
        return 0
    baseline = {'state': state_manifest(data), 'executable_sha256': sha(app / 'iPadB2C'),
                'book_files': {p: h for row in pending for p, h in fingerprints[row['id']].items()}}
    parent = output / '.kyobo-export'
    if parent.is_symlink():
        raise Blocked('unsafe_work_path', 'The private run directory cannot be a symlink.')
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    run = parent / ('run-' + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    dump(run / 'run.json', {'schema': SCHEMA, 'run_id': run.name, 'app': str(app), 'data': str(data), 'created': datetime.datetime.now().astimezone().isoformat()})
    dump(run / 'baseline.json', baseline)
    emit('plan', books=[public_book(r) for r in pending], output=str(output), run=str(run), network='denied', clock=args.clock,
         changes='disposable app/home only; no original-app launch, keychain writes or system-clock changes')
    successes, failures, cleanup_result = [], [], None
    try:
        jobs, target, home, validator = stage(run, app, data, adapter, pending)
        for row, job in zip(pending, jobs):
            key = row['barcode'] + '-' + row['format']
            work = run / key
            work.mkdir(mode=0o700)
            emit('extracting', book=public_book(row))
            try:
                env = {k: v for k, v in os.environ.items() if not k.startswith(('DYLD_', 'KY_'))}
                env.update(HOME=str(home), CFFIXED_USER_HOME=str(home), TMPDIR=str(home / 'tmp') + '/', KY_HOME=str(home),
                           KY_JOB=str(run / (key + '.job.json')), KY_LOG=str(work / 'native.jsonl'), KY_CLOCK=args.clock,
                           KY_ORIGINAL_DB=str(data / 'Library/Kyobo/data/books.db'), KY_WRITE_SENTINEL=str(HERE / 'kyobo.py'),
                           KY_SAFETY_CLIENT=str(run / 'scratch/SafetyClient.dylib'))
                result = execute(['/usr/bin/sandbox-exec', '-f', str(run / 'scratch/offline.sb'), str(target / 'iPadB2C')], work, 'native', timeout=180, env=env)
                events = [json.loads(s) for s in (work / 'native.jsonl').read_text().splitlines()] if (work / 'native.jsonl').exists() else []
                if result.returncode or not any(e['event'] == 'exported' for e in events):
                    raise Blocked('extraction_failed', 'The native reader did not produce a verified-format candidate.', exit=result.returncode,
                                  events=[e for e in events if e['event'] in ('packager_failed', 'unsupported_size', 'exception', 'epub_resource_failed', 'reader_abi_mismatch')])
                command = lambda argv, label, **kw: checked(argv, work, label, **kw)
                candidate = Path(job['output'])
                if row['format'] == 'pdf':
                    validation = validate_pdf(candidate, validator, work, command)
                else:
                    candidate = run / 'scratch' / (key + '.epub')
                    validation = package_epub(job['output'], candidate, validator, work, command)
                name = filename(row['title'], row['format'])
                if sum(filename(r['title'], r['format']) == name for r in selected) > 1:
                    name = filename(row['title'] + ' [' + row['barcode'] + ']', row['format'])
                destination = output / name
                digest = publish(candidate, destination)
                record = {'id': row['id'], 'title': row['title'], 'file': name, 'format': row['format'], 'sha256': digest,
                          'bytes': destination.stat().st_size, 'source_files': fingerprints[row['id']], 'validation': validation,
                          'historical_process_time': any(e['event'] == 'process_local_historical_time' for e in events)}
                successes.append(record)
                existing['books'][row['id']] = record
                dump(manifests, existing)
                dump(work / 'result.json', record)
                emit('exported', book=public_book(row), file=str(destination), sha256=digest,
                     validation=console_validation(validation), validation_report=str(work / 'result.json'))
            except Exception as e:
                failure = {'book': public_book(row), 'code': getattr(e, 'code', 'processing_error'), 'message': str(e), 'details': getattr(e, 'details', {})}
                failures.append(failure)
                dump(work / 'failure.json', failure)
                emit('book_failed', **failure)
    finally:
        cleanup_result = cleanup(run)
        dump(run / 'result.json', {'exported_ids': [r['id'] for r in successes], 'failures': failures, 'cleanup': cleanup_result})
        emit('cleanup', run=str(run), **cleanup_result)
    emit('complete', exported=len(successes), reused=len(reusable), failures=failures, output=str(output), manifest=str(manifests), run=str(run))
    return 1 if failures or not cleanup_result['ok'] else 0


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'list', 'export'):
        command = sub.add_parser(name)
        command.add_argument('--data', help='Explicit Kyobo sandbox Data directory')
        command.add_argument('--app', help='Explicit installed app or inner iPadB2C.app')
        if name == 'list':
            command.add_argument('--query', default='')
        if name == 'export':
            choice = command.add_mutually_exclusive_group(required=True)
            choice.add_argument('--book', action='append', default=[], help='Exact ID or unique title query; repeat for multiple books')
            choice.add_argument('--all', action='store_true')
            command.add_argument('--format', choices=('pdf', 'epub'))
            command.add_argument('--out', required=True, help='Output directory chosen from the user request or current context')
            command.add_argument('--clock', choices=('current', 'license-window'), default='license-window', help='Process-local treatment of expired cached licenses for permanent purchases')
    clean = sub.add_parser('cleanup')
    clean.add_argument('--run', required=True)
    return p


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        if args.command == 'cleanup':
            result = cleanup(args.run)
            emit('cleanup', **result)
            return 0 if result['ok'] else 1
        if args.command == 'list':
            data = discover_data(args.data)
            rows = read_library(data)
            found = [r for r in rows if not args.query or matches(r, args.query)]
            emit('library', books=[public_book(r) for r in found], unique_titles=len({r['barcode'] for r in found}))
            return 0
        if args.command == 'doctor':
            app, data = discover_app(args.app), discover_data(args.data)
            adapter = preflight(app, data)
            emit('ready', app=str(app), data=str(data), supported_version=adapter['source_app_version'],
                 supported_build=adapter.get('source_app_build'),
                 executable_sha256=sha(app / 'iPadB2C'), legacy_backup_candidates=len(list((data / 'Library/Fasoo/.fasooStore').glob('*.cer.corrupt.bak'))))
            return 0
        return export(args)
    except Blocked as e:
        emit('blocked', code=e.code, message=str(e), **e.details)
        return 2
    except KeyboardInterrupt:
        emit('interrupted', message='Inspect the run cleanup report; use cleanup --run if needed.')
        return 130
    except Exception as e:
        emit('failed', code=type(e).__name__, message=str(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
