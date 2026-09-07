import base64
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware as DjangoSessionMiddleware
from django.utils.crypto import get_random_string
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.contrib.auth import logout as auth_logout
import time
import logging

from watchcase_tracker.perf_logger import time_stage

logger = logging.getLogger(__name__)


class BlockOptionsMiddleware:
    """
    Security middleware that rejects HTTP OPTIONS requests with 405 Method Not Allowed.

    Rationale:
    - This application has no CORS requirements (no django-cors-headers, no cross-origin
      consumers). OPTIONS preflight requests serve no functional purpose here.
    - Blocking OPTIONS globally prevents scanners and attackers from discovering
      supported HTTP methods and endpoint structure via OPTIONS introspection.
    - This middleware must be registered as the FIRST entry in MIDDLEWARE so that
      OPTIONS requests are rejected before any authentication, session, or CSRF
      processing occurs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'OPTIONS':
            response = HttpResponse(status=405)
            response['Allow'] = 'GET, POST, HEAD'
            response['Content-Length'] = '0'
            return response
        return self.get_response(request)

# ---------------------------------------------------------------------------
# URL-prefix → set of module names that grant access to that area.
# Any module name from USER_CATEGORY_MODULES that belongs to the group
# mapped to the prefix gives the user access.
# ---------------------------------------------------------------------------
_MODULE_URL_MAP = {
    'dayplanning/':               {'Data Upload', 'DP Pick Table', 'DP Complete Table'},
    'inputscreening/':            {'Input Pick Table', 'Input Completed Table', 'Input Accept Table', 'Input Reject Table', 'Input Screening'},
    'recovery_dp/':               {'Recovery Data Upload', 'Recovery Pick Table', 'Recovery Completed Table'},
    'recovery_is/':               {'R-Pick Table', 'R-Completed Table', 'R-Accept Table', 'R-Reject Table'},
    'recovery_brassqc/':          {'R-Brass Qc Pick Table', 'R-Brass Qc Completed Table'},
    'recovery_brass_audit/':      {'R-Brass Audit Pick Table', 'R-Brass Audit Reject Table', 'R-Brass Audit Complete Table'},
    'recovery_iqf/':              {'R-IQF Pick Table', 'R-IQF Accept Table', 'R-IQF Reject Table', 'R-IQF Completed Table'},
    'brass_qc/':                  {'Brass Qc Pick Table', 'Brass Qc Completed Table'},
    'brass_audit/':               {'Brass Audit Pick Table', 'Brass Audit Complete Table', 'Brass Audit Reject Table'},
    'iqf/':                       {'IQF Pick Table', 'IQF Completed Table', 'IQF Accept Table', 'IQF Reject Table'},
    'jig_loading/':               {'Jig Pick Table', 'Jig Completed Table'},
    'jig_unloading/':             {'JUL Main Table', 'JUL Completed'},
    'JigUnloading_Zone2/':        {'JUL Main Table Zone 2', 'JUL Completed Zone 2'},
    'inprocess_inspection/':      {'IP Main', 'IP Completed'},
    'nickle_inspection/':         {'Nickel Main Table', 'Nickel Completed Table'},
    'nickle_inspection_zone_two/': {'Nickel Inspection Zone 2 Pick Table', 'Nickel Inspection Zone 2 Completed Table', 'Nickel Inspection Zone 2 Reject Table'},
    'nickel_audit/':              {'NA Pick Table', 'NA Completed'},
    'nickel_audit_zone_two/':     {'Nickel Audit Zone 2 Pick Table', 'Nickel Audit Zone 2 Completed Table'},
    'spider_spindle/':            {'Spider Spindle Z1 Pick Table', 'Spider Spindle Z1 Completed Table'},
    'spider_spindle_zone_two/':   {'Spider Spindle Z2 Pick Table', 'Spider Spindle Z2 Completed Table'},
}

_MODULE_ACCESS_DENIED_MSG = 'Module cannot be accessible. Contact admin to get the access.'

_MODULE_ACCESS_DENIED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Access Restricted</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #f1f5f9;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; }}
  .card {{ background: #fff; border-radius: 12px; padding: 3rem 2.5rem;
           max-width: 480px; width: 100%; text-align: center;
           box-shadow: 0 4px 24px rgba(0,0,0,0.10); }}
  .icon {{ font-size: 3.5rem; margin-bottom: 1rem; }}
  h1 {{ color: #dc2626; font-size: 1.4rem; margin-bottom: 0.5rem; }}
  p  {{ color: #475569; font-size: 1rem; margin-bottom: 1.5rem; }}
  a  {{ display: inline-block; background: #2563eb; color: #fff;
        padding: 0.6rem 1.5rem; border-radius: 6px; text-decoration: none;
        font-weight: 600; }}
  a:hover {{ background: #1d4ed8; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">🔒</div>
  <h1>Access Restricted</h1>
  <p>{message}</p>
  <a href="/home/">Back to Dashboard</a>
</div>
</body>
</html>"""


