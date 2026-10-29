"""Faster queueing and reprinting: a library of the sliced files on the Pi, and reuse of files already on a printer.

- **The library:** each uploaded .3mf is kept once, under its fingerprint, with its plates (uploads/library/). Before
  uploading, the Queue a print dialog sends the file's fingerprint: when the Pi already has that file, it isn't sent
  again (POST /api/upload/check), and **Recent files** queues one without uploading at all. Files are hard links, so
  a job's own copy and the library's are the same data on disk; the library keeps LIBRARY_BYTES at most, dropping the
  least recently used files (a queued job keeps its own copy).
- **Files already on a printer:** when a job starts, its file isn't sent to the printer again if the Pi sent this same
  file (same fingerprint) to the same printer under the same name before, and the printer still has a file of that
  name with that exact size (queueing.upload). Settings → Printers turns it off.

The fingerprint: SHA-256 of the size, the first 64 KiB and the last 64 KiB (the whole file when it's smaller). Two
files that only share a size never match. A .3mf is a zip, whose last bytes list every file inside it with its CRC-32
checksum, so a change anywhere in the file changes the fingerprint. static/app.js computes the same in the browser.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

SAMPLE = 64 * 1024
LIBRARY_BYTES = 5 * 1024 ** 3          # the library's size limit (least recently used files go first)
LIBRARY_DAYS = 180                     # unused this long: dropped
FINGERPRINT = re.compile(r'[0-9a-f]{64}')


def fingerprint(path):
    size = os.path.getsize(path)
    digest = hashlib.sha256(f'{size}\n'.encode())
    with open(path, 'rb') as stream:
        if size <= 2 * SAMPLE:
            digest.update(stream.read())
        else:
            digest.update(stream.read(SAMPLE))
            stream.seek(size - SAMPLE)
            digest.update(stream.read(SAMPLE))
    return digest.hexdigest()


def link(source, target):
    """target as a hard link to source (no copy), or a copy where links aren't possible."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


class FileCache:
    def __init__(self, store, uploads, settings=None, clock=time.time):
        self.db, self.uploads, self.clock = store.db, Path(uploads), clock
        self.settings = settings if settings is not None else {}
        self.folder = self.uploads / 'library'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS file_library (
              fingerprint TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, info TEXT NOT NULL,
              added REAL NOT NULL, used REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS printer_files (
              printer TEXT NOT NULL, remote TEXT NOT NULL, fingerprint TEXT NOT NULL, size INTEGER NOT NULL,
              sent REAL NOT NULL, PRIMARY KEY (printer, remote));
        ''')
        self.db.commit()

    # ---- the library ----

    def path(self, key):
        if not FINGERPRINT.fullmatch(str(key)):
            raise ValueError('Unknown file.')
        return self.folder / f'{key}.3mf'

    def remember(self, asset, name, info):
        """Keep an uploaded file (and what the dialog shows about it) in the library; returns its fingerprint."""
        key = fingerprint(asset)
        if not self.path(key).exists():
            link(asset, self.path(key))
        now = self.clock()
        with self.db:
            self.db.execute('INSERT INTO file_library(fingerprint,name,size,info,added,used,uses) VALUES(?,?,?,?,?,?,1) '
                            'ON CONFLICT(fingerprint) DO UPDATE SET name=excluded.name,info=excluded.info,used=excluded.used,uses=uses+1',
                            (key, str(name)[:200], os.path.getsize(asset), json.dumps(info), now, now))
        self.prune()
        return key

    def entry(self, key):
        row = self.db.execute('SELECT * FROM file_library WHERE fingerprint=?', (str(key),)).fetchone()
        if not row:
            return None
        if not self.path(key).is_file() or self.path(key).stat().st_size != row['size']:
            self.forget(key)   # the file went missing: forget it rather than offer it
            return None
        return dict(row, info=json.loads(row['info']))

    def use(self, key, name=None):
        """A new upload (its own asset) made from a library file, without sending it again: (asset, entry)."""
        entry = self.entry(key)
        if not entry:
            raise ValueError("That file isn't on the Pi any more: upload it again.")
        asset = secrets.token_hex(16)
        target = self.uploads / f'{asset}.3mf'
        link(self.path(key), target)
        name = Path(str(name or entry['name']).replace('\\', '/')).name[:200] or entry['name']
        target.with_suffix('.name').write_text(name)
        with self.db:
            self.db.execute('UPDATE file_library SET used=?,uses=uses+1,name=? WHERE fingerprint=?', (self.clock(), name, key))
        return asset, dict(entry, name=name)

    def recent(self, limit=60):
        out = []
        for row in self.db.execute('SELECT * FROM file_library ORDER BY used DESC LIMIT ?', (limit,)).fetchall():
            info = json.loads(row['info'])
            out.append(dict(fingerprint=row['fingerprint'], name=row['name'], size=row['size'], added=row['added'], used=row['used'],
                            uses=row['uses'], model=info.get('model', ''), plates=[p for p in info.get('plates', []) if p.get('sliced')]))
        return out

    def forget(self, key):
        with self.db:
            self.db.execute('DELETE FROM file_library WHERE fingerprint=?', (str(key),))
        self.path(key).unlink(missing_ok=True)

    def prune(self):
        """Least recently used first, until the library is within LIBRARY_BYTES; and anything unused for LIBRARY_DAYS."""
        rows = self.db.execute('SELECT fingerprint,size,used FROM file_library ORDER BY used DESC').fetchall()
        total, cutoff = 0, self.clock() - LIBRARY_DAYS * 86400
        for row in rows:
            total += row['size']
            if total > LIBRARY_BYTES or row['used'] < cutoff:
                self.forget(row['fingerprint'])

    # ---- files already on a printer ----

    def reuse_enabled(self):
        return self.settings.get('reuse_printer_files', True) is not False

    def on_printer(self, printer, remote, key, size):
        """True when the Pi sent this same file (fingerprint `key`, `size` bytes) to this printer under this name: the
        printer's file of that name, if it still has that exact size, needn't be sent again (queueing.upload checks the
        size on the printer). The fingerprint is worked out in a worker thread first; this only reads the database."""
        if not self.reuse_enabled():
            return False
        row = self.db.execute('SELECT fingerprint,size FROM printer_files WHERE printer=? AND remote=?', (printer, remote)).fetchone()
        return bool(row) and row['size'] == size and row['fingerprint'] == key

    def sent(self, printer, remote, key, size):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO printer_files(printer,remote,fingerprint,size,sent) VALUES(?,?,?,?,?)',
                            (printer, remote, key, size, self.clock()))
