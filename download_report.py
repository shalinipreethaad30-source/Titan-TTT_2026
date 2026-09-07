#!/usr/bin/env python3
"""Download Titan's existing Excel export unchanged.

Defaults to local HTTP. For deployment set TITAN_BASE_URL to your HTTPS URL.
For HTTP development, Django must use CSRF_COOKIE_SECURE=False and
SESSION_COOKIE_SECURE=False. This script does not change Django settings.

Install: python -m pip install requests
Enter your own Titan username and password when prompted.
Optional: set TITAN_USERNAME and TITAN_PASSWORD for unattended runs.
To reuse your browser login, set TITAN_SESSIONID to its sessionid cookie value.
Run: python download_report.py
Optional: --from-date YYYY-MM-DD --to-date YYYY-MM-DD --plating-stock-no VALUE
Uses the existing server login, CSRF protection, session and Reports permission.
No credentials/cookies are stored on disk and no Excel is generated locally.
"""
import argparse
import getpass
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
import os
from pathlib import Path
import sys
import zlib
from urllib.parse import urlencode, urljoin, urlsplit
from zipfile import BadZipFile, ZipFile
try:
    import requests
except ImportError:
    raise SystemExit('Missing dependency. Run: python -m pip install requests')

MODULES = (
    'day-planning', 'input-screening', 'brass-qc', 'iqf', 'brass-audit',
    'jig-loading', 'inprocess-inspection', 'jig-unloading-z1', 'jig-unloading-z2',
    'nickel-inspection-z1', 'nickel-inspection-z2', 'nickel-audit-z1',
    'nickel-audit-z2', 'spider-spindle-z1', 'spider-spindle-z2',
)
EXCEL_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class DownloadError(Exception):
    """A user-readable error, without dumping credentials or server HTML."""