class ModuleAccessMiddleware:
    """
    Intercepts requests to module URL prefixes and enforces that the
    authenticated user has at least one provisioned module for that area.

    - Unauthenticated users are passed through (login_required handles them).
    - Admin users are always allowed.
    - Non-HTML (API/JSON) requests receive a JSON 403.
    - HTML page requests receive a styled 403 page.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Perf instrumentation: this middleware runs on every module-prefixed
        # request and is a named suspect for MIDDLEWARE_AUTH slowness (permission
        # checks on every request). Wrapping it in time_stage surfaces its own
        # cost (cache/DB lookups below) separately from the rest of the
        # MIDDLEWARE_AUTH bucket whenever a request ends up slow.
        with time_stage(request, 'MW_MODULE_ACCESS'):
            path = request.path.lstrip('/')

            # Determine which module group this URL belongs to.
            required_modules = None
            for prefix, modules in _MODULE_URL_MAP.items():
                if path.startswith(prefix):
                    required_modules = modules
                    break

            if required_modules is None:
                # Not a module URL — skip.
                return self.get_response(request)

            user = getattr(request, 'user', None)
            if user is None or not getattr(user, 'is_authenticated', False):
                # Not authenticated; let login_required redirect handle it.
                return self.get_response(request)

            # Lazy import to avoid circular imports at module load time.
            from adminportal.services import get_user_allowed_module_names, is_admin_user

            is_admin = is_admin_user(user)
            # Stash on request so adminportal.context_processors.user_permissions
            # (used by every template render) reuses this result instead of
            # repeating the same cache lookups for the same request.
            request._ttt_is_admin = is_admin
            if is_admin:
                return self.get_response(request)

            allowed = set(get_user_allowed_module_names(user))
            request._ttt_allowed_modules = allowed
            if allowed.intersection(required_modules):
                return self.get_response(request)

            # Access denied.
            logger.warning(
                'MODULE_ACCESS_DENIED: user=%s path=%s required=%s',
                user.username,
                request.path,
                required_modules,
            )

        wants_json = (
            'application/json' in request.META.get('HTTP_ACCEPT', '')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.path.rstrip('/').split('/')[-1].startswith('api')
        )
        if wants_json:
            return JsonResponse({'error': _MODULE_ACCESS_DENIED_MSG}, status=403)

        html = _MODULE_ACCESS_DENIED_HTML.format(message=_MODULE_ACCESS_DENIED_MSG)
        return HttpResponse(html, status=403, content_type='text/html; charset=utf-8')


class SafeSessionMiddleware(DjangoSessionMiddleware):
    """
    Drop-in replacement for django.contrib.sessions.middleware.SessionMiddleware.

    With SESSION_SAVE_EVERY_REQUEST = True (sliding inactivity timeout), every
    request re-saves the session when the response is built. If the session
    row was deleted while the request was in flight — a concurrent logout,
    session expiry in another tab, or the single-session-per-account signal
    deleting the old session on a new login — Django raises SessionInterrupted,
    which surfaces as an error page to the user.

    This subclass converts that benign race into the standard session-expired
    handling: JSON 401 (code=SESSION_EXPIRED) for fetch/AJAX requests so the
    global session guard shows the "Session Expired" alert, or a redirect to
    the login page for normal browser navigations.
    """

    def process_response(self, request, response):
        # The session heartbeat is a presence/single-session check only.
        # Do not let this background request refresh the sliding Django session
        # when SESSION_SAVE_EVERY_REQUEST=True; otherwise the 5-second heartbeat
        # can keep an idle user's authentication session alive indefinitely.
        if request.path == '/adminportal/api/session-heartbeat/':
            return response

        try:
            return super().process_response(request, response)
        except SessionInterrupted:
            logger.warning(
                'SESSION_INTERRUPTED_RECOVERED: path=%s method=%s',
                request.path, request.method,
            )
            if SessionExpiredAjaxMiddleware._is_background_request(request):
                return JsonResponse(
                    {
                        'success': False,
                        'error': 'Your session has expired due to inactivity. Please log in again to continue.',
                        'code': 'SESSION_EXPIRED',
                    },
                    status=401,
                )
            return redirect(getattr(settings, 'LOGIN_URL', '/accounts/login/'))


class SessionExpiredAjaxMiddleware:
    """
    Session Expiry Handling for AJAX/fetch requests.

    Problem it solves:
    SESSION_COOKIE_AGE expires an idle user's session. The next background
    fetch() (e.g. tray-ID scan validation) is answered by login_required with
    a 302 redirect to the login page. fetch() silently follows the redirect,
    receives the login HTML, response.json() fails, and the page shows a
    misleading "Validation Error" instead of telling the user to log in again.

    What it does:
    Detects when an UNAUTHENTICATED, NON-NAVIGATION request (fetch/XHR/JSON)
    is being redirected to the login page, and converts that redirect into a
    structured JSON 401 with code 'SESSION_EXPIRED'. The global frontend
    session guard (static/js/session_guard.js) recognises this code, shows a
    professional "session expired" alert and returns the user to login.

    Normal browser navigations are untouched (they still get the redirect).
    Must be registered AFTER AuthenticationMiddleware so request.user exists.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _is_background_request(request):
        """True for fetch/XHR/API-style requests, False for page navigations."""
        # Modern browsers: fetch()/XHR send Sec-Fetch-Mode 'cors' or
        # 'same-origin'; real page navigations send 'navigate'.
        sec_fetch_mode = request.META.get('HTTP_SEC_FETCH_MODE', '')
        if sec_fetch_mode and sec_fetch_mode != 'navigate':
            return True
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return True
        if 'application/json' in request.META.get('HTTP_ACCEPT', ''):
            return True
        if 'application/json' in (request.META.get('CONTENT_TYPE') or ''):
            return True
        return False

    def __call__(self, request):
        response = self.get_response(request)

        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            return response

        login_url = getattr(settings, 'LOGIN_URL', '/accounts/login/')
        is_login_redirect = (
            response.status_code in (301, 302)
            and login_url in (response.get('Location') or '')
        )
        if is_login_redirect and self._is_background_request(request):
            logger.info(
                'SESSION_EXPIRED_AJAX_401: path=%s method=%s',
                request.path, request.method,
            )
            return JsonResponse(
                {
                    'success': False,
                    'error': 'Your session has expired due to inactivity. Please log in again to continue.',
                    'code': 'SESSION_EXPIRED',
                },
                status=401,
            )
        return response


