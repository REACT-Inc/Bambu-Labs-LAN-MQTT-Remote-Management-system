"""Fetch authenticated printer status into a local, atomically replaced JSON file."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


FIELDS = ('summary', 'detail', 'tooltip')
UNAVAILABLE = dict.fromkeys(FIELDS, 'Printer status unavailable')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a password or session cookie to a redirected service.
        return None


def write_status(path, document):
    if not isinstance(document, dict) or any(not isinstance(document.get(k), str) for k in FIELDS):
        raise ValueError('Invalid status document')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({k: document[k] for k in FIELDS}, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StatusClient:
    def __init__(self, url, password_file):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
            raise ValueError('Use an HTTPS service URL, or HTTP on loopback only.')
        self.url = url.rstrip('/')
        self.password_file = Path(password_file)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())

    def read_status(self):
        with self.opener.open(self.url + '/api/printer-status', timeout=10) as response:
            return json.load(response)

    def fetch(self):
        try:
            return self.read_status()
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        password = self.password_file.read_text(encoding='utf-8').rstrip('\r\n')
        request = urllib.request.Request(self.url + '/api/login',
            data=json.dumps({'password': password}).encode(),
            headers={'Content-Type': 'application/json', 'X-PM': '1'}, method='POST')
        with self.opener.open(request, timeout=10) as response:
            response.read()
        return self.read_status()


def refresh(client, output):
    try:
        write_status(output, client.fetch())
        return True
    except (OSError, ValueError, TypeError):
        # Replace old success text; do not expose URLs, credentials or error bodies.
        write_status(output, UNAVAILABLE)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    try:
        client = StatusClient(args.url, args.password_file)
    except ValueError as exc:
        parser.error(str(exc))
    while True:
        ok = refresh(client, args.output)
        if args.once:
            return 0 if ok else 1
        time.sleep(15)


if __name__ == '__main__':
    raise SystemExit(main())