class LoginToken(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'csrfmiddlewaretoken':
            self.token = attrs.get('value')


def iso_date(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d')
        if parsed.strftime('%Y-%m-%d') != value:
            raise ValueError
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use a real date in YYYY-MM-DD format.') from exc


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default=os.getenv('TITAN_BASE_URL', 'http://127.0.0.1:8000'),
                        help='Running Django application URL')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--module', choices=MODULES, help='Same values as the UI module selector')
    mode.add_argument('--consolidated', action='store_true', help='Consolidated report (default)')
    parser.add_argument('--from-date', '--date-from', dest='date_from', type=iso_date)
    parser.add_argument('--to-date', '--date-to', dest='date_to', type=iso_date)
    parser.add_argument('--plating-stock-no', '--plating-stk-no', dest='plating_stk_no')
    parser.add_argument('--timeout', type=float, default=120, help='Socket timeout in seconds (default: 120)')
    parser.add_argument('--output-dir', type=Path, default=Path.cwd(), help='Existing directory (default: current directory)')
    args = parser.parse_args(argv)
    parts = urlsplit(args.base_url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        parser.error('--base-url must be an HTTP(S) URL without credentials, query, or fragment.')
    if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('Deployed servers require HTTPS. Supply --base-url https://your-titan-domain.')
    try:
        parts.port
    except ValueError:
        parser.error('--base-url has an invalid port.')
    if not 0 < args.timeout < float('inf'):
        parser.error('--timeout must be a finite positive number.')
    if not args.output_dir.is_dir():
        parser.error('--output-dir must be an existing directory.')
    args.base_url = args.base_url.rstrip('/') + '/'
    return args


def report_url(args):
    if args.module:
        params = {'module': args.module}
        endpoint = 'reports_module/download_report/'
    else:
        params = {key: value for key, value in (
            ('date_from', args.date_from), ('date_to', args.date_to),
            ('plating_stk_no', (args.plating_stk_no or '').strip())) if value}
        endpoint = 'reports_module/consolidated_report/download/'
    return urljoin(args.base_url, endpoint) + ('?' + urlencode(params) if params else '')


AUTH_REQUIRED = 'Authentication required. Please login again.'
ACCESS_DENIED = 'Access denied: Your account does not have permission to access the Reports Module.'
LOGIN_FAILED = 'Login failed: Invalid username or password.'


def check_response(response):
    status = response.status_code
    if status == 401 or 300 <= status < 400:
        raise DownloadError(AUTH_REQUIRED)
    if status == 403:
        raise DownloadError(ACCESS_DENIED)
    if status != 200:
        message = {404: 'No matching report data or endpoint not found. Check filters and --base-url.',
                   429: 'Too many requests. Wait before trying again.'}.get(
                       status, 'Django returned HTTP {}. Check the application logs.'.format(status))
        raise DownloadError(message)


def login(session, args):
    username = os.getenv('TITAN_USERNAME')
    password = os.getenv('TITAN_PASSWORD')
    if sys.stdin.isatty():
        if not username:
            username = input('Titan username: ').strip()
        if not password:
            password = getpass.getpass('Titan password: ')
    if not username or not password:
        raise DownloadError('Username and password are required. Run in a terminal or set TITAN_USERNAME and TITAN_PASSWORD.')
    login_url = urljoin(args.base_url, 'accounts/login/')
    with session.get(login_url, timeout=args.timeout, allow_redirects=False) as response:
        check_response(response)
        token = LoginToken()
        token.feed(response.text)
    if urlsplit(login_url).scheme == 'http' and any(c.secure for c in session.cookies):
        raise DownloadError('Django issued Secure cookies over HTTP. In the Django server terminal set $env:DJANGO_COOKIE_SECURE = "false", then restart python manage.py runserver. For deployment use --base-url with HTTPS.')
    if not token.token:
        raise DownloadError('Login CSRF token missing. Check the login endpoint; browser verification may be required.')
    with session.post(login_url, data={
            'username': username, 'password': password,
            'csrfmiddlewaretoken': token.token,
            'next': urlsplit(report_url(args)).path},
            headers={'Referer': login_url}, timeout=args.timeout,
            allow_redirects=False) as response:
        if response.status_code == 200:
            raise DownloadError(LOGIN_FAILED)
        if response.status_code in (302, 303):
            target = urlsplit(urljoin(login_url, response.headers.get('Location', '')))
            origin = urlsplit(login_url)
            if ((target.scheme, target.netloc) != (origin.scheme, origin.netloc)
                    or target.path.rstrip('/') == origin.path.rstrip('/')):
                raise DownloadError(AUTH_REQUIRED)
            # The protected download validates the resulting session and permissions.
            return
        if response.status_code == 403:
            raise DownloadError('Login rejected: CSRF or additional browser verification required.')
        check_response(response)


def validate_excel(data, content_type):
    if content_type not in (EXCEL_TYPE, 'application/octet-stream'):
        if content_type == 'text/html':
            raise DownloadError('Received HTML instead of Excel: session expired or login/verification is required.')
        raise DownloadError('Expected Excel, received {}. No file saved.'.format(content_type))
    try:
        # Read-only validation of the container; never edit or regenerate it.
        with ZipFile(BytesIO(data)) as archive:
            if not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(archive.namelist()):
                raise DownloadError('Response is not an XLSX workbook. No file saved.')
            if archive.testzip() is not None:
                raise DownloadError('Downloaded workbook is corrupt. No file saved.')
    except (BadZipFile, RuntimeError, EOFError, zlib.error, NotImplementedError) as exc:
        raise DownloadError('Invalid or truncated XLSX response. No file saved.') from exc


def save_download(data, args):
    prefix = args.module.replace('-', '_') + '_Report' if args.module else 'Consolidated_Report'
    stem = '{}_{}'.format(prefix, datetime.now().strftime('%Y%m%d_%H%M%S'))
    for index in range(1000):
        suffix = '' if index == 0 else '_{:02d}'.format(index)
        path = args.output_dir / (stem + suffix + '.xlsx')
        try:
            handle = path.open('xb')  # Never overwrite an existing report.
        except FileExistsError:
            continue
        try:
            with handle:
                handle.write(data)  # Save exactly the bytes delivered by Django.
        except BaseException:
            path.unlink(missing_ok=True)  # Remove only this run's incomplete output.
            raise
        return path.resolve()
    raise DownloadError('Too many matching filenames; choose another output directory.')


def main(argv=None):
    args = arguments(argv)
    if args.module and (args.date_from or args.date_to or args.plating_stk_no):
        print('Note: date/stock filters are ignored for module downloads, matching the UI.', file=sys.stderr)
    try:
        with requests.Session() as session:
            # Use only supplied credentials; never inherit credentials from a .netrc file.
            session.trust_env = False
            session.headers.update({'User-Agent': 'Titan-Report-Downloader/2.0'})
            browser_session = os.getenv('TITAN_SESSIONID', '').strip()
            if browser_session:
                if not browser_session.isascii() or not browser_session.isalnum():
                    raise DownloadError('TITAN_SESSIONID must contain only the Django sessionid cookie value.')
                origin = urlsplit(args.base_url)
                session.cookies.set('sessionid', browser_session, domain=origin.hostname,
                                    path='/', secure=origin.scheme == 'https')
            else:
                login(session, args)
            with session.get(report_url(args), headers={'Accept': EXCEL_TYPE},
                             timeout=args.timeout, allow_redirects=False) as response:
                check_response(response)
                data = response.content
                content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                validate_excel(data, content_type)
            path = save_download(data, args)
        print('Report downloaded successfully: {}'.format(path))
        return 0
    except requests.exceptions.Timeout:
        message = 'Request timed out. Check Django or increase --timeout.'
    except requests.exceptions.ConnectionError:
        message = 'Cannot connect to Django. Check the running server, --base-url, and network/TLS settings.'
    except requests.exceptions.RequestException:
        message = 'Connection interrupted or invalid HTTP response. No report saved.'
    except (DownloadError, OSError, ValueError) as exc:
        message = str(exc)
    except (KeyboardInterrupt, EOFError):
        message = 'Download cancelled.'
    print(message, file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