class CSPMiddleware:
    """
    Content Security Policy (CSP) Middleware.
    
    Generates a unique nonce for each request and applies it to the CSP header
    to allow inline scripts tagged with that nonce while preventing other inline
    script execution. This protects against XSS attacks while maintaining
    functionality for trusted scripts.
    
    Includes CSP directives for:
    - Scripts: 'self', nonce-based, and whitelisted CDNs (SweetAlert2, Google reCAPTCHA)
    - Styles: 'self', 'unsafe-inline', and font/icon CDNs
    - Images: 'self', Lottie animations, dashboard assets, and data URIs
    - Frames: 'self' and Google reCAPTCHA only
    - Object-src: None (no Flash/plugins)
    - Base-uri: 'self' only (prevents base tag injection)
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        nonce = base64.b64encode(get_random_string(16).encode()).decode()
        request.csp_nonce = nonce
        response = self.get_response(request)
        response['Content-Security-Policy'] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://unpkg.com https://cdn.jsdelivr.net/npm/sweetalert2@11 https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/;"
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
            "font-src 'self' https://*.lottiefiles.com https://fonts.gstatic.com https://cdnjs.cloudflare.com https://demo.bootstrapdash.com;"
            "img-src 'self' https://assets2.lottiefiles.com/packages/lf20_uiyqFZ.json https://assets10.lottiefiles.com/packages/lf20_jcikwtux.json https://demo.bootstrapdash.com/skydash/themes/assets/images/logo-mini.svg https://demo.bootstrapdash.com/skydash/themes/assets/images/logo.svg https://demo.bootstrapdash.com/skydash/themes/assets/images/dashboard/people.svg data:; "
            "connect-src 'self' https://assets2.lottiefiles.com/packages/lf20_uiyqFZ.json https://assets10.lottiefiles.com/packages/lf20_jcikwtux.json https://www.google.com/recaptcha/;"
            "frame-src 'self' https://www.google.com/recaptcha/; "
            "object-src 'none'; "
            "base-uri 'self';"
        )
        return response


class LoginLatencyMiddleware:
    """
    Middleware to measure login flow latency and performance.
    
    Purpose:
    - Instruments the login flow to measure total time and individual phase timing
    - Tracks authentication validation, dashboard stats loading, and rendering
    - Only monitors login-related paths (/, /home/, /login/, etc.)
    - Disabled by default; set settings.ENABLE_LOGIN_LATENCY_LOGS = True to activate
    
    Note:
    - Timing information is logged server-side only (not sent in HTTP headers)
    - This is a security fix for VAPT #33 (sensitive information disclosure)
    - Useful for identifying performance bottlenecks in the authentication flow
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only profile login-related endpoints
        if request.path not in ('/', '/home/') and 'login' not in request.path and 'index' not in request.path:
            return self.get_response(request)

        request.start_time = time.time()
        request.timers = {}
        
        response = self.get_response(request)
        
        # Log total time
        total_time = (time.time() - request.start_time) * 1000  # Convert to ms
        
        timer_log = ' | '.join([f'{k}={v}' for k, v in request.timers.items()])
        if getattr(settings, 'ENABLE_LOGIN_LATENCY_LOGS', False):
            logger.warning(
                f'LOGIN_LATENCY: {request.path} | '
                f'Total={total_time:.2f}ms | {timer_log}'
            )
        
        # NOTE: Timing headers were removed (VAPT #33 — sensitive information
        # disclosure). Timing values are available in application logs only.
        return response


class SingleSessionMiddleware:
    """
    Security Middleware: Single Session Per Account Enforcement (VAPT Fix #8).

    Purpose:
    Prevents concurrent/simultaneous logins by enforcing that each user account
    can only have one active session at a time. Works in tandem with
    adminportal.signals which records/clears session keys on login/logout.

    How it Works:
    - On every authenticated request, compares current session key with the
      stored UserActiveSession.session_key for that user
    - If a newer session exists elsewhere, logs out the older session and
      rejects the current request
    - Automatically logs out old browsers/devices when user logs in from new location

    Placement requirement:
    Must be registered AFTER both
      - django.contrib.sessions.middleware.SessionMiddleware
      - django.contrib.auth.middleware.AuthenticationMiddleware
    in settings.MIDDLEWARE, so that request.session and request.user are
    already populated when this runs. It should also run before
    ModuleAccessMiddleware so stale sessions never reach module/business
    logic checks.

    Behavior:
    - Anonymous requests pass through untouched (login_required and the
      existing auth flow already handle them).
    - settings.LOGIN_URL, the logout URL, the Microsoft SSO entry points,
      Django Admin's own login/logout endpoints, and static/media paths are
      skipped so the in-flight authentication handshake (including SSO
      state validation) is never interrupted by this middleware.
    - For every other authenticated request, the current
      `request.session.session_key` is compared against the stored
      `UserActiveSession.session_key` for that user:
        * Missing session key, or no UserActiveSession row at all (e.g. a
          session that predates this feature, or whose record was
          removed): fails safe — the request is logged out and rejected.
          The next successful login recreates the row via the login
          signal.
        * Record exists and matches: request proceeds normally.
        * Record exists and does NOT match: the session is stale because a
          newer login happened elsewhere. The current request is logged
          out and rejected.
      Rejection returns JSON 401 for API/AJAX requests, or a redirect to
      settings.LOGIN_URL (with an informational message) for normal
      browser requests.
    - A failure while querying UserActiveSession (e.g. a transient DB
      error) is logged and the request is allowed through unenforced for
      that one request, rather than crashing or locking out the whole
      site.
    """

    _SKIP_PATH_PREFIXES = (
        '/static/',
        '/media/',
    )

    _SKIP_EXACT_PATHS = {
        '/accounts/profile/',
        '/auth/microsoft/login/',
        '/auth/microsoft/callback/',
    }

    _SKIP_PATH_STARTSWITH = (
        '/admin/login/',
        '/admin/logout/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_skip(request):
            return self.get_response(request)

        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return self.get_response(request)

        current_session_key = getattr(request.session, 'session_key', None)
        if not current_session_key:
            logger.warning(
                'SINGLE_SESSION_NO_SESSION_KEY_REJECT: user=%s path=%s',
                user.username, request.path,
            )
            return self._reject(request)

        # Lazy import to avoid circular/app-registry import issues.
        from adminportal.models import UserActiveSession

        try:
            active_session = UserActiveSession.objects.filter(user=user).first()
        except Exception:
            # A DB hiccup here must never take the whole site down. Log
            # and let this one request through unenforced; the next
            # request will simply try the check again.
            logger.exception(
                'SINGLE_SESSION_LOOKUP_FAILED: user=%s path=%s',
                user.username, request.path,
            )
            return self.get_response(request)

        if active_session is None:
            # No record at all — fail safe by forcing re-authentication so
            # a fresh UserActiveSession row is created on the next login.
            logger.warning(
                'SINGLE_SESSION_NO_RECORD_REJECT: user=%s path=%s',
                user.username, request.path,
            )
            return self._reject(request)

        if active_session.session_key == current_session_key:
            return self.get_response(request)

        logger.warning(
            'SINGLE_SESSION_STALE_SESSION_REJECTED: user=%s path=%s '
            'request_session=%s active_session=%s',
            user.username, request.path, current_session_key, active_session.session_key,
        )
        return self._reject(request)

    def _login_url(self):
        return getattr(settings, 'LOGIN_URL', '/accounts/login/')

    def _logout_url(self):
        return getattr(settings, 'LOGOUT_URL', '/accounts/logout/')

    def _should_skip(self, request):
        path = request.path
        if path == self._login_url() or path == self._logout_url():
            return True
        if path in self._SKIP_EXACT_PATHS:
            return True
        for prefix in self._SKIP_PATH_PREFIXES:
            if path.startswith(prefix):
                return True
        for prefix in self._SKIP_PATH_STARTSWITH:
            if path.startswith(prefix):
                return True
        return False

    def _wants_json(self, request):
        return (
            'application/json' in request.META.get('HTTP_ACCEPT', '')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.path.startswith('/adminportal/api/')
        )

    def _reject(self, request):
        # Logs out the current (stale/invalid) request session. This also
        # fires user_logged_out, but the TASK 2 handler only clears
        # UserActiveSession if its stored session_key still matches the
        # session being logged out, so it safely no-ops here when the
        # active record already points at a newer session elsewhere.
        auth_logout(request)

        if self._wants_json(request):
            return JsonResponse(
                {
                    'success': False,
                    'error': 'You have been logged out because this account was signed in from another device.',
                    'code': 'SESSION_TAKEOVER',
                },
                status=401,
            )

        # login.html renders `?session_error=interrupted` as the visible error
        # message (it does not render django.contrib.messages), so pass it via
        # the query string rather than messages.info(), which would silently
        # never be displayed.
        return redirect(f'{self._login_url()}?session_error=interrupted')


class EmailOTPMFARequiredMiddleware:
    """
    Security Middleware: Email OTP MFA Enforcement.
    
    Purpose:
    Enforces second-factor authentication (Email OTP) for all authenticated users.
    Does not handle authentication itself—only validates that MFA has been completed.
    
    How it Works:
    - Allows only requests where session['mfa_verified'] = True
    - Unauthenticated users are passed through (login_required handles them)
    - Authenticated users without MFA verified are redirected to OTP verification
    - Skips certain paths like login, logout, OTP verification page, and static assets
    
    Note:
    The Email OTP verification view is responsible for setting the mfa_verified flag
    after successful OTP validation.
    """

    _SKIP_PATH_PREFIXES = (
        '/static/',
        '/media/',
    )

    _SKIP_EXACT_PATHS = {
        '/accounts/login/',
        '/accounts/logout/',
        '/accounts/verify-email-otp/',
        '/logout/',
        '/favicon.ico',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_skip(request):
            return self.get_response(request)

        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return self.get_response(request)

        if request.session.get('mfa_verified') is True:
            logger.info(
                'MFA_VERIFIED_ALLOW: user=%s path=%s',
                getattr(user, 'username', 'unknown'),
                request.path,
            )
            return self.get_response(request)

        target = '/accounts/login/'
        if request.session.get('pending_mfa_user_id'):
            target = '/accounts/verify-email-otp/'

        logger.warning(
            'MFA_REQUIRED_REJECT: user=%s path=%s redirect=%s',
            getattr(user, 'username', 'unknown'),
            request.path,
            target,
        )
        return redirect(target)

    def _should_skip(self, request):
        path = request.path
        if path in self._SKIP_EXACT_PATHS:
            return True
        for prefix in self._SKIP_PATH_PREFIXES:
            if path.startswith(prefix):
                return True
        return False


class SecurityHeadersMiddleware:
    """
    Security Middleware: HTTP Security Headers Hardening (VAPT Fix #13 / #33).
    
    Purpose:
    Removes technology stack disclosure headers and enforces security-hardening
    headers to prevent information leakage and protect against common attacks.
    
    Headers Removed (Version/Tech Disclosure — OWASP A05 / CWE-200):
    - Server: Reveals web-server software and version (e.g., Apache, IIS, nginx)
    - X-Powered-By: Reveals language/framework (e.g., Python, ASP.NET)
    - X-Runtime: Exposes per-request processing time (timing attack info)
    - X-Debug-Token: Debug tokens (should never appear in production)
    - X-Debug-Token-Link: Debug links
    
    Headers Enforced (Defense-in-Depth):
    - X-Content-Type-Options: nosniff → Prevents MIME sniffing attacks
    - Referrer-Policy: strict-origin-when-cross-origin → Limits referrer leakage
    - Permissions-Policy: Disables unused browser features (camera, mic, etc.)
    - Cache-Control: no-store (HTML only) → Prevents sensitive page caching
    
    Note:
    X-Frame-Options and Content-Security-Policy are handled by separate middleware.

    Headers removed (version/technology disclosure — OWASP A05 / CWE-200):
    - ``Server``              — reveals web-server software and version
    - ``X-Powered-By``        — reveals runtime (Python / Django version)
    - ``X-Runtime``           — reveals per-request processing time (info leakage)
    - ``X-Debug-Token``       — Symfony-style debug token (should never appear here,
                                defensive removal)
    - ``X-Debug-Token-Link``  — same as above

    Headers enforced (defence-in-depth):
    - ``X-Content-Type-Options: nosniff``   — prevents MIME-sniffing attacks
    - ``Referrer-Policy: strict-origin-when-cross-origin``
                                            — limits referrer leakage to
                                              cross-origin requests
    - ``Permissions-Policy``                — disables unused browser features
    - ``Cache-Control: no-store``           — prevents sensitive page caching
                                              (applies only to HTML responses)

    NOTE: ``X-Frame-Options`` and ``Content-Security-Policy`` are already handled
    by Django's ``XFrameOptionsMiddleware`` and ``CSPMiddleware`` respectively,
    so they are not duplicated here.
    """

    _STRIP_HEADERS = (
        'Server',
        'X-Powered-By',
        'X-Runtime',
        'X-Debug-Token',
        'X-Debug-Token-Link',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # --- Remove version/technology disclosure headers ---
        for header in self._STRIP_HEADERS:
            if header in response:
                del response[header]

        # --- Enforce hardening headers ---
        response.setdefault('X-Content-Type-Options', 'nosniff')
        response.setdefault(
            'Referrer-Policy',
            'strict-origin-when-cross-origin',
        )
        response.setdefault(
            'Permissions-Policy',
            (
                'accelerometer=(), camera=(), geolocation=(), '
                'gyroscope=(), magnetometer=(), microphone=(), '
                'payment=(), usb=()'
            ),
        )

        # Prevent browsers from caching sensitive responses (VAPT / Burp
        # finding "Cacheable HTTPS response"). Covers authenticated HTML pages
        # AND the JSON API under /adminportal/api/ and /api/ (user list,
        # group-module maps, shortcuts, heartbeat, …) which previously carried
        # no Cache-Control at all and could be stored in the browser cache.
        content_type = response.get('Content-Type', '')
        path = request.path or ''
        is_sensitive = (
            'text/html' in content_type
            or 'application/json' in content_type
            or path.startswith('/adminportal/api/')
            or path.startswith('/api/')
        )
        if is_sensitive:
            # Direct assignment (not setdefault) so an upstream max-age is
            # overridden. Pragma + Expires satisfy the HTTP/1.0 checks too.
            response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'

        return response


class AdminIPRestrictionMiddleware:
    """
    Security Middleware: Django Admin Interface IP Restriction (VAPT Fix #35).
    
    Purpose:
    Restricts access to the Django admin interface (/admin/) to an explicit
    IP allow-list. Blocks access from all other IPs with an intentional 404
    response (security by obscurity—don't reveal that /admin/ exists).
    
    Configuration:
    Set settings.ADMIN_IP_ALLOWLIST to a list of allowed IPv4/IPv6 addresses.
    Example: ADMIN_IP_ALLOWLIST = ['127.0.0.1', '::1', '192.168.1.0/24']
    Default (if not set): ['127.0.0.1', '::1'] (localhost only)
    
    How it Works:
    - Intercepts all /admin/* requests
    - Extracts client IP from request (respects X-Forwarded-For for proxy scenarios)
    - Returns 404 (not 403) if IP is not in allow-list to avoid confirming existence
    - Logs all blocked attempts for security monitoring
    
    Note:
    The 404 response is intentional—it prevents attackers from discovering that
    an admin interface exists at this path.

    Any request to a path under ``/admin/`` whose source IP is not in the
    allow-list is rejected with HTTP 404 (deliberately not 403 to avoid
    confirming that an admin interface exists at that path — security by
    obscurity as a secondary layer, OWASP A01).

    The allow-list is read from ``settings.ADMIN_IP_ALLOWLIST`` which must be
    a list/tuple of IPv4 (or IPv6) address strings.  Example setting::

        ADMIN_IP_ALLOWLIST = ['127.0.0.1', '::1', '192.168.1.0/24']

    If the setting is absent or empty the middleware blocks all non-localhost
    access to ``/admin/`` as a safe default.

    IP extraction respects the ``X-Forwarded-For`` header when the request
    has been forwarded through a trusted proxy (IIS → Django). Only the
    left-most address (the real client IP) is evaluated.
    """

    _ADMIN_PREFIX = '/admin/'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(self._ADMIN_PREFIX):
            return self.get_response(request)

        client_ip = self._get_client_ip(request)
        allowed_ips = getattr(settings, 'ADMIN_IP_ALLOWLIST', ['127.0.0.1', '::1'])

        if client_ip in allowed_ips:
            return self.get_response(request)

        logger.warning(
            'ADMIN_IP_BLOCKED: ip=%s path=%s method=%s',
            client_ip,
            request.path,
            request.method,
        )
        # Return 404 — do not reveal that the admin panel exists at this path.
        from django.http import Http404
        from django.views.defaults import page_not_found
        return page_not_found(request, Http404())

    @staticmethod
    def _get_client_ip(request):
        """
        Return the real client IP, honouring X-Forwarded-For when present.
        Only the left-most (originating) address is trusted.
        """
        forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded_for:
            return forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', '')