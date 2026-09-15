from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from modelmasterapp.models import *
from modelmasterapp.tray_code_mapping import TRAY_CODE_CHOICES, NORMAL_TRAY_CODES, JUMBO_TRAY_CODES
from modelmasterapp.image_utils import (
    build_model_image_key_from_filename,
    build_model_image_keys_from_stock,
    detect_image_type,
    get_global_no_image,
    get_image_url,
    get_model_image_lookup_name,
    get_model_view_images,
    get_no_image,
    is_no_image_model_image,
    sort_images_front_first,
)
from rest_framework import status
from rest_framework.renderers import TemplateHTMLRenderer, JSONRenderer
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone  # Added timezone import
from django.conf import settings
import json
import re
import csv
import io
import logging
import hashlib
import time as perf_time
from decimal import Decimal, InvalidOperation
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited
from .forms import AdaptiveCaptchaAuthenticationForm
from .serializers import *
from .utils import extract_table_headings_from_html
from .decorators import require_admin, IsAdminPermission
import datetime
from InputScreening import *
from django.db import transaction

logger = logging.getLogger(__name__)
from django.db.models import Sum, Q
from django.db.models.functions import Cast
from django.db.models import IntegerField
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from adminportal.models import *
from .models import *
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import os
from django.contrib.auth.models import Group
from django.http import JsonResponse
from django.contrib.auth import authenticate, get_user_model, login
from django.views.decorators.csrf import csrf_exempt
import json
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.templatetags.static import static
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.html import escape
from django.views.decorators.http import require_GET, require_POST
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Module, UserModuleProvision
from Recovery_DP.models import *
from Jig_Loading.models import JigLoadingMaster


def _perf_username(username):
    normalized_username = str(username or '').strip().lower()
    if not normalized_username:
        return ''
    return hashlib.sha256(normalized_username.encode('utf-8')).hexdigest()[:16]


def _emit_auth_event(request, event_name, level, message, details=None):
    security_logger = logging.getLogger('security.auth')
    log_level = getattr(logging, str(level or 'INFO').upper(), logging.INFO)
    security_logger.log(
        log_level,
        '%s: %s path=%s details=%s',
        event_name,
        message,
        getattr(request, 'path', ''),
        details or {},
    )


def image_perf_counter():
    return perf_time.perf_counter()


def image_duration_ms(started_at):
    return round((perf_time.perf_counter() - started_at) * 1000, 3)


def hash_value(value, prefix='value'):
    normalized_value = str(value or '').strip().upper()
    if not normalized_value:
        return ''
    digest = hashlib.sha256(normalized_value.encode('utf-8')).hexdigest()[:16]
    return f'{prefix}:{digest}'


def emit_image_event(request, event_name, level, message, details=None):
    log_level = getattr(logging, str(level or 'INFO').upper(), logging.INFO)
    logger.log(
        log_level,
        '%s: %s path=%s details=%s',
        event_name,
        message,
        getattr(request, 'path', ''),
        details or {},
    )


def emit_lookup_start(request, lookup_source, **details):
    emit_image_event(
        request,
        'IMAGE.LOOKUP.START',
        'DEBUG',
        'Image lookup started',
        {'lookup_source': lookup_source, **details},
    )


def emit_lookup_not_found(request, lookup_source, duration_ms, reason, details=None):
    emit_image_event(
        request,
        'IMAGE.LOOKUP.NOT_FOUND',
        'INFO',
        'Image lookup returned no result',
        {
            'lookup_source': lookup_source,
            'duration_ms': duration_ms,
            'reason': reason,
            **(details or {}),
        },
    )


def emit_media_read(request, file_field, lookup_source=''):
    if not file_field:
        return
    emit_image_event(
        request,
        'IMAGE.MEDIA.READ',
        'DEBUG',
        'Media file referenced',
        {
            'lookup_source': lookup_source,
            'file_name': getattr(file_field, 'name', ''),
        },
    )


def emit_media_write(request, file_field, duration_ms, result='saved'):
    if not file_field:
        return
    emit_image_event(
        request,
        'IMAGE.MEDIA.WRITE',
        'INFO',
        'Media file written',
        {
            'lookup_source': 'model_image_upload',
            'duration_ms': duration_ms,
            'result': result,
            'file_name': getattr(file_field, 'name', ''),
            'content_type': getattr(file_field, 'content_type', ''),
            'size': getattr(file_field, 'size', None),
        },
    )


def emit_media_delete(request, file_field, duration_ms, result='file_deleted'):
    if not file_field:
        return
    emit_image_event(
        request,
        'IMAGE.MEDIA.DELETE',
        'INFO',
        'Media file deleted',
        {
            'lookup_source': 'model_image_delete',
            'duration_ms': duration_ms,
            'result': result,
            'file_name': getattr(file_field, 'name', ''),
        },
    )


def emit_image_error(request, lookup_source, error):
    emit_image_event(
        request,
        'IMAGE.ERROR',
        'ERROR',
        'Image operation failed',
        {
            'lookup_source': lookup_source,
            'error_type': error.__class__.__name__,
            'error': str(error),
        },
    )


def _only_model_view_images(images_payload):
    return [
        image
        for image in images_payload
        if image.get('view_code') in _ALLOWED_MODEL_IMAGE_VIEWS
    ]


def _build_placeholder_image_payload(stock_no, request=None, metadata_model=None):
    placeholder = _get_no_image_placeholder()
    placeholder_url = ''
    placeholder_image = None

    if placeholder:
        image_url = get_image_url(placeholder)
        if image_url:
            placeholder_url = image_url
            placeholder_image = {
                'id': placeholder.id,
                'url': placeholder_url,
                'view_code': 'NO_IMAGE',
                'view': 'No Image',
                'is_placeholder': True,
            }

    return {
        'found': False,
        'stock_no': stock_no,
        'raw_stock_no': stock_no,
        'model_no': getattr(metadata_model, 'model_no', '') or '',
        'version': '',
        'bath_type': '',
        'ep_bath_type': getattr(metadata_model, 'ep_bath_type', '') or '',
        'images': [placeholder_image] if placeholder_image else [],
        'preview_image': placeholder_url,
        'preview_view': 'No Image',
        'visual_aid_url': '/adminportal/dp_visualaid/?plating_stk_no=' + str(stock_no or ''),
    }


def get_allowed_modules_for_user(user):
    """
    Get list of module names accessible by user.
    Optimized with caching per user for fast repeated calls.
    Cache key includes user ID to prevent cross-user data leakage.
    """
    from .services import get_user_allowed_module_names
    return get_user_allowed_module_names(user)


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class ShortcutConfigurationAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request, format=None):
        import time as _time
        from .services import get_active_shortcut_configurations
        t0 = _time.time()
        shortcuts = get_active_shortcut_configurations()
        elapsed_ms = (_time.time() - t0) * 1000
        logger.debug(
            'shortcuts API: %.2fms count=%d user=%s',
            elapsed_ms, len(shortcuts), request.user.username,
        )
        return Response({
            'success': True,
            'shortcuts': shortcuts,
        })


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class SessionHeartbeatAPIView(APIView):
    """
    Called periodically by session_guard.js while a tab is genuinely open.
    Refreshes UserActiveSession.updated_at so the single-session-per-account
    check in services.get_active_session_conflict_message can tell a closed
    tab/browser apart from one that is still open, instead of waiting out
    the full idle session timeout.
    """
    renderer_classes = [JSONRenderer]

    def post(self, request, format=None):
        from django.utils import timezone
        from .models import UserActiveSession

        session_key = getattr(request.session, 'session_key', None)
        if session_key:
            UserActiveSession.objects.filter(
                user=request.user, session_key=session_key,
            ).update(updated_at=timezone.now())

        return Response({'success': True})


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class NetworkPingAPIView(APIView):
    """
    Lightweight liveness check polled by the network-status banner in
    base.html (static/templates/base.html) to detect a slow/unstable
    connection. Intentionally does no DB work - only round-trip time matters.
    """
    renderer_classes = [JSONRenderer]

    def get(self, request, format=None):
        return Response({'success': True})


# -----------------------------------------------------------------------------
# TimedLoginView
# - Extends Django's auth LoginView with per-phase timing so we can see exactly
#   what part of the POST is slow (form validation vs authenticate vs session
#   save vs redirect).
# - Keeps login lightweight. Dashboard stats are loaded separately by the
#   dashboard API after /home/ is rendered, never during login POST.
# - Routed via watchcase_tracker/urls.py; settings.py is not modified here.
# -----------------------------------------------------------------------------
class TimedLoginView(__import__('django.contrib.auth.views', fromlist=['LoginView']).LoginView):
    template_name = 'login.html'
    authentication_form = AdaptiveCaptchaAuthenticationForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.conf import settings
        from .services import is_recaptcha_configured, should_require_login_captcha

        form = context.get('form')
        username = ''
        if form is not None and getattr(form, 'data', None):
            username = form.data.get('username', '').strip()
        elif self.request.method == 'POST':
            username = self.request.POST.get('username', '').strip()

        context['enable_microsoft_login'] = getattr(settings, 'ENABLE_MICROSOFT_LOGIN', False)
        # One-shot flag set by the SSO callback when the Microsoft account has
        # no matching user in User Management.
        context['sso_access_denied'] = self.request.session.pop('sso_access_denied', False)
        if self.request.GET.get('otp_error') == 'expired':
            context['error'] = 'Verification code expired.'
        if self.request.GET.get('session_error') == 'interrupted':
            context['error'] = 'Session Interrupted - please relogin to proceed.'
        # Surfaced by the SSO routes when Microsoft sign-in cannot start/finish,
        # so the failure is visible instead of silently returning to this page.
        sso_error = self.request.GET.get('sso_error')
        if sso_error:
            context['error'] = {
                'unavailable': 'Microsoft sign-in is temporarily unavailable. Please contact the administrator.',
                'not_configured': 'Microsoft sign-in is not configured. Please contact the administrator.',
            }.get(sso_error, 'Microsoft sign-in failed. Please try again or contact the administrator.')
        context['show_captcha'] = bool(
            getattr(form, 'require_captcha', False)
            or should_require_login_captcha(username)
        )
        context['captcha_configured'] = is_recaptcha_configured()
        return context

    def get_form_kwargs(self):
        import time as _time
        t0 = _time.time()
        kwargs = super().get_form_kwargs()
        from .services import should_require_login_captcha

        username = ''
        data = kwargs.get('data')
        if data:
            username = data.get('username', '').strip()
        t1 = _time.time()
        kwargs['require_captcha'] = should_require_login_captcha(username)
        if getattr(settings, 'ENABLE_LOGIN_LATENCY_LOGS', False):
            logger.warning(
                'LOGIN_FORM_KWARGS_TIMING: super_get_form_kwargs=%.2fms | should_require_login_captcha=%.2fms',
                (t1 - t0) * 1000, (_time.time() - t1) * 1000,
            )
        return kwargs

    def _login_error_message(self, form, username):
        captcha_error = getattr(form, 'get_captcha_error_message', lambda: None)()
        if captcha_error:
            return captcha_error

        from .services import get_account_lock_message

        return (
            get_account_lock_message(username)
            or 'Invalid username or password. Please try again.'
        )


    def form_valid(self, form):
        # Let Django perform the normal login
        response = super().form_valid(form)

        # Skip OTP verification
        self.request.session["mfa_verified"] = True
        self.request.session.modified = True

        return response

    def _refresh_captcha_context_after_failed_login(self, response, username):
        from .services import is_recaptcha_configured, should_require_login_captcha

        if not hasattr(response, 'context_data'):
            return

        captcha_required = should_require_login_captcha(username)
        if captcha_required:
            response.context_data['form'] = self.get_form_class()(
                self.request,
                initial={'username': username},
                require_captcha=True,
            )
        response.context_data['show_captcha'] = captcha_required
        response.context_data['captcha_configured'] = is_recaptcha_configured()

    # Rate limiting (CWE-307): cap login POSTs per source IP regardless of
    # which username is being tried. block=False so we can return our own
    # friendly HTML error instead of django-ratelimit's default 403 page;
    # the actual 429 response is built below when request.limited is True.
    # This is in addition to, not a replacement for, the existing
    # AccountLockoutBackend (5 failed attempts -> account lock).
    @method_decorator(ratelimit(key='ip', rate='10/m', method='POST', block=False))
    def post(self, request, *args, **kwargs):
        from django.conf import settings
        import time as _time
        login_start = perf_time.perf_counter()
        username = request.POST.get('username', '').strip()

        # request.limited is set by the ratelimit decorator above.
        if getattr(request, 'limited', False):
            security_logger = logging.getLogger('security.auth')
            security_logger.warning(
                'LOGIN_RATE_LIMITED: ip=%s path=%s',
                request.META.get('REMOTE_ADDR', 'unknown'),
                request.path,
            )
            message = 'Too many login attempts. Please wait a minute and try again.'
            if request.headers.get('Accept', '').find('application/json') != -1 or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': message}, status=429)
            response = self.render_to_response(self.get_context_data(form=self.get_form()))
            response.status_code = 429
            if hasattr(response, 'context_data'):
                response.context_data['error'] = message
            return response

        timers = {}
        t_start = _time.time()

        t0 = _time.time()
        form = self.get_form()
        timers['get_form'] = (_time.time() - t0) * 1000

        t0 = _time.time()
        is_valid = form.is_valid()  # runs authenticate() + password verify
        timers['form_is_valid'] = (_time.time() - t0) * 1000

        if is_valid:
            t0 = _time.time()
            response = self.form_valid(form)
            timers['form_valid_otp'] = (_time.time() - t0) * 1000
            user = form.get_user()
            _emit_auth_event(
                request,
                'AUTH.LOGIN.SUCCESS',
                'INFO',
                'Login attempt succeeded',
                {
                    'user_id': getattr(user, 'pk', None),
                    'username_hash': _perf_username(getattr(user, 'username', username)),
                    'duration_ms': round((perf_time.perf_counter() - login_start) * 1000, 3),
                    'created': bool(getattr(request.session, 'session_key', None)),
                    'authentication_backend': getattr(user, 'backend', None),
                },
            )
        else:
            response = self.form_invalid(form)
            timers['form_invalid'] = 0.0
            # Account lockout policy: surface a clear message via the template's
            # {{ error }} block (locked account vs. plain invalid credentials).
            username = request.POST.get('username', '').strip()
            if hasattr(response, 'context_data'):
                response.context_data['error'] = self._login_error_message(form, username)
            self._refresh_captcha_context_after_failed_login(response, username)
            error_message = self._login_error_message(form, username)
            reason = 'locked_account' if error_message and 'locked' in error_message.lower() else 'invalid_credentials'
            _emit_auth_event(
                request,
                'AUTH.LOGIN.FAILED',
                'WARNING',
                'Login attempt failed',
                {
                    'username_hash': _perf_username(username),
                    'reason_category': reason,
                    'duration_ms': round((perf_time.perf_counter() - login_start) * 1000, 3),
                },
            )

        total = (_time.time() - t_start) * 1000
        breakdown = ' | '.join(f'{k}={v:.2f}ms' for k, v in timers.items())
        if getattr(settings, 'ENABLE_LOGIN_LATENCY_LOGS', False):
            logger.warning('LOGIN_POST_TIMING: total=%.2fms | %s', total, breakdown)
        return response


def _email_otp_context(request, message=None, error=None):
    from .otp_service import get_resend_cooldown_remaining

    remaining = get_resend_cooldown_remaining(request)
    return {
        'error': error,
        'message': message,
        'resend_remaining_seconds': remaining,
        'resend_cooldown_active': remaining > 0,
    }


def verify_email_otp(request):
    from .otp_service import (
        clear_pending_otp_session,
        validate_pending_otp_session,
    )

    pending_user_id = request.session.get('pending_mfa_user_id')
    if not pending_user_id:
        return redirect('login')

    context = _email_otp_context(request)

    if request.method == 'POST':
        if getattr(settings, 'DEBUG', False):
            logger.info('OTP_VERIFY_FORM_POST: path=%s', request.path)
        raw_otp = request.POST.get('otp', '').strip()
        otp = ''.join(ch for ch in raw_otp if ch.isdigit())
        if getattr(settings, 'DEBUG', False) and otp:
            logger.info('OTP_SUBMITTED_DEBUG_SUFFIX: otp_last2=%s', otp[-2:])

        if not otp:
            context['error'] = 'Invalid verification code.'
            return render(request, 'two_step_auth/verify_email_otp.html', context)
        if len(otp) != 6:
            context['error'] = 'Invalid verification code.'
            return render(request, 'two_step_auth/verify_email_otp.html', context)

        is_valid, reason = validate_pending_otp_session(request, otp)

        if not is_valid:
            if reason == 'expired':
                return redirect('/accounts/login/?otp_error=expired')

            if reason == 'max_attempts':
                return redirect('login')

            context['error'] = 'Invalid verification code.'
            return render(request, 'two_step_auth/verify_email_otp.html', context)

        User = get_user_model()
        try:
            user = User.objects.get(pk=pending_user_id)
        except User.DoesNotExist:
            clear_pending_otp_session(request)
            logger.warning('MFA_LOGIN_FAILED_USER_MISSING: user_id=%s', pending_user_id)
            return redirect('login')

        next_url = request.session.get('pending_mfa_next_url') or '/home/'
        login(request, user)
        clear_pending_otp_session(request)
        request.session.pop('pending_mfa_next_url', None)
        request.session['mfa_verified'] = True
        request.session.modified = True
        logger.info(
            'MFA_LOGIN_SUCCESS: user_id=%s username=%s',
            getattr(user, 'id', None),
            getattr(user, 'username', 'unknown'),
        )

        if not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = '/home/'

        return redirect(next_url)

    if getattr(settings, 'DEBUG', False):
        logger.info('OTP_VERIFY_FORM_RENDERED: path=%s', request.path)
    return render(request, 'two_step_auth/verify_email_otp.html', context)


@require_POST
def resend_email_otp(request):
    from .otp_service import (
        OTPResendCooldownError,
        clear_pending_otp_session,
        resend_pending_otp_session,
    )

    pending_user_id = request.session.get('pending_mfa_user_id')
    if not pending_user_id:
        return redirect('login')

    User = get_user_model()
    try:
        user = User.objects.get(pk=pending_user_id)
    except User.DoesNotExist:
        clear_pending_otp_session(request)
        logger.warning('OTP_RESEND_FAILED: user_id=%s reason=user_missing', pending_user_id)
        return redirect('login')

    context = _email_otp_context(request)

    try:
        resend_pending_otp_session(request, user)
    except OTPResendCooldownError:
        context = _email_otp_context(
            request,
            message='Please wait before requesting another code.',
        )
        return render(request, 'two_step_auth/verify_email_otp.html', context)
    except Exception:
        logger.exception(
            'OTP_RESEND_FAILED: user_id=%s username=%s',
            getattr(user, 'id', None),
            getattr(user, 'username', 'unknown'),
        )
        context = _email_otp_context(
            request,
            error='Unable to resend verification code. Please try again.',
        )
        return render(request, 'two_step_auth/verify_email_otp.html', context)

    messages.success(request, 'A new verification code has been sent.')
    return redirect('verify_email_otp')


@method_decorator(login_required(login_url='login'), name='dispatch')
class IndexView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'index.html'
 
    def get(self, request, format=None):
        from django.utils import timezone
        import time
        
        # Timing checkpoint 1: Start
        t1 = time.time()
        
        # Get allowed modules (optimized with values_list for minimal data transfer)
        allowed_modules = get_allowed_modules_for_user(request.user)
        
        t2 = time.time()
        if hasattr(request, 'timers'):
            request.timers['allowed_modules'] = f'{(t2-t1)*1000:.2f}ms'
        
        # Do not touch dashboard stats during page render.
        # The browser loads them separately from DashboardStatsAPIView after
        # /home/ is already visible, so slow cache/log/DB work cannot block TTFB.
        t3 = time.time()
        request._ttt_allowed_modules = allowed_modules
        dashboard_stats = []
        t4 = time.time()
        if hasattr(request, 'timers'):
            request.timers['dashboard_stats'] = f'{(t4-t3)*1000:.2f}ms'

        # One-shot: only the landing page right after an SSO redirect shows
        # the "no modules assigned" alert, not every dashboard visit.
        sso_just_logged_in = request.session.pop('sso_just_logged_in', False)
        show_sso_no_modules_alert = bool(sso_just_logged_in and not allowed_modules)

        # Build context
        context = {
            'user': request.user,
            'allowed_modules': allowed_modules,
            'dashboard_stats': dashboard_stats,
            'dashboard_stats_loading': True,
            'current_date': timezone.now().strftime('%d %b %Y'),
            'show_sso_no_modules_alert': show_sso_no_modules_alert,
        }
        
        # Create response (DRF handles rendering)
        response = Response(context)
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response['Pragma'] = 'no-cache'
        
        t5 = time.time()
        if hasattr(request, 'timers'):
            request.timers['context_build'] = f'{(t5-t4)*1000:.2f}ms'
        
        return response


@method_decorator(login_required(login_url='login'), name='dispatch')
class DashboardStatsAPIView(APIView):
    renderer_classes = [JSONRenderer]
    throttle_classes = [UserRateThrottle]

    def get(self, request, format=None):
        from django.conf import settings
        from .services import (
            filter_dashboard_stats_for_modules,
            get_dashboard_cache_snapshot,
            refresh_dashboard_stats_async,
        )
        import time

        t1 = time.time()
        allowed_modules = get_allowed_modules_for_user(request.user)
        dashboard_stats, labels, missing_labels, cache_lookup_ms = get_dashboard_cache_snapshot(
            allowed_module_names=allowed_modules
        )
        dashboard_stats = filter_dashboard_stats_for_modules(dashboard_stats, allowed_modules)
        refresh_started = refresh_dashboard_stats_async(missing_labels) if missing_labels else False
        elapsed_ms = (time.time() - t1) * 1000
        if getattr(settings, 'ENABLE_DASHBOARD_LATENCY_LOGS', False):
            logger.warning(
                'DASHBOARD_STATS_API: user_id=%s modules=%s missing=%s refresh_started=%s lookup=%.2fms total=%.2fms',
                request.user.id,
                len(dashboard_stats),
                missing_labels,
                refresh_started,
                cache_lookup_ms,
                elapsed_ms,
            )

        response = Response({
            'success': True,
            'stats': dashboard_stats,
            'count': len(dashboard_stats),
            'labels': [stat.get('label') for stat in dashboard_stats if stat.get('label')],
            'refreshing': bool(missing_labels),
            'refresh_started': refresh_started,
            'missing_labels': missing_labels,
            'expected_labels': labels,
            'retry_after_ms': 1200 if missing_labels else 0,
        })
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response['Pragma'] = 'no-cache'
        return response

    
@method_decorator(login_required(login_url='login-api'), name='dispatch')
class Visual_AidView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'ModelMaster/VisualAid.html'

    def get(self, request, batch_id=None, format=None):
        # Get parameters from URL or query string
        batch_id = batch_id or request.GET.get('batch_id')
        lot_id = request.GET.get('lot_id')
        plating_stk_no = request.GET.get('plating_stk_no')

        context = {
            'user': request.user,
        }
        visual_aid_started = image_perf_counter()
        emit_image_event(
            request,
            'IMAGE.VISUAL_AID.START',
            'INFO',
            'Visual Aid image request started',
            {
                'lookup_source': 'visual_aid',
                'stock_hash': hash_value((plating_stk_no or batch_id or '').strip().upper(), prefix='stock')
                if (plating_stk_no or batch_id) else None,
            },
        )

        # Import required models
        from modelmasterapp.models import ModelMasterCreation, LookLikeModel, ModelMaster, TotalStockModel

        batch_obj = None
        model_master_obj = None
        data_source = None  # Track whether data comes from ModelMasterCreation or ModelMaster

        # Handle lot_id parameter - NEW ADDITION
        if lot_id:
            logger.debug("Visual_AidView received lot_id: %s", lot_id)
            
            # Find TotalStockModel by lot_id to get batch_id
            total_stock_obj = TotalStockModel.objects.filter(lot_id=lot_id).first()
            
            if total_stock_obj and hasattr(total_stock_obj, 'batch_id'):
                batch_id_obj = total_stock_obj.batch_id
                logger.debug("Found batch_id object from TotalStockModel: %s", batch_id_obj)
                logger.debug("Type of batch_id object: %s", type(batch_id_obj))
                
                # Check if batch_id is a ForeignKey (ModelMasterCreation object) or string
                if hasattr(batch_id_obj, 'batch_id'):
                    # batch_id is a ForeignKey to ModelMasterCreation
                    batch_obj = batch_id_obj  # This IS the ModelMasterCreation object
                    logger.debug("batch_id is ForeignKey, using object directly: %s", batch_obj)
                    model_master_obj = batch_obj.model_stock_no
                    data_source = "ModelMasterCreation"
                elif isinstance(batch_id_obj, str):
                    # batch_id is a string field
                    full_batch_id_str = str(batch_id_obj)
                    logger.debug("batch_id is string: %s", full_batch_id_str)
                    
                    # Extract only the BATCH part after " - " if it exists
                    if " - " in full_batch_id_str:
                        batch_id = full_batch_id_str.split(" - ", 1)[1]
                        logger.debug("Extracted batch_id: %s", batch_id)
                    else:
                        batch_id = full_batch_id_str
                        logger.debug("Using full batch_id as no separator found: %s", batch_id)
                    
                    # Find ModelMasterCreation by batch_id string
                    batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
                    if batch_obj:
                        logger.debug("Found batch_obj by batch_id: %s", batch_obj)
                        model_master_obj = batch_obj.model_stock_no
                        data_source = "ModelMasterCreation"
                    else:
                        logger.debug("No ModelMasterCreation found for batch_id: %s", batch_id)
                else:
                    logger.debug("Unknown batch_id type: %s", type(batch_id_obj))
            else:
                logger.debug("No TotalStockModel found with lot_id %s or no batch_id attribute", lot_id)

        # Handle plating_stk_no parameter
        elif plating_stk_no:
            logger.debug("Visual_AidView received plating_stk_no: %s", plating_stk_no)
            
            # First, try to find ModelMasterCreation through ModelMaster
            model_master_obj = ModelMaster.objects.filter(plating_stk_no=plating_stk_no).first()
            logger.debug("Found ModelMaster by plating_stk_no: %s", model_master_obj)
            
            if model_master_obj:
                # Try to find corresponding ModelMasterCreation
                batch_obj = ModelMasterCreation.objects.filter(model_stock_no=model_master_obj).first()
                
                if batch_obj:
                    logger.debug("Found batch_obj by ModelMaster: %s", batch_obj)
                    data_source = "ModelMasterCreation"
                else:
                    logger.debug("No ModelMasterCreation found, using ModelMaster data directly")
                    data_source = "ModelMaster"
            else:
                logger.debug("No ModelMaster found with plating_stk_no: %s", plating_stk_no)
                
        # Handle batch_id parameter
        elif batch_id:
            logger.debug("Visual_AidView received batch_id: %s", batch_id)
            batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
            if batch_obj:
                logger.debug("Found batch_obj by batch_id: %s", batch_obj)
                model_master_obj = batch_obj.model_stock_no
                data_source = "ModelMasterCreation"

        # Populate context based on available data
        if batch_obj or model_master_obj:
            if data_source == "ModelMasterCreation" and batch_obj:
                # Use ModelMasterCreation data (preferred)
                stock_for_images = batch_obj.plating_stk_no or getattr(batch_obj.model_stock_no, 'plating_stk_no', '')
                hover_payload = _get_model_hover_payload(
                    request,
                    plating_stk_no or stock_for_images,
                )
                images_payload = hover_payload.get('images', []) if hover_payload else []
                image_data = _only_model_view_images(images_payload)
                if not image_data:
                    image_data = [img for img in images_payload if img.get('url')][:1]
                image_urls = [img['url'] for img in image_data]
                
                context.update({
                    'batch_id': batch_obj.batch_id,
                    'lot_id': lot_id,  # Include lot_id in context if it was provided
                    'image_urls': image_urls,
                    'image_data': image_data,
                    'plating_stk_no': batch_obj.plating_stk_no,
                    'changes': batch_obj.changes,
                    'polish_finish': batch_obj.polish_finish,
                    'version': batch_obj.version.version_internal if batch_obj.version else None,
                    'data_source': 'ModelMasterCreation'
                })
                model_master_instance = batch_obj.model_stock_no
                
            elif data_source == "ModelMaster" and model_master_obj:
                # Use ModelMaster data directly
                hover_payload = _get_model_hover_payload(
                    request,
                    plating_stk_no or model_master_obj.plating_stk_no,
                )
                images_payload = hover_payload.get('images', []) if hover_payload else []
                image_data = _only_model_view_images(images_payload)
                if not image_data:
                    image_data = [img for img in images_payload if img.get('url')][:1]
                image_urls = [img['url'] for img in image_data]
                
                context.update({
                    'batch_id': None,
                    'lot_id': lot_id,  # Include lot_id in context if it was provided
                    'image_urls': image_urls,
                    'image_data': image_data,
                    'plating_stk_no': model_master_obj.plating_stk_no,
                    'changes': getattr(model_master_obj, 'changes', 'N/A'),  # May not exist in ModelMaster
                    'polish_finish': model_master_obj.polish_finish,
                    'version': model_master_obj.version,
                    'brand': model_master_obj.brand,
                    'gender': model_master_obj.gender,
                    'ep_bath_type': model_master_obj.ep_bath_type,
                    'data_source': 'ModelMaster'
                })
                model_master_instance = model_master_obj

            logger.debug("Using %s as data source", data_source)
            logger.debug("Context plating_stk_no: %s", context.get('plating_stk_no'))

            # Get related versions and similar models
            if model_master_instance:
                # Get all ModelMaster objects with same model_no for variants
                masters = ModelMaster.objects.filter(model_no=model_master_instance.model_no)
                version_labels = []
                seen_versions = set()

                for master in masters:
                    if not master.version:
                        continue
                    version_text = str(master.version)
                    if version_text in seen_versions:
                        continue
                    seen_versions.add(version_text)
                    version_labels.append(version_text)

                context['modelmaster_versions'] = version_labels
                context['same_model_list'] = _build_similar_model_list(
                    model_master_instance,
                    creation_model=ModelMasterCreation,
                )
            else:
                context['same_model_list'] = []
                context['modelmaster_versions'] = []
        else:
            # Handle error cases
            if lot_id:
                context['error'] = f"No data found for lot_id: {lot_id}"
            elif plating_stk_no:
                context['error'] = f"No ModelMaster found with plating_stk_no: {plating_stk_no}"
            elif batch_id:
                context['error'] = f"No ModelMasterCreation found for batch_id: {batch_id}"
            else:
                context['error'] = "Please provide either lot_id, batch_id, or plating_stk_no parameter"

        emit_image_event(
            request,
            'IMAGE.VISUAL_AID.END',
            'INFO',
            'Visual Aid image request completed',
            {
                'lookup_source': 'visual_aid',
                'duration_ms': image_duration_ms(visual_aid_started),
                'images_returned': len(context.get('image_urls', []) or []),
                'result': 'error' if context.get('error') else 'completed',
            },
        )
        return Response(context)
    
_STOCK_NO_RE = re.compile(r'^[A-Z0-9/_-]{1,50}$', re.IGNORECASE)
_STOCK_NO_CANONICAL_RE = re.compile(r'(\d{4})([A-Z])([A-Z])([A-Z])(\d{2})', re.IGNORECASE)

_IMAGE_VIEW_LABELS = {
    'TV': 'Top View',
    'FV': 'Front View',
    'IV': 'Inside View',
    'RSV': 'Right Side View',
    'LSV': 'Left Side View',
    'BV': 'Bottom View',
}

_IMAGE_VIEW_ORDER = {
    'TV': 0,
    'FV': 1,
    'IV': 2,
    'RSV': 3,
}

_ALLOWED_MODEL_IMAGE_VIEWS = {'TV', 'FV', 'IV', 'RSV'}

_HOVER_PREVIEW_PREFERENCE = ('IV',)
SIMILAR_MODEL_PREFIX_MAP = {
    '2617': '2648',
    '2648': '2617',
}


def _build_similar_model_list(model_master_instance, creation_model=None):
    if not model_master_instance:
        return []

    creation_model = creation_model or ModelMasterCreation
    current_pk = getattr(model_master_instance, 'pk', None)
    current_stock_no = str(getattr(model_master_instance, 'plating_stk_no', '') or '').strip()
    current_model_no = str(getattr(model_master_instance, 'model_no', '') or '').strip()
    current_match = re.search(r'\d{4}', current_stock_no or current_model_no)
    current_model_digits = current_match.group(0) if current_match else current_model_no[:4]
    target_model_digits = SIMILAR_MODEL_PREFIX_MAP.get(current_model_digits)
    seen_models = set()

    def add_model(model, items):
        stock_value = str(
            getattr(model, 'plating_stk_no', None)
            or getattr(model, 'model_no', None)
            or ''
        ).strip()
        match = re.search(r'\d{4}', stock_value)
        if not match or getattr(model, 'pk', None) == current_pk:
            return

        model_digits = match.group(0)
        if model_digits == current_model_digits:
            return
        if target_model_digits and model_digits != target_model_digits:
            return
        if model_digits in seen_models:
            return

        seen_models.add(model_digits)
        stock_no = str(getattr(model, 'plating_stk_no', '') or '').strip()

        items.append({
            'model_no': model_digits,
            'plating_stk_no': stock_no,
            'model_master_id': model.pk,
            'has_creation': False,
            'has_model_master': True,
            'version': model.version,
            'polish_finish': str(model.polish_finish) if model.polish_finish else None,
        })

    similar_items = []

    if target_model_digits:
        mapped_masters = (
            ModelMaster.objects
            .filter(
                Q(plating_stk_no__startswith=target_model_digits)
                | Q(model_no__startswith=target_model_digits)
            )
            .order_by('id')
        )
        for related_master in mapped_masters:
            add_model(related_master, similar_items)
    else:
        look_like_obj = (
            LookLikeModel.objects
            .filter(same_plating_stk_no=model_master_instance)
            .prefetch_related('plating_stk_no')
            .first()
        )
        logger.debug("LookLikeModel object: %s", look_like_obj)

        if look_like_obj:
            for related_master in look_like_obj.plating_stk_no.all():
                add_model(related_master, similar_items)

    if similar_items:
        target_ids = [item['model_master_id'] for item in similar_items]
        creation_ids = set(
            creation_model.objects
            .filter(model_stock_no_id__in=target_ids)
            .values_list('model_stock_no_id', flat=True)
        )
        for item in similar_items:
            item['has_creation'] = item['model_master_id'] in creation_ids
        return similar_items

    if target_model_digits:
        return [{
            'model_no': target_model_digits,
            'plating_stk_no': '',
            'model_master_id': None,
            'has_creation': False,
            'has_model_master': False,
            'version': '',
            'polish_finish': None,
        }]

    return []


def _parse_stock_no(raw_stock_no):
    stock_no = (raw_stock_no or '').strip().upper()
    if not stock_no or not _STOCK_NO_RE.match(stock_no):
        return None

    match = _STOCK_NO_CANONICAL_RE.search(stock_no)
    if not match:
        return None

    model_no, polish_code, plating_code, bath_code, version_code = match.groups()
    image_keys = build_model_image_keys_from_stock(stock_no)
    canonical = ''.join(match.groups()).upper()
    return {
        'raw': stock_no,
        'canonical': canonical,
        'lookup_stock_no': stock_no,
        'model_no': model_no,
        'polish_code': polish_code.upper(),
        'plating_code': plating_code.upper(),
        'bath_code': bath_code.upper(),
        'version_code': version_code,
        'image_key': image_keys[0] if image_keys else '',
        'image_keys': image_keys,
    }


def _resolve_base_model_number(raw_model_number):
    raw_value = (raw_model_number or '').strip()
    if not raw_value:
        return ''

    model_master = (
        ModelMaster.objects
        .filter(Q(plating_stk_no__iexact=raw_value) | Q(model_no__iexact=raw_value))
        .only('model_no')
        .first()
    )
    if model_master and model_master.model_no:
        return str(model_master.model_no).strip()

    stock_parts = _parse_stock_no(raw_value)
    if stock_parts:
        return stock_parts['model_no']

    return raw_value.upper()


def _detect_image_view(image_or_name):
    for attr_name in ('view_code', 'image_view', 'view'):
        explicit_view = getattr(image_or_name, attr_name, '')
        explicit_view = str(explicit_view or '').strip().upper()
        if explicit_view in _ALLOWED_MODEL_IMAGE_VIEWS:
            return explicit_view, _IMAGE_VIEW_LABELS[explicit_view]

    if hasattr(image_or_name, 'master_image'):
        image_name = _get_model_image_lookup_name(image_or_name)
    else:
        image_name = image_or_name

    view_code = detect_image_type(image_name)
    if view_code in _ALLOWED_MODEL_IMAGE_VIEWS:
        return view_code, _IMAGE_VIEW_LABELS[view_code]
    return 'VIEW', 'View'

def _get_model_image_lookup_name(img):
    return get_model_image_lookup_name(img)


def _image_matches_key(img, image_key):
    return (
        build_model_image_key_from_filename(_get_model_image_lookup_name(img))
        == str(image_key or '').lower()
    )


def _filter_images_by_key(images, image_keys):
    valid_images = list(images)
    for image_key in image_keys:
        matched_images = [
            img
            for img in valid_images
            if getattr(img, 'master_image', None)
            and _image_matches_key(img, image_key)
        ]
        if matched_images:
            return matched_images
    return []


def _link_model_image_to_matching_masters(model_image):
    image_key = build_model_image_key_from_filename(
        _get_model_image_lookup_name(model_image)
    )
    if not image_key:
        return 0

    matching_masters = [
        master
        for master in ModelMaster.objects.prefetch_related('images').all()
        if image_key in build_model_image_keys_from_stock(master.plating_stk_no)
    ]

    linked_count = 0
    for master in matching_masters:
        if not master.images.filter(pk=model_image.pk).exists():
            master.images.add(model_image)
            linked_count += 1

    return linked_count


def _get_no_image_placeholder():
    return get_global_no_image()

def _sort_model_images(images):
    selected_by_view = {}
    seen_images = set()

    def sort_key(img):
        lookup_name = _get_model_image_lookup_name(img)
        return (
            _IMAGE_VIEW_ORDER.get(
                _detect_image_view(img)[0],
                99,
            ),
            os.path.basename(lookup_name).lower(),
            getattr(img, 'id', 0) or 0,
        )

    valid_images = [
        img
        for img in images
        if getattr(img, 'master_image', None)
    ]

    for img in sorted(valid_images, key=sort_key):
        lookup_name = _get_model_image_lookup_name(img)
        image_key = (
            getattr(img, 'pk', None),
            str(getattr(getattr(img, 'master_image', None), 'name', '') or '').strip().lower(),
        )
        if image_key in seen_images:
            continue
        seen_images.add(image_key)

        view_code = _detect_image_view(img)[0]

        if view_code not in _ALLOWED_MODEL_IMAGE_VIEWS:
            continue

        selected_by_view.setdefault(view_code, img)

    return [
        selected_by_view[view_code]
        for view_code in ('TV', 'FV', 'IV', 'RSV')
        if view_code in selected_by_view
    ]


def _get_images_for_stock(
    stock_parts,
    model_master=None,
    model_creation=None,
):
    if model_master:
        model_images = list(model_master.images.all())
        matched_model_images = _filter_images_by_key(model_images, stock_parts['image_keys'])
        no_image = get_no_image(model_images)

        if matched_model_images or no_image:
            source_images = list(matched_model_images)
            if no_image and no_image not in source_images:
                source_images.append(no_image)
            return source_images

    if model_creation:
        creation_images = list(model_creation.images.all())
        matched_creation_images = _filter_images_by_key(creation_images, stock_parts['image_keys'])
        no_image = get_no_image(creation_images)

        if matched_creation_images or no_image:
            source_images = list(matched_creation_images)
            if no_image and no_image not in source_images:
                source_images.append(no_image)
            return source_images

    placeholder = get_global_no_image()
    return [placeholder] if placeholder else []


def _build_image_payload(request, images):
    """
    Build the image payload used by hover preview and Visual Aid.

    Real model images and the configured NO_IMAGE placeholder are both
    returned. The placeholder must not be filtered out, otherwise templates
    receive an empty image list and display "No image available".
    """
    payload = []

    for img in images:
        try:
            emit_media_read(
                request,
                getattr(img, 'master_image', None),
                lookup_source='image_payload',
            )

            image_url = get_image_url(img)
            if not image_url:
                continue

            resolved_url = image_url

            if is_no_image_model_image(img):
                payload.append({
                    'id': img.id,
                    'url': resolved_url,
                    'view_code': 'NO_IMAGE',
                    'view': 'No Image',
                    'is_placeholder': True,
                })
                continue

            view_code, view_label = _detect_image_view(img)

            payload.append({
                'id': img.id,
                'url': resolved_url,
                'view_code': view_code,
                'view': view_label,
                'is_placeholder': False,
            })

        except Exception as exc:
            logger.warning(
                'Unable to build image payload for image id=%s: %s',
                getattr(img, 'id', None),
                exc,
            )

    return payload


def _build_model_view_payload(request, images):
    payload = []

    for view_code in ('TV', 'FV', 'IV', 'RSV'):
        img = next(iter(get_model_view_images(images, view_sequence=(view_code,))), None)
        if not img:
            continue
        if is_no_image_model_image(img):
            continue

        try:
            emit_media_read(
                request,
                img.master_image,
                lookup_source='image_payload',
            )
            image_url = get_image_url(img)
            if not image_url:
                continue
        except Exception:
            continue

        payload.append({
            'id': img.id,
            'url': image_url,
            'view_code': view_code,
            'view': _IMAGE_VIEW_LABELS[view_code],
            'is_placeholder': is_no_image_model_image(img),
        })

    return payload


def _only_isometric_view(images_payload):
    """
    Visual Aid page must show a single Isometric View image only (no
    multi-view gallery). Falls back to the first available image if the
    model has no dedicated IV upload, so the page never renders blank.
    """
    iv_images = [img for img in images_payload if img.get('view_code') == 'IV']
    if iv_images:
        return [iv_images[0]]
    return images_payload[:1]


def _get_model_hover_payload(request, raw_stock_no):
    feature_started = image_perf_counter()
    stock_parts = _parse_stock_no(raw_stock_no)
    if not stock_parts:
        emit_lookup_not_found(
            request,
            'hover_preview_payload',
            image_duration_ms(feature_started),
            'invalid_stock_number',
        )
        return None

    emit_lookup_start(
        request,
        'hover_preview_payload',
        stock_no=stock_parts['lookup_stock_no'],
        model_no=stock_parts['model_no'],
        view_requested='hover',
    )

    model_master = (
        ModelMaster.objects
        .prefetch_related('images')
        .filter(plating_stk_no__iexact=stock_parts['lookup_stock_no'])
        .first()
    )
    model_creation = (
        ModelMasterCreation.objects
        .select_related('model_stock_no')
        .prefetch_related('images')
        .filter(
            Q(plating_stk_no__iexact=stock_parts['lookup_stock_no'])
            | Q(model_stock_no__plating_stk_no__iexact=stock_parts['lookup_stock_no'])
        )
        .first()
    )

    metadata_model = model_master or getattr(model_creation, 'model_stock_no', None)
    resolved_images = _get_images_for_stock(
        stock_parts,
        model_master=model_master,
        model_creation=model_creation,
    )
    ordered_images = _sort_model_images(resolved_images)
    images_payload = _build_image_payload(request, ordered_images)

    preview = next(
        (img for img in images_payload if img.get('view_code') == 'IV'),
        None,
    )
    if not preview and images_payload:
        preview = images_payload[0]

    if not preview:
        emit_lookup_not_found(
            request,
            'hover_preview_payload',
            image_duration_ms(feature_started),
            'model_images_not_found',
            {
                'stock_hash': hash_value(stock_parts['lookup_stock_no'], prefix='stock'),
                'model_hash': hash_value(stock_parts['model_no'], prefix='model'),
            },
        )
        placeholder_payload = _build_placeholder_image_payload(
            stock_parts['lookup_stock_no'],
            request=request,
            metadata_model=metadata_model,
        )
        if metadata_model:
            placeholder_payload.update({
                'found': True,
                'model_no': metadata_model.model_no or stock_parts['model_no'],
                'version': stock_parts['version_code'],
                'bath_type': stock_parts['bath_code'],
                'ep_bath_type': metadata_model.ep_bath_type or '',
            })
        return placeholder_payload

    return {
        'found': True,
        'stock_no': stock_parts['lookup_stock_no'],
        'raw_stock_no': stock_parts['raw'],
        'model_no': getattr(metadata_model, 'model_no', '') or stock_parts['model_no'],
        'version': stock_parts['version_code'],
        'bath_type': stock_parts['bath_code'],
        'ep_bath_type': getattr(metadata_model, 'ep_bath_type', '') or '',
        'images': images_payload,
        'preview_image': preview.get('url', ''),
        'preview_view': preview.get('view', ''),
        'visual_aid_url': '/adminportal/dp_visualaid/?plating_stk_no=' + stock_parts['lookup_stock_no'],
    }


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class ModelHoverPreviewAPIView(APIView):
    """
    GET /adminportal/api/model-hover-preview/<stock_no>/
    Returns image URLs and metadata for the model identified by stock_no.
    Used by the global ttt-stock-hover.js popup system.
    All model image URLs are returned as relative /media/... paths so they
    work correctly behind IIS/reverse proxies and never expose localhost URLs.
    """
    renderer_classes = [JSONRenderer]

    def get(self, request, stock_no=None, format=None):
        stock_no = stock_no or request.GET.get('stock_no', '')
        payload = _get_model_hover_payload(request, stock_no)
        if payload is None:
            return Response({'error': 'Invalid stock number'}, status=status.HTTP_400_BAD_REQUEST)
        response = Response(payload, status=status.HTTP_200_OK)
        response['Cache-Control'] = 'no-store'
        return response



def _canonical_version_label(version):
    """
    Return the project-wide version label, for example:
    A -> Version A
    version a -> Version A
    Version A -> Version A
    """
    raw_version = str(version or '').strip()
    if not raw_version:
        return ''

    suffix = re.sub(
        r'^VERSION\s*',
        '',
        raw_version,
        flags=re.IGNORECASE,
    ).strip().upper()

    return f'Version {suffix}' if suffix else ''


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class ModelVersionComparisonAPIView(APIView):
    renderer_classes = [JSONRenderer]
    http_method_names = ['get']

    def get(self, request, format=None):
        requested_model_number = request.GET.get('model_number', '')
        requested_version = _canonical_version_label(request.GET.get('version', ''))
        if not requested_model_number or not requested_model_number.strip():
            return Response({
                'success': False,
                'message': 'model_number is required.',
            }, status=status.HTTP_400_BAD_REQUEST)

        requested_model_number = requested_model_number.strip().upper()
        base_model_number = _resolve_base_model_number(requested_model_number).strip().upper()
        if requested_version:
            is_allowed, validation_message = validate_model_version(base_model_number, requested_version)
            if not is_allowed:
                return Response({
                    'success': False,
                    'message': validation_message,
                }, status=status.HTTP_400_BAD_REQUEST)

        model_filters = Q(model_number__iexact=requested_model_number)
        if base_model_number:
            model_filters |= Q(model_number__iexact=base_model_number)

        comparisons = ModelVersionComparison.objects.filter(
            model_filters,
            is_active=True
        ).prefetch_related('images')

        if requested_version:
            comparisons = comparisons.filter(version__iexact=requested_version)

        comparison = (
            comparisons
            .order_by('model_number', 'version')
            .first()
        )

        images = _get_version_comparison_image_urls(comparison) if comparison else []

        if not comparison or not images:
            return Response({
                'success': False,
                'message': 'No variant comparison image is available for this model.',
            }, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'success': True,
            'model_number': comparison.model_number,
            'version': _canonical_version_label(comparison.version or comparison.title),
            'title': comparison.title or f'Difference between different variants of {comparison.model_number}',
            'description': comparison.description or '',
            'info': _build_version_info_payload(comparison),
            'comparison_image': images[0],
            'images': images,
        }, status=status.HTTP_200_OK)


_VERSION_COMPARISON_ALLOWED_MIME = frozenset({
    'image/jpeg',
    'image/png',
    'image/webp',
})
_VERSION_COMPARISON_ALLOWED_EXT = frozenset({
    '.jpg',
    '.jpeg',
    '.png',
    '.webp',
})
_VERSION_COMPARISON_MAX_SIZE = 10 * 1024 * 1024
ALLOWED_MODEL_VERSIONS = {
    '2617': {'Version A', 'Version B', 'Version C', 'Version D'},
    '2648': {'Version A', 'Version B', 'Version D', 'Version E'},
    '1805': {'Version D', 'Version K'},
}
_VERSION_INFO_DECIMAL_FIELDS = (
    ('thickness', 'Thickness'),
    ('bcb_to_lug_bottom', 'BCB to Lug Bottom'),
    ('dial_slant_angle', 'Dial Slant Angle'),
    ('dial_slant_height', 'Dial Slant Height'),
)
VERSION_INFO_FIELDS = {
    '2617': {
        'o_ring_pocket',
        'cph_depth',
        'cph_centre_to_dial_butting',
    },
    '2648': {
        'o_ring_pocket',
        'dial_slant_finish',
        'thickness',
        'bcb_to_lug_bottom',
        'dial_slant_angle',
        'dial_slant_height',
    },
    '1805': {
        'outer_wall_thickness',
        'cph_centre_to_bc_butting',
        'crm_length',
    },
}
VERSION_INFO_NUMERIC_FIELDS = {
    'cph_depth',
    'cph_centre_to_dial_butting',
    'thickness',
    'bcb_to_lug_bottom',
    'dial_slant_angle',
    'dial_slant_height',
    'outer_wall_thickness',
    'cph_centre_to_bc_butting',
    'crm_length',
}
VERSION_INFO_O_RING_OPTIONS = {
    '2617': {'Yes', 'No', 'Without O Ring Box in Case Centre'},
    '2648': {'Yes', 'No'},
}


def validate_model_version(model_number, version):
    normalized_model = str(model_number or '').strip().upper()
    normalized_version = _canonical_version_label(version)
    allowed_versions = ALLOWED_MODEL_VERSIONS.get(normalized_model)

    if not allowed_versions:
        return False, 'Unsupported model number.'

    if normalized_version not in allowed_versions:
        allowed_text = ', '.join(sorted(allowed_versions))
        return (
            False,
            f'{normalized_version or "Version"} is not allowed for model '
            f'{normalized_model}. Allowed versions: {allowed_text}.'
        )

    return True, ''


def _parse_optional_decimal(value, label):
    raw_value = (value or '').strip()
    if not raw_value:
        return None, ''

    try:
        return Decimal(raw_value), ''
    except (InvalidOperation, ValueError):
        return None, f'{label} must be a valid number.'


def _format_optional_decimal(value):
    if value is None:
        return ''

    formatted_value = format(value, 'f')
    if '.' in formatted_value:
        formatted_value = formatted_value.rstrip('0').rstrip('.')
    return formatted_value


def _legacy_version_info_payload(item):
    return {
        'o_ring_pocket': item.o_ring_pocket or '',
        'dial_slant_finish': item.dial_slant_finish or '',
        'thickness': _format_optional_decimal(item.thickness),
        'bcb_to_lug_bottom': _format_optional_decimal(item.bcb_to_lug_bottom),
        'dial_slant_angle': _format_optional_decimal(item.dial_slant_angle),
        'dial_slant_height': _format_optional_decimal(item.dial_slant_height),
    }


def _build_version_info_payload(item):
    if not item:
        return {}

    model_number = (item.model_number or '').strip().upper()
    allowed_fields = VERSION_INFO_FIELDS.get(model_number, set())
    stored_info = item.version_info if isinstance(item.version_info, dict) else {}
    if stored_info:
        return {
            field_name: str(stored_info.get(field_name, '')).strip()
            for field_name in allowed_fields
            if str(stored_info.get(field_name, '')).strip()
        }

    if model_number == '2648':
        legacy_info = _legacy_version_info_payload(item)
        return {
            field_name: value
            for field_name, value in legacy_info.items()
            if field_name in allowed_fields and value
        }

    return {}


def _extract_legacy_version_info_from_post(post_data):
    legacy_info = {}
    for field_name in VERSION_INFO_FIELDS['2648']:
        raw_value = (post_data.get(field_name, '') or '').strip()
        if raw_value:
            legacy_info[field_name] = raw_value
    return legacy_info


def _validate_version_info_payload(model_number, info_payload):
    normalized_model = (model_number or '').strip().upper()
    allowed_fields = VERSION_INFO_FIELDS.get(normalized_model)
    if allowed_fields is None:
        return None, 'Unsupported model number.'

    if not isinstance(info_payload, dict):
        return None, 'version_info must be a JSON object.'

    cleaned_info = {}
    for raw_key, raw_value in info_payload.items():
        field_name = str(raw_key or '').strip()
        if field_name not in allowed_fields:
            return None, f'{field_name} is not allowed for model {normalized_model}.'

        value = '' if raw_value is None else str(raw_value).strip()
        if not value or value == 'Not specified':
            continue

        if field_name == 'o_ring_pocket':
            allowed_options = VERSION_INFO_O_RING_OPTIONS.get(normalized_model, set())
            if value not in allowed_options:
                allowed_text = ', '.join(sorted(allowed_options))
                return None, f'O Ring Pocket must be one of: {allowed_text}.'

        if field_name in VERSION_INFO_NUMERIC_FIELDS:
            _, error_message = _parse_optional_decimal(value, field_name.replace('_', ' ').title())
            if error_message:
                return None, error_message

        cleaned_info[field_name] = value

    return cleaned_info, ''


def _extract_version_info_from_post(post_data):
    return _extract_legacy_version_info_from_post(post_data), ''

def _extract_version_info_json_from_post(model_number, post_data):
    raw_info = post_data.get('version_info')
    if raw_info is None:
        legacy_info, _ = _extract_version_info_from_post(post_data)
        return _validate_version_info_payload(model_number, legacy_info)

    try:
        parsed_info = json.loads(raw_info or '{}')
    except json.JSONDecodeError:
        return None, 'version_info contains malformed JSON.'

    return _validate_version_info_payload(model_number, parsed_info)


def _build_legacy_version_info_defaults(model_number, version_info):
    if model_number != '2648':
        return {
            'o_ring_pocket': None,
            'dial_slant_finish': None,
            'thickness': None,
            'bcb_to_lug_bottom': None,
            'dial_slant_angle': None,
            'dial_slant_height': None,
        }

    defaults = {
        'o_ring_pocket': version_info.get('o_ring_pocket') or None,
        'dial_slant_finish': version_info.get('dial_slant_finish') or None,
    }
    for field_name, _label in _VERSION_INFO_DECIMAL_FIELDS:
        value = version_info.get(field_name)
        defaults[field_name] = Decimal(value) if value else None
    return defaults


def _get_version_comparison_image_items(item):
    if not item:
        return []

    image_items = [
        {
            'id': image_item.id,
            'url': image_item.image.url,
            'filename': image_item.original_filename or os.path.basename(image_item.image.name),
            'display_order': image_item.display_order,
        }
        for image_item in item.images.all()
        if image_item.image
    ]

    if not image_items and item.comparison_image:
        image_items.append({
            'id': None,
            'url': item.comparison_image.url,
            'filename': os.path.basename(item.comparison_image.name),
            'display_order': 0,
        })

    return image_items


def _get_version_comparison_image_urls(item):
    return [image_item['url'] for image_item in _get_version_comparison_image_items(item)]


def _build_version_comparison_payload(item):
    image_items = _get_version_comparison_image_items(item)
    return {
        'id': item.id,
        'model_number': item.model_number,
        'version': item.version or item.title or '',
        'title': item.title or '',
        'comparison_image': image_items[0]['url'] if image_items else '',
        'images': image_items,
        'image_count': len(image_items),
        'description': item.description or '',
        'info': _build_version_info_payload(item),
        'is_active': item.is_active,
        'updated_at': item.updated_at.isoformat() if item.updated_at else '',
    }


def _validate_version_comparison_image(uploaded_file):
    if not uploaded_file:
        return 'Image is required.'

    if uploaded_file.size and uploaded_file.size > _VERSION_COMPARISON_MAX_SIZE:
        return 'Image exceeds maximum allowed size of 10 MB.'

    _, ext = os.path.splitext((uploaded_file.name or '').lower())
    if ext not in _VERSION_COMPARISON_ALLOWED_EXT:
        return 'File extension is not allowed. Allowed: jpg, jpeg, png, webp.'

    content_type = getattr(uploaded_file, 'content_type', '') or ''
    if content_type and content_type not in _VERSION_COMPARISON_ALLOWED_MIME:
        return 'File type is not allowed. Only PNG, JPG, JPEG and WEBP images are accepted.'

    return None


@login_required(login_url='login-api')
@require_admin
@require_GET
def model_version_comparison_list(request):
    items = (
        ModelVersionComparison.objects
        .prefetch_related('images')
        .order_by('model_number', 'version')
    )
    return JsonResponse({
        'success': True,
        'items': [_build_version_comparison_payload(item) for item in items],
    })


@login_required(login_url='login-api')
@require_admin
@require_POST
def model_version_comparison_upload(request):
    raw_model_number = request.POST.get('model_number', '').strip().upper()
    version = _canonical_version_label(request.POST.get('version', ''))
    uploaded_files = request.FILES.getlist('comparison_images')
    if not uploaded_files:
        uploaded_files = request.FILES.getlist('comparison_image')

    if not raw_model_number:
        return JsonResponse({
            'success': False,
            'message': 'Base model number is required.',
        }, status=400)

    if not version:
        return JsonResponse({
            'success': False,
            'message': 'Version is required.',
        }, status=400)

    base_model_number = _resolve_base_model_number(raw_model_number).strip().upper()
    if not base_model_number:
        return JsonResponse({
            'success': False,
            'message': 'Base model number is required.',
        }, status=400)

    is_allowed, validation_message = validate_model_version(base_model_number, version)
    if not is_allowed:
        return JsonResponse({
            'success': False,
            'message': validation_message,
        }, status=400)

    version_info, info_error = _extract_version_info_json_from_post(base_model_number, request.POST)
    if info_error:
        return JsonResponse({
            'success': False,
            'message': info_error,
        }, status=400)

    existing_comparison = ModelVersionComparison.objects.filter(
        model_number=base_model_number,
        version=version,
    ).first()
    if not uploaded_files and not existing_comparison:
        return JsonResponse({
            'success': False,
            'message': 'At least one image is required for a new version record.',
        }, status=400)

    for uploaded_file in uploaded_files:
        validation_error = _validate_version_comparison_image(uploaded_file)
        if validation_error:
            return JsonResponse({
                'success': False,
                'message': f'{uploaded_file.name}: {validation_error}',
            }, status=400)

    with transaction.atomic():
        comparison, created = ModelVersionComparison.objects.update_or_create(
            model_number=base_model_number,
            version=version,
            defaults={
                'title': version,
                'version_info': version_info,
                **_build_legacy_version_info_defaults(base_model_number, version_info),
                'is_active': True,
            }
        )

        start_order = comparison.images.count()
        existing_filenames = set(
            comparison.images
            .exclude(original_filename='')
            .values_list('original_filename', flat=True)
        )
        seen_request_filenames = set()
        created_image_count = 0
        skipped_duplicate_count = 0
        for uploaded_file in uploaded_files:
            original_filename = uploaded_file.name or ''
            if original_filename and (
                original_filename in existing_filenames or
                original_filename in seen_request_filenames
            ):
                skipped_duplicate_count += 1
                continue

            created_image = ModelVersionComparisonImage.objects.create(
                comparison=comparison,
                image=uploaded_file,
                original_filename=original_filename,
                display_order=start_order + created_image_count,
            )

            # Keep the legacy single-image field synchronized with the first
            # related image. Older server code and fallback lookups still read
            # comparison_image, while newer code reads comparison.images.
            if not comparison.comparison_image and created_image.image:
                comparison.comparison_image.name = created_image.image.name
                comparison.save(update_fields=['comparison_image', 'updated_at'])

            if original_filename:
                seen_request_filenames.add(original_filename)
            created_image_count += 1

    return JsonResponse({
        'success': True,
        'created': created,
        'message': 'Version images uploaded successfully.',
        'model_number': comparison.model_number,
        'version': comparison.version or '',
        'uploaded_count': created_image_count,
        'skipped_duplicate_count': skipped_duplicate_count,
        'comparison_image': _build_version_comparison_payload(comparison)['comparison_image'],
        'item': _build_version_comparison_payload(comparison),
    })


@login_required(login_url='login-api')
@require_admin
@require_POST
def model_version_comparison_delete(request):
    item_id = request.POST.get('id')
    if not item_id:
        return JsonResponse({
            'success': False,
            'message': 'Version image ID is required.',
        }, status=400)

    comparison = get_object_or_404(ModelVersionComparison, pk=item_id)
    for image_item in comparison.images.all():
        if image_item.image:
            image_item.image.delete(save=False)
    if comparison.comparison_image:
        comparison.comparison_image.delete(save=False)
    comparison.delete()
    return JsonResponse({
        'success': True,
        'message': 'Version image deleted successfully.',
    })


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class Rec_Visual_AidView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'ModelMaster/VisualAid.html'

    def get(self, request, batch_id=None, format=None):
        # Get parameters from URL or query string
        batch_id = batch_id or request.GET.get('batch_id')
        plating_stk_no = request.GET.get('plating_stk_no')

        context = {
            'user': request.user,
        }
        visual_aid_started = image_perf_counter()
        emit_image_event(
            request,
            'IMAGE.VISUAL_AID.START',
            'INFO',
            'Visual Aid image request started',
            {
                'lookup_source': 'recovery_visual_aid',
                'stock_hash': hash_value((plating_stk_no or batch_id or '').strip().upper(), prefix='stock')
                if (plating_stk_no or batch_id) else None,
            },
        )


        batch_obj = None
        model_master_obj = None
        data_source = None  # Track whether data comes from ModelMasterCreation or ModelMaster

        # Handle plating_stk_no parameter
        if plating_stk_no:
            print(f"🔍 Visual_AidView received plating_stk_no: {plating_stk_no}")
            
            # First, try to find RecoveryMasterCreation through ModelMaster
            model_master_obj = ModelMaster.objects.filter(plating_stk_no=plating_stk_no).first()
            print(f"Found ModelMaster by plating_stk_no: {model_master_obj}")
            
            if model_master_obj:
                # Try to find corresponding RecoveryMasterCreation
                batch_obj = RecoveryMasterCreation.objects.filter(model_stock_no=model_master_obj).first()
                
                if batch_obj:
                    print(f"Found batch_obj by ModelMaster: {batch_obj}")
                    data_source = "RecoveryMasterCreation"
                else:
                    print(f"No RecoveryMasterCreation found, using ModelMaster data directly")
                    data_source = "ModelMaster"
            else:
                print(f"No ModelMaster found with plating_stk_no: {plating_stk_no}")
                
        # Handle batch_id parameter
        elif batch_id:
            print(f"🔍 Visual_AidView received batch_id: {batch_id}")
            batch_obj = RecoveryMasterCreation.objects.filter(batch_id=batch_id).first()
            if batch_obj:
                print(f"Found batch_obj by batch_id: {batch_obj}")
                model_master_obj = batch_obj.model_stock_no
                data_source = "RecoveryMasterCreation"

        # Populate context based on available data
        if batch_obj or model_master_obj:
            if data_source == "RecoveryMasterCreation" and batch_obj:
                # Use RecoveryMasterCreation data (preferred)
                images = get_model_view_images(batch_obj.images.all())
                image_urls = _build_image_url_list(
                    request,
                    images,
                    lookup_source='recovery_visual_aid.batch_images',
                )
                
                context.update({
                    'batch_id': batch_obj.batch_id,
                    'image_urls': image_urls,
                    'plating_stk_no': batch_obj.plating_stk_no,
                    'changes': batch_obj.changes,
                    'polish_finish': batch_obj.polish_finish,
                    'version': batch_obj.version.version_internal if batch_obj.version else None,
                    'data_source': 'ModelMasterCreation'
                })
                model_master_instance = batch_obj.model_stock_no
                
            elif data_source == "ModelMaster" and model_master_obj:
                # Use ModelMaster data directly
                images = get_model_view_images(model_master_obj.images.all())
                image_urls = _build_image_url_list(
                    request,
                    images,
                    lookup_source='recovery_visual_aid.model_master_images',
                )
                
                context.update({
                    'batch_id': None,
                    'image_urls': image_urls,
                    'plating_stk_no': model_master_obj.plating_stk_no,
                    'changes': getattr(model_master_obj, 'changes', 'N/A'),  # May not exist in ModelMaster
                    'polish_finish': model_master_obj.polish_finish,
                    'version': model_master_obj.version,
                    'brand': model_master_obj.brand,
                    'gender': model_master_obj.gender,
                    'ep_bath_type': model_master_obj.ep_bath_type,
                    'data_source': 'ModelMaster'
                })
                model_master_instance = model_master_obj

            print(f"Using {data_source} as data source")
            print(f"Context plating_stk_no: {context.get('plating_stk_no')}")

            # Get related versions and similar models
            if model_master_instance:
                # Get all ModelMaster objects with same model_no for variants
                masters = ModelMaster.objects.filter(model_no=model_master_instance.model_no)
                version_list = [m.version for m in masters if m.version]
                version_labels = [str(v) for v in version_list]
                context['modelmaster_versions'] = version_labels

                context['same_model_list'] = _build_similar_model_list(
                    model_master_instance,
                    creation_model=RecoveryMasterCreation,
                )
                print(f"Same model list with ModelMaster details: {context['same_model_list']}")
            else:
                context['same_model_list'] = []
                context['modelmaster_versions'] = []
        else:
            # Handle error cases
            if plating_stk_no:
                context['error'] = f"No ModelMaster found with plating_stk_no: {plating_stk_no}"
            elif batch_id:
                context['error'] = f"No ModelMasterCreation found for batch_id: {batch_id}"
            else:
                context['error'] = "Please provide either batch_id or plating_stk_no parameter"

        emit_image_event(
            request,
            'IMAGE.VISUAL_AID.END',
            'INFO',
            'Visual Aid image request completed',
            {
                'lookup_source': 'recovery_visual_aid',
                'duration_ms': image_duration_ms(visual_aid_started),
                'images_returned': len(context.get('image_urls', []) or []),
                'result': 'error' if context.get('error') else 'completed',
            },
        )
        return Response(context)

@method_decorator(login_required(login_url='login-api'), name='dispatch')
class Other_Visual_AidView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'ModelMaster/VisualAid.html'

    def get(self, request, batch_id=None, format=None):
        # Get parameters from URL or query string
        batch_id = batch_id or request.GET.get('batch_id')
        plating_stk_no = request.GET.get('plating_stk_no')
        model_no = request.GET.get('model_no')  # Add this new parameter

        context = {
            'user': request.user,
        }
        visual_aid_started = image_perf_counter()
        emit_image_event(
            request,
            'IMAGE.VISUAL_AID.START',
            'INFO',
            'Visual Aid image request started',
            {
                'lookup_source': 'other_visual_aid',
                'stock_hash': hash_value((plating_stk_no or batch_id or model_no or '').strip().upper(), prefix='stock')
                if (plating_stk_no or batch_id or model_no) else None,
            },
        )

        # Import required models
        from modelmasterapp.models import ModelMasterCreation, LookLikeModel, ModelMaster

        batch_obj = None
        model_master_obj = None
        data_source = None

        # Handle model_no parameter (NEW)
        if model_no:
            print(f"🔍 Visual_AidView received model_no: {model_no}")
            
            # Find ModelMaster by model_no (first match)
            model_master_obj = ModelMaster.objects.filter(model_no__startswith=model_no).first()
            print(f"Found ModelMaster by model_no: {model_master_obj}")
            
            if model_master_obj:
                # Try to find corresponding ModelMasterCreation
                batch_obj = ModelMasterCreation.objects.filter(model_stock_no=model_master_obj).first()
                
                if batch_obj:
                    print(f"Found batch_obj by ModelMaster: {batch_obj}")
                    data_source = "ModelMasterCreation"
                else:
                    print(f"No ModelMasterCreation found, using ModelMaster data directly")
                    data_source = "ModelMaster"
            else:
                print(f"No ModelMaster found with model_no: {model_no}")

        # Handle plating_stk_no parameter (EXISTING)
        elif plating_stk_no:
            print(f"🔍 Visual_AidView received plating_stk_no: {plating_stk_no}")
            
            # First, try to find ModelMasterCreation through ModelMaster
            model_master_obj = ModelMaster.objects.filter(plating_stk_no=plating_stk_no).first()
            print(f"Found ModelMaster by plating_stk_no: {model_master_obj}")
            
            if model_master_obj:
                # Try to find corresponding ModelMasterCreation
                batch_obj = ModelMasterCreation.objects.filter(model_stock_no=model_master_obj).first()
                
                if batch_obj:
                    print(f"Found batch_obj by ModelMaster: {batch_obj}")
                    data_source = "ModelMasterCreation"
                else:
                    print(f"No ModelMasterCreation found, using ModelMaster data directly")
                    data_source = "ModelMaster"
            else:
                print(f"No ModelMaster found with plating_stk_no: {plating_stk_no}")
                
        # Handle batch_id parameter (EXISTING)
        elif batch_id:
            print(f"🔍 Visual_AidView received batch_id: {batch_id}")
            batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
            if batch_obj:
                print(f"Found batch_obj by batch_id: {batch_obj}")
                model_master_obj = batch_obj.model_stock_no
                data_source = "ModelMasterCreation"

        # Populate context based on available data
        if batch_obj or model_master_obj:
            if data_source == "ModelMasterCreation" and batch_obj:
                # Use ModelMasterCreation data (preferred)
                images = get_model_view_images(batch_obj.images.all())
                image_urls = _build_image_url_list(
                    request,
                    images,
                    lookup_source='other_visual_aid.batch_images',
                )
                
                context.update({
                    'batch_id': batch_obj.batch_id,
                    'image_urls': image_urls,
                    'plating_stk_no': batch_obj.plating_stk_no,
                    'changes': batch_obj.changes,
                    'polish_finish': batch_obj.polish_finish,
                    'version': batch_obj.version.version_internal if batch_obj.version else None,
                    'data_source': 'ModelMasterCreation'
                })
                model_master_instance = batch_obj.model_stock_no
                
            elif data_source == "ModelMaster" and model_master_obj:
                # Use ModelMaster data directly
                images = get_model_view_images(model_master_obj.images.all())
                image_urls = _build_image_url_list(
                    request,
                    images,
                    lookup_source='other_visual_aid.model_master_images',
                )
                
                context.update({
                    'batch_id': None,
                    'image_urls': image_urls,
                    'plating_stk_no': model_master_obj.plating_stk_no,
                    'changes': getattr(model_master_obj, 'changes', 'N/A'),  # May not exist in ModelMaster
                    'polish_finish': model_master_obj.polish_finish,
                    'version': model_master_obj.version,
                    'brand': model_master_obj.brand,
                    'gender': model_master_obj.gender,
                    'ep_bath_type': model_master_obj.ep_bath_type,
                    'data_source': 'ModelMaster'
                })
                model_master_instance = model_master_obj

            print(f"Using {data_source} as data source")
            print(f"Context plating_stk_no: {context.get('plating_stk_no')}")

            # Get related versions and similar models
            if model_master_instance:
                # Get all ModelMaster objects with same model_no for variants
                masters = ModelMaster.objects.filter(model_no=model_master_instance.model_no)
                version_list = [m.version for m in masters if m.version]
                version_labels = [str(v) for v in version_list]
                context['modelmaster_versions'] = version_labels

                context['same_model_list'] = _build_similar_model_list(
                    model_master_instance,
                    creation_model=ModelMasterCreation,
                )
                print(f"Same model list with ModelMaster details: {context['same_model_list']}")
            else:
                context['same_model_list'] = []
                context['modelmaster_versions'] = []
        else:
            # Handle error cases
            if plating_stk_no:
                context['error'] = f"No ModelMaster found with plating_stk_no: {plating_stk_no}"
            elif batch_id:
                context['error'] = f"No ModelMasterCreation found for batch_id: {batch_id}"
            else:
                context['error'] = "Please provide either batch_id or plating_stk_no parameter"

        emit_image_event(
            request,
            'IMAGE.VISUAL_AID.END',
            'INFO',
            'Visual Aid image request completed',
            {
                'lookup_source': 'other_visual_aid',
                'duration_ms': image_duration_ms(visual_aid_started),
                'images_returned': len(context.get('image_urls', []) or []),
                'result': 'error' if context.get('error') else 'completed',
            },
        )
        return Response(context)



from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class DP_ViewmasterView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'ModelMaster/viewmasters.html'

    def get_paginated_data(self, queryset, page_param, items_per_page=10):
        """Helper method to paginate queryset"""
        paginator = Paginator(queryset, items_per_page)
        page = self.request.GET.get(page_param, 1)
        
        try:
            paginated_items = paginator.page(page)
        except PageNotAnInteger:
            paginated_items = paginator.page(1)
        except EmptyPage:
            paginated_items = paginator.page(paginator.num_pages)
        
        return paginated_items

    def get(self, request, format=None):
        # Check if this is an AJAX request for pagination
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return self.handle_ajax_pagination(request)
        
        # Fetch all data with pagination
        model_masters = self.get_paginated_data(ModelMaster.objects.all(), 'model_page')
        polish_finishes = self.get_paginated_data(PolishFinishType.objects.all(), 'polish_page')
        plating_colors = self.get_paginated_data(Plating_Color.objects.all(), 'plating_page')
        tray_types = self.get_paginated_data(TrayType.objects.all(), 'tray_page')
        locations = self.get_paginated_data(Location.objects.all(), 'vendor_page')
        model_images = self.get_paginated_data(ModelImage.objects.all(), 'images_page')
        tray_ids = self.get_paginated_data(TrayId.objects.all(), 'trayid_page')
        categories = self.get_paginated_data(Category.objects.all(), 'category_page')
        ip_rejections = self.get_paginated_data(IP_Rejection_Table.objects.all(), 'iprejection_page')
        nickel_rejections = self.get_paginated_data(Nickel_QC_Rejection_Table.objects.all(), 'nickelrejection_page')
        brassiqf_rejections = self.get_paginated_data(Brass_QC_Rejection_Table.objects.all(), 'brassiqf_page')

        context = {
            'model_masters': model_masters,
            'polish_finishes': polish_finishes,
            'plating_colors': plating_colors,
            'tray_types': tray_types,
            'locations': locations,
            'model_images': model_images,
            'tray_ids': tray_ids,
            'categories': categories,
            'ip_rejections': ip_rejections,
            'nickel_rejections': nickel_rejections,
            'brassiqf_rejections': brassiqf_rejections,
        }
        return Response(context)

    def handle_ajax_pagination(self, request):
        """Handle AJAX pagination requests"""
        tab_name = request.GET.get('tab')
        page = request.GET.get('page', 1)
        
        # Map tab names to models
        tab_mapping = {
            'model': ModelMaster.objects.all(),
            'polish': PolishFinishType.objects.all(),
            'plating': Plating_Color.objects.all(),
            'tray': TrayType.objects.all(),
            'vendor': Location.objects.all(),
            'images': ModelImage.objects.all(),
            'trayid': TrayId.objects.all(),
            'category': Category.objects.all(),
            'iprejection': IP_Rejection_Table.objects.all(),
            'nickelrejection': Nickel_QC_Rejection_Table.objects.all(),
            'brassiqf': Brass_QC_Rejection_Table.objects.all(),
        }
        
        if tab_name not in tab_mapping:
            return JsonResponse({'error': 'Invalid tab'}, status=400)
        
        queryset = tab_mapping[tab_name]
        
        # Paginate the data
        paginator = Paginator(queryset, 10)
        try:
            paginated_data = paginator.page(page)
        except PageNotAnInteger:
            paginated_data = paginator.page(1)
        except EmptyPage:
            paginated_data = paginator.page(paginator.num_pages)
        
        # Generate HTML for the specific tab
        html_data = self.generate_table_rows(tab_name, paginated_data)
        
        return JsonResponse({
            'html': html_data,
            'current_page': paginated_data.number,
            'total_pages': paginated_data.paginator.num_pages,
            'total_items': paginated_data.paginator.count,
            'has_previous': paginated_data.has_previous(),
            'has_next': paginated_data.has_next(),
            'previous_page': paginated_data.previous_page_number() if paginated_data.has_previous() else None,
            'next_page': paginated_data.next_page_number() if paginated_data.has_next() else None,
        })

    def generate_table_rows(self, tab_name, paginated_data):
        """Generate HTML table rows for specific tab data.

        Security: every user-entered value is passed through escape() so stored
        HTML (e.g. <script>, <img>) is displayed as plain text, never rendered.
        """
        html_rows = ""

        if tab_name == 'model':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox model-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.model_no)}</td>
                    <td>{escape(obj.plating_stk_no)}</td>
                    <td>{escape(obj.polish_finish.polish_finish) if obj.polish_finish else '-'}</td>
                    <td>{escape(obj.ep_bath_type)}</td>
                    <td>{escape(obj.version)}</td>
                    <td>{escape(obj.tray_type.tray_type) if obj.tray_type else '-'}</td>
                    <td>{escape(obj.tray_type.tray_capacity) if obj.tray_type else '-'}</td>
                </tr>
                """
        elif tab_name == 'polish':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox polish-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.polish_finish)}</td>
                    <td>{escape(obj.polish_internal)}</td>
                </tr>
                """
        elif tab_name == 'plating':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox plating-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.plating_color)}</td>
                    <td>{escape(obj.plating_color_internal)}</td>
                </tr>
                """
        elif tab_name == 'tray':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox tray-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.tray_type)}</td>
                    <td>{escape(obj.tray_capacity)}</td>
                </tr>
                """
        elif tab_name == 'vendor':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox vendor-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.location_name)}</td>
                </tr>
                """
        elif tab_name == 'images':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                image_name = escape(obj.master_image.name) if obj.master_image else '-'
                image_url = f'<a href="{escape(obj.master_image.url)}" target="_blank">{escape(obj.master_image.url)}</a>' if obj.master_image else '-'
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox images-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{image_name}</td>
                    <td>{image_url}</td>
                </tr>
                """
        elif tab_name == 'trayid':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox trayid-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.tray_id)}</td>
                    <td>{escape(obj.tray_type)}</td>
                    <td>{escape(obj.tray_capacity)}</td>
                </tr>
                """
        elif tab_name == 'category':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox category-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.category_name)}</td>
                </tr>
                """
        elif tab_name == 'iprejection':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox iprejection-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.rejection_reason_id)}</td>
                    <td>{escape(obj.rejection_reason)}</td>
                </tr>
                """
        elif tab_name == 'brassiqf':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox brassiqf-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.rejection_reason_id)}</td>
                    <td>{escape(obj.rejection_reason)}</td>
                </tr>
                """
        elif tab_name == 'nickelrejection':
            for i, obj in enumerate(paginated_data, start=paginated_data.start_index()):
                html_rows += f"""
                <tr>
                    <td><input type="checkbox" class="select-checkbox nickelrejection-checkbox" name="selected_ids" value="{obj.id}"></td>
                    <td>{i}</td>
                    <td>{obj.date_time.strftime('%Y-%m-%d')}</td>
                    <td>{escape(obj.rejection_reason)}</td>
                </tr>
                """
        
        if not html_rows:
            # Return appropriate empty message based on tab
            col_count = {
                'model': 10, 'polish': 5, 'plating': 5, 'tray': 5, 'vendor': 4,
                'images': 5, 'trayid': 6, 'category': 4, 'iprejection': 6,
                'brassiqf': 5, 'nickelrejection': 4
            }
            html_rows = f'<tr><td colspan="{col_count.get(tab_name, 5)}">No records found.</td></tr>'
        
        return html_rows

    def post(self, request, format=None):
        """Handle deletion of selected items"""
        try:
            action = request.POST.get('action')
            if action != 'delete':
                return JsonResponse({'success': False, 'error': 'Invalid action'})

            tab_name = request.POST.get('tab_name')
            selected_ids = request.POST.getlist('selected_ids')

            if not selected_ids:
                return JsonResponse({'success': False, 'error': 'No items selected'})

            # Map tab names to models
            model_mapping = {
                'model': ModelMaster,
                'polish': PolishFinishType,
                'plating': Plating_Color,
                'tray': TrayType,
                'vendor': Location,
                'images': ModelImage,
                'trayid': TrayId,
                'category': Category,
                'iprejection': IP_Rejection_Table,  
                'brassiqf': Brass_QC_Rejection_Table,
                'nickelrejection': Nickel_QC_Rejection_Table,
            }

            if tab_name not in model_mapping:
                return JsonResponse({'success': False, 'error': 'Invalid tab name'})

            model_class = model_mapping[tab_name]
            
            # Delete selected items
            deleted_count = 0
            for item_id in selected_ids:
                try:
                    if tab_name == 'brassiqf':
                        # Delete from all Brass/IQF tables by rejection_reason_id
                        obj = get_object_or_404(Brass_QC_Rejection_Table, id=item_id)
                        reason_id = obj.rejection_reason_id
                        obj.delete()
                        Brass_QC_Rejection_Table.objects.filter(rejection_reason_id=reason_id).delete()  
                        Brass_Audit_Rejection_Table.objects.filter(rejection_reason_id=reason_id).delete()
                        IQF_Rejection_Table.objects.filter(rejection_reason_id=reason_id).delete()
                        deleted_count += 1
                    elif tab_name == 'nickelrejection':
                        # Delete from both Nickel tables by rejection_reason text
                        obj = get_object_or_404(Nickel_QC_Rejection_Table, id=item_id)
                        reason_text = obj.rejection_reason
                        obj.delete()
                        Nickel_Audit_Rejection_Table.objects.filter(rejection_reason=reason_text).delete()
                        deleted_count += 1
                    else:
                        item = get_object_or_404(model_class, id=item_id)
                        item.delete()
                        deleted_count += 1
                except Exception as e:
                    logger.error(f"Error deleting item {item_id}: {str(e)}", exc_info=True)
                    continue

            return JsonResponse({
                'success': True, 
                'deleted_count': deleted_count,
                'message': f'Successfully deleted {deleted_count} item(s)'
            })

        except Exception as e:
            return JsonResponse({
                'success': False, 
                'error': 'Unable to process the request. Please verify the submitted data and try again.'
            })

@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class DP_ModelmasterView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'ModelMaster/dp_modelmaster.html'

    def get(self, request, format=None):
        # Get all data for dropdowns and existing records
        context = {
            'polish_finishes': PolishFinishType.objects.all(),
            'plating_colors': Plating_Color.objects.all(),
            'tray_types': TrayType.objects.all(),
            'vendors': Vendor.objects.all(),
            'model_images': ModelImage.objects.all(),
            'model_masters': ModelMaster.objects.all(),
            'versions': Version.objects.all(),
            # Add categories for dropdown
            'categories': Category.objects.all(),
        }
        return Response(context)

    def post(self, request, format=None):
        # Handle Category form submission
        if 'category_name' in request.data:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = CategorySerializer(data=data)
            if serializer.is_valid():
                category = serializer.save()
                # Redirect to same page with category_name in query params
                from django.shortcuts import redirect
                return redirect(f"/adminportal/dp_modelmaster/?category_name={category.category_name}")
            else:
                # Re-render page with errors
                context = {
                    'polish_finishes': PolishFinishType.objects.all(),
                    'plating_colors': Plating_Color.objects.all(),
                    'tray_types': TrayType.objects.all(),
                    'vendors': Vendor.objects.all(),
                    'model_images': ModelImage.objects.all(),
                    'model_masters': ModelMaster.objects.all(),
                    'versions': Version.objects.all(),
                    'categories': Category.objects.all(),
                    'category_form_errors': serializer.errors,
                }
                return Response(context)


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class PolishFinishAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Polish Finish types"""
        polish_finishes = PolishFinishType.objects.all()
        serializer = PolishFinishTypeSerializer(polish_finishes, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Polish Finish type"""
        try:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = PolishFinishTypeSerializer(data=data)
            if serializer.is_valid():
                polish_finish = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Polish finish created successfully!',
                    'data': PolishFinishTypeSerializer(polish_finish).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Polish Finish type"""
        try:
            polish_finish = get_object_or_404(PolishFinishType, pk=pk)
            serializer = PolishFinishTypeSerializer(polish_finish, data=request.data)
            if serializer.is_valid():
                updated_polish_finish = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Polish finish updated successfully!',
                    'data': PolishFinishTypeSerializer(updated_polish_finish).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Polish Finish type"""
        try:
            polish_finish = get_object_or_404(PolishFinishType, pk=pk)
            polish_finish.delete()
            return Response({
                'success': True,
                'message': 'Polish finish deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class PlatingColorAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Plating Colors"""
        plating_colors = Plating_Color.objects.all()
        serializer = PlatingColorSerializer(plating_colors, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Plating Color"""
        try:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = PlatingColorSerializer(data=data)
            if serializer.is_valid():
                plating_color = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Plating color created successfully!',
                    'data': PlatingColorSerializer(plating_color).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Plating Color"""
        try:
            plating_color = get_object_or_404(Plating_Color, pk=pk)
            serializer = PlatingColorSerializer(plating_color, data=request.data)
            if serializer.is_valid():
                updated_plating_color = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Plating color updated successfully!',
                    'data': PlatingColorSerializer(updated_plating_color).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Plating Color"""
        try:
            plating_color = get_object_or_404(Plating_Color, pk=pk)
            plating_color.delete()
            return Response({
                'success': True,
                'message': 'Plating color deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class JigCapacityAPIView(APIView):
    """Read-only listing of Jig Loading Master records for the Model Master 'Jig Capacity' tab."""
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Jig Capacity master records (jig_type, jig_capacity, forging_info per model)"""
        jig_masters = JigLoadingMaster.objects.select_related('model_stock_no').order_by(
            'model_stock_no__model_no'
        )
        serializer = JigCapacitySerializer(jig_masters, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def put(self, request, pk):
        """Update an existing Jig Capacity master record"""
        try:
            jig_master = get_object_or_404(JigLoadingMaster, pk=pk)
            serializer = JigCapacitySerializer(jig_master, data=request.data)
            if serializer.is_valid():
                updated_jig_master = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Jig capacity updated successfully!',
                    'data': JigCapacitySerializer(updated_jig_master).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class TrayTypeAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Tray Types"""
        tray_types = TrayType.objects.all()
        serializer = TrayTypeSerializer(tray_types, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Tray Type"""
        try:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = TrayTypeSerializer(data=data)
            if serializer.is_valid():
                tray_type = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Tray type created successfully!',
                    'data': TrayTypeSerializer(tray_type).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Tray Type"""
        try:
            tray_type = get_object_or_404(TrayType, pk=pk)
            serializer = TrayTypeSerializer(tray_type, data=request.data)
            if serializer.is_valid():
                updated_tray_type = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Tray type updated successfully!',
                    'data': TrayTypeSerializer(updated_tray_type).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Tray Type"""
        try:
            tray_type = get_object_or_404(TrayType, pk=pk)
            tray_type.delete()
            return Response({
                'success': True,
                'message': 'Tray type deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class ModelImageAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Model Images"""
        model_images = ModelImage.objects.all()
        serializer = ModelImageSerializer(model_images, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    # Allowed image MIME types and extensions (Issue #24)
    _ALLOWED_IMAGE_MIME = frozenset({
        'image/jpeg', 'image/png', 'image/gif',
    })
    _ALLOWED_IMAGE_EXT = frozenset({
        '.jpg', '.jpeg', '.png', '.gif',
    })
    # Dangerous intermediate extensions that must not appear anywhere in the filename
    _DANGEROUS_EXT = frozenset({
        '.exe', '.php', '.sh', '.bat', '.cmd', '.ps1', '.js', '.py',
        '.rb', '.pl', '.asp', '.aspx', '.jsp', '.cgi', '.dll', '.so',
    })

    @staticmethod
    def _validate_image_file(image):
        """Returns an error string, or None if the file is acceptable."""
        import os
        from PIL import Image, UnidentifiedImageError

        name = image.name or ''
        _, ext = os.path.splitext(name.lower())
        size = getattr(image, 'size', None)
        # Block dangerous intermediate extensions (e.g. sample.exe.png)
        stem = os.path.splitext(name)[0].lower()
        for dext in ModelImageAPIView._DANGEROUS_EXT:
            if stem.endswith(dext) or f'{dext}.' in stem:
                return f'File "{name}" contains a disallowed intermediate extension.'
        if ext not in ModelImageAPIView._ALLOWED_IMAGE_EXT:
            return f'Unsupported format "{ext or "(none)"}"; use JPG, JPEG, PNG, or GIF.'
        content_type = getattr(image, 'content_type', '') or ''
        if content_type and content_type not in ModelImageAPIView._ALLOWED_IMAGE_MIME:
            return f'File type "{content_type}" is not allowed. Only image files are accepted.'
        if size is not None and size <= 0:
            return 'Invalid or damaged image file: the file is empty.'
        if size is not None and size > settings.MODEL_IMAGE_MAX_UPLOAD_SIZE:
            max_mb = settings.MODEL_IMAGE_MAX_UPLOAD_SIZE / (1024 * 1024)
            return f'Image exceeds maximum allowed size of {max_mb:g} MB.'

        try:
            image.seek(0)
            with Image.open(image) as opened_image:
                detected_format = (opened_image.format or '').upper()
                if detected_format == 'MPO':
                    detected_format = 'JPEG'
                allowed_by_ext = {
                    '.jpg': 'JPEG',
                    '.jpeg': 'JPEG',
                    '.png': 'PNG',
                    '.gif': 'GIF',
                }
                expected_format = allowed_by_ext.get(ext)
                if detected_format != expected_format:
                    return (
                        f'File extension "{ext}" does not match detected '
                        f'image format "{detected_format or "unknown"}".'
                    )
                opened_image.verify()
        except UnidentifiedImageError as exc:
            return f'Invalid or damaged image file: {exc}'
        except Exception as exc:
            return f'Invalid or damaged image file: {exc}'
        finally:
            try:
                image.seek(0)
            except Exception:
                pass

        return None

    @staticmethod
    def _flatten_serializer_errors(errors):
        if not errors:
            return 'Invalid image file.'
        if isinstance(errors, dict):
            messages = []
            for field, value in errors.items():
                if isinstance(value, (list, tuple)):
                    detail = '; '.join(str(item) for item in value)
                else:
                    detail = str(value)
                messages.append(f'{field}: {detail}')
            return ' | '.join(messages) or 'Invalid image file.'
        return str(errors)

    @staticmethod
    def _log_upload_diagnostic(request, image, field_name, validation_error=None):
        import os

        filename = os.path.basename(getattr(image, 'name', '') or '')
        _, ext = os.path.splitext(filename.lower())
        diagnostic = {
            'field_name': field_name,
            'filename': filename,
            'extension': ext,
            'content_type': getattr(image, 'content_type', '') or '',
            'size': getattr(image, 'size', None),
        }
        if validation_error:
            diagnostic.update({
                'validation_exception_class': validation_error.__class__.__name__,
                'validation_exception_message': str(validation_error),
            })
            logger.warning('Model image upload rejected: %s', diagnostic)
        else:
            logger.info('Model image upload received: %s', diagnostic)

    def post(self, request):
        """Upload new Model Images"""
        try:
            # Handle multiple image uploads
            uploaded_images = []
            failed_images = []

            if 'images' in request.FILES:
                images = request.FILES.getlist('images')
                if not images:
                    return Response({
                        'success': False,
                        'message': 'No images provided',
                        'uploaded_count': 0,
                        'failed_count': 0,
                        'uploaded': [],
                        'failed': [],
                        'data': [],
                    }, status=status.HTTP_400_BAD_REQUEST)

                for image in images:
                    original_filename = os.path.basename(image.name or '').strip()
                    self._log_upload_diagnostic(request, image, 'images')

                    # --- File type validation (Issue #24) ---
                    err = self._validate_image_file(image)
                    if err:
                        failed_images.append({
                            'filename': original_filename,
                            'reason': err,
                        })
                        self._log_upload_diagnostic(
                            request,
                            image,
                            'images',
                            validation_error=ValueError(err),
                        )
                        continue

                    image_data = {
                        'master_image': image,
                        'original_filename': original_filename,
                        'date_time': timezone.now(),
                        'createdby': request.user.pk,
                    }
                    serializer = ModelImageSerializer(data=image_data)
                    if serializer.is_valid():
                        try:
                            image.seek(0)
                            write_started = image_perf_counter()
                            model_image = serializer.save()
                            _link_model_image_to_matching_masters(model_image)
                            emit_media_write(
                                request,
                                image,
                                image_duration_ms(write_started),
                                result='saved',
                            )
                            uploaded_images.append({
                                **ModelImageSerializer(model_image).data,
                                'filename': original_filename,
                            })
                        except Exception as exc:
                            emit_image_error(request, 'model_image_upload_file', exc)
                            logger.exception(
                                'ModelImage upload failed for filename=%s',
                                original_filename,
                            )
                            failed_images.append({
                                'filename': original_filename,
                                'reason': str(exc) or 'Unable to save image.',
                            })
                    else:
                        reason = self._flatten_serializer_errors(serializer.errors)
                        failed_images.append({
                            'filename': original_filename,
                            'reason': reason,
                            'errors': serializer.errors,
                        })
                        self._log_upload_diagnostic(
                            request,
                            image,
                            'images',
                            validation_error=ValueError(reason),
                        )

                uploaded_count = len(uploaded_images)
                failed_count = len(failed_images)
                if uploaded_count and failed_count:
                    message = (
                        f'{uploaded_count} image(s) uploaded successfully. '
                        f'{failed_count} image(s) failed.'
                    )
                    response_status = status.HTTP_207_MULTI_STATUS
                elif uploaded_count:
                    message = f'{uploaded_count} image(s) uploaded successfully!'
                    response_status = status.HTTP_201_CREATED
                else:
                    message = 'No images were uploaded.'
                    response_status = status.HTTP_400_BAD_REQUEST

                return Response({
                    'success': bool(uploaded_count),
                    'message': message,
                    'uploaded_count': uploaded_count,
                    'failed_count': failed_count,
                    'uploaded': [
                        {
                            'filename': item.get('filename') or item.get('original_filename') or '',
                            'id': item.get('id'),
                        }
                        for item in uploaded_images
                    ],
                    'failed': failed_images,
                    'data': uploaded_images,
                }, status=response_status)
            else:
                return Response({
                    'success': False,
                    'message': 'No images provided',
                    'uploaded_count': 0,
                    'failed_count': 0,
                    'uploaded': [],
                    'failed': [],
                    'data': [],
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            emit_image_error(request, 'model_image_upload', e)
            logger.exception('ModelImageAPIView.post upload error')
            return Response({
                'success': False,
                'message': 'An internal error occurred while uploading. Please contact the administrator.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Model Image"""
        try:
            model_image = get_object_or_404(ModelImage, pk=pk)
            # Delete the actual file
            if model_image.master_image:
                delete_started = image_perf_counter()
                file_field = model_image.master_image
                model_image.master_image.delete()
                emit_media_delete(
                    request,
                    file_field,
                    image_duration_ms(delete_started),
                    result='file_deleted',
                )
            model_image.delete()
            return Response({
                'success': True,
                'message': 'Model image deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            emit_image_error(request, 'model_image_delete', e)
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class ModelMasterAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Model Masters"""
        from Jig_Loading.models import ModelMicroGroup
        model_masters = ModelMaster.objects.select_related('polish_finish', 'tray_type', 'plating_color').all()
        grouped_psns = set(ModelMicroGroup.objects.filter(is_active=True).values_list('plating_stk_no', flat=True))
        serializer = ModelMasterSerializer(model_masters, many=True, context={'grouped_psns': grouped_psns})
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Model Master"""
        try:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = ModelMasterSerializer(data=data)
            if serializer.is_valid():
                model_master = serializer.save()
                
                # Handle many-to-many relationship for images
                if 'images' in request.data:
                    image_ids = request.data.getlist('images') if hasattr(request.data, 'getlist') else request.data.get('images', [])
                    if image_ids:
                        model_master.images.set(image_ids)
                
                return Response({
                    'success': True,
                    'message': 'Model master created successfully!',
                    'data': ModelMasterSerializer(model_master).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Model Master"""
        try:
            model_master = get_object_or_404(ModelMaster, pk=pk)
            serializer = ModelMasterSerializer(model_master, data=request.data)
            if serializer.is_valid():
                updated_model_master = serializer.save()
                
                # Handle many-to-many relationship for images
                if 'images' in request.data:
                    image_ids = request.data.getlist('images') if hasattr(request.data, 'getlist') else request.data.get('images', [])
                    updated_model_master.images.set(image_ids)
                
                return Response({
                    'success': True,
                    'message': 'Model master updated successfully!',
                    'data': ModelMasterSerializer(updated_model_master).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Model Master"""
        try:
            model_master = get_object_or_404(ModelMaster, pk=pk)
            model_master.delete()
            return Response({
                'success': True,
                'message': 'Model master deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


MODEL_MASTER_BULK_UPLOAD_HEADERS = [
    'model_no', 'plating_stk_no', 'polish_finish', 'ep_bath_type', 'tray_type', 'tray_capacity', 'version',
]

MODEL_MASTER_BULK_UPLOAD_HEADER_LABELS = [
    'Model No', 'Plating Stock Number', 'Polish Finish', 'EP Bath Type', 'Tray Type', 'Tray Code', 'Tray Capacity', 'Version',
]


@login_required(login_url='login-api')
@require_admin
def model_master_bulk_upload_template(request):
    """Return a downloadable .xlsx template for Model Master bulk upload."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = 'Model Master Template'
    ws.append(MODEL_MASTER_BULK_UPLOAD_HEADER_LABELS)

    for col_idx in range(1, len(MODEL_MASTER_BULK_UPLOAD_HEADER_LABELS) + 1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = 26

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="model_master_bulk_upload_template.xlsx"'
    return response


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class ModelMasterBulkUploadAPIView(APIView):
    """Bulk-create Model Master records from an uploaded .xlsx/.csv file."""
    renderer_classes = [JSONRenderer]

    def _read_rows(self, uploaded_file):
        name = (uploaded_file.name or '').lower()
        if name.endswith('.csv'):
            decoded = uploaded_file.read().decode('utf-8-sig')
            reader = csv.reader(io.StringIO(decoded))
            rows = list(reader)
        elif name.endswith('.xlsx'):
            from openpyxl import load_workbook
            wb = load_workbook(uploaded_file, data_only=True)
            ws = wb.active
            rows = [[cell if cell is not None else '' for cell in row] for row in ws.iter_rows(values_only=True)]
        else:
            raise ValueError('Unsupported file type. Please upload a .xlsx or .csv file.')

        if not rows:
            return []

        header_aliases = {'plating_stock_number': 'plating_stk_no'}
        headers = [re.sub(r'\s+', '_', str(h).strip().lower()) for h in rows[0]]
        headers = [header_aliases.get(header, header) for header in headers]
        data_rows = []
        for raw_row in rows[1:]:
            row_dict = {}
            for idx, header in enumerate(headers):
                row_dict[header] = str(raw_row[idx]).strip() if idx < len(raw_row) and raw_row[idx] is not None else ''
            data_rows.append(row_dict)
        return data_rows

    def post(self, request):
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({
                'success': False,
                'message': 'Please select a file to upload.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            data_rows = self._read_rows(uploaded_file)
        except Exception as e:
            return Response({
                'success': False,
                'message': str(e) if isinstance(e, ValueError) else 'Unable to read the uploaded file. Please use the provided template.'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not data_rows:
            return Response({
                'success': False,
                'message': 'The uploaded file has no data rows.'
            }, status=status.HTTP_400_BAD_REQUEST)

        created_count = 0
        row_errors = []

        for offset, row in enumerate(data_rows):
            row_number = offset + 2  # account for header row, 1-indexed
            model_no = (row.get('model_no') or '').strip()
            plating_stk_no = (row.get('plating_stk_no') or '').strip()
            polish_finish_name = (row.get('polish_finish') or '').strip()
            ep_bath_type = (row.get('ep_bath_type') or '').strip()
            tray_type_name = (row.get('tray_type') or '').strip()
            tray_code_raw = (row.get('tray_code') or '').strip().upper()
            tray_capacity_raw = (row.get('tray_capacity') or '').strip()
            version = (row.get('version') or '').strip()

            if not model_no and not plating_stk_no and not tray_type_name and not version:
                continue  # skip fully blank rows

            if not model_no or not plating_stk_no or not version or not tray_type_name:
                row_errors.append({'row': row_number, 'message': 'Model No, Plating Stock Number, Version, and Tray Type are required.'})
                continue

            polish_finish_obj = None
            if polish_finish_name:
                polish_finish_obj = PolishFinishType.objects.filter(polish_finish__iexact=polish_finish_name).first()
                if not polish_finish_obj:
                    row_errors.append({'row': row_number, 'message': f'Polish Finish "{polish_finish_name}" was not found.'})
                    continue

            tray_type_obj = TrayType.objects.filter(tray_type__iexact=tray_type_name).first()
            if not tray_type_obj:
                row_errors.append({'row': row_number, 'message': f'Tray Type "{tray_type_name}" was not found.'})
                continue

            tray_code = None
            if tray_code_raw:
                valid_tray_codes = {choice[0] for choice in TRAY_CODE_CHOICES}
                if tray_code_raw not in valid_tray_codes:
                    row_errors.append({'row': row_number, 'message': f'Tray Code "{tray_code_raw}" is invalid. Allowed values: {", ".join(sorted(valid_tray_codes))}.'})
                    continue
                tray_type_key = tray_type_obj.tray_type.strip().lower()
                if tray_type_key == 'normal' and tray_code_raw not in NORMAL_TRAY_CODES:
                    row_errors.append({'row': row_number, 'message': f'Tray Code "{tray_code_raw}" does not match Tray Type "Normal". Allowed: {", ".join(sorted(NORMAL_TRAY_CODES))}.'})
                    continue
                if tray_type_key == 'jumbo' and tray_code_raw not in JUMBO_TRAY_CODES:
                    row_errors.append({'row': row_number, 'message': f'Tray Code "{tray_code_raw}" does not match Tray Type "Jumbo". Allowed: {", ".join(sorted(JUMBO_TRAY_CODES))}.'})
                    continue
                tray_code = tray_code_raw

            tray_capacity = None
            if tray_capacity_raw:
                try:
                    tray_capacity = int(float(tray_capacity_raw))
                except ValueError:
                    row_errors.append({'row': row_number, 'message': f'Tray Capacity "{tray_capacity_raw}" is not a valid number.'})
                    continue
            else:
                tray_capacity = tray_type_obj.tray_capacity

            payload = {
                'model_no': model_no,
                'plating_stk_no': plating_stk_no,
                'polish_finish': polish_finish_obj.id if polish_finish_obj else None,
                'ep_bath_type': ep_bath_type,
                'tray_type': tray_type_obj.id,
                'tray_code': tray_code,
                'tray_capacity': tray_capacity,
                'version': version,
                'date_time': timezone.now(),
            }

            serializer = ModelMasterSerializer(data=payload)
            if serializer.is_valid():
                serializer.save()
                created_count += 1
            else:
                row_errors.append({'row': row_number, 'message': formatValidationErrorsForBulk(serializer.errors)})

        total_rows = created_count + len(row_errors)
        return Response({
            'success': created_count > 0,
            'message': f'{created_count} of {total_rows} model master row(s) created successfully.',
            'created': created_count,
            'total': total_rows,
            'errors': row_errors,
        }, status=status.HTTP_200_OK if created_count > 0 or total_rows == 0 else status.HTTP_400_BAD_REQUEST)


def formatValidationErrorsForBulk(errors):
    parts = []
    for field, messages in errors.items():
        if isinstance(messages, list):
            parts.append(f"{field}: {'; '.join(str(m) for m in messages)}")
        else:
            parts.append(f"{field}: {messages}")
    return ' | '.join(parts) if parts else 'Validation failed.'


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class ModelGroupManageAPIView(APIView):
    """
    Manage Jig Loading's ModelMicroGroup combinations (group_name <-> plating_stk_no)
    from Model Master, so admins can view/reorganize them without Django admin.
    Single action-based endpoint, consistent with TrayIdAPI's /api/tray/manage/ pattern.
    """
    renderer_classes = [JSONRenderer]

    def post(self, request):
        from Jig_Loading.models import ModelMicroGroup
        action = request.data.get('action')
        try:
            if action == 'list':
                return self._list(ModelMicroGroup)
            elif action == 'move':
                return self._move(request, ModelMicroGroup)
            elif action == 'rename':
                return self._rename(request, ModelMicroGroup)
            elif action == 'remove':
                return self._remove(request, ModelMicroGroup)
            return Response({'success': False, 'message': 'Unknown action.'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("ModelGroupManageAPIView error")
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _list(self, ModelMicroGroup):
        psn_to_model_no = {}
        for m in ModelMaster.objects.exclude(plating_stk_no__isnull=True).exclude(plating_stk_no='').only('plating_stk_no', 'model_no'):
            psn_to_model_no.setdefault(m.plating_stk_no, m.model_no)

        groups = {}
        grouped_psns = set()
        for entry in ModelMicroGroup.objects.filter(is_active=True).order_by('group_name', 'plating_stk_no'):
            groups.setdefault(entry.group_name, []).append({
                'plating_stk_no': entry.plating_stk_no,
                'model_no': psn_to_model_no.get(entry.plating_stk_no, entry.plating_stk_no),
            })
            grouped_psns.add(entry.plating_stk_no)

        ungrouped = [
            {'plating_stk_no': psn, 'model_no': model_no}
            for psn, model_no in psn_to_model_no.items()
            if psn not in grouped_psns
        ]
        ungrouped.sort(key=lambda item: item['plating_stk_no'])

        return Response({
            'success': True,
            'groups': [{'group_name': name, 'models': members} for name, members in sorted(groups.items())],
            'ungrouped': ungrouped,
        })

    def _move(self, request, ModelMicroGroup):
        plating_stk_no = (request.data.get('plating_stk_no') or '').strip()
        group_name = (request.data.get('group_name') or '').strip()
        if not plating_stk_no:
            return Response({'success': False, 'message': 'plating_stk_no is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if group_name and not ModelMaster.objects.filter(plating_stk_no=plating_stk_no).exists():
            return Response({
                'success': False,
                'message': f'"{plating_stk_no}" does not match any existing Model Master record. Pick a model from the dropdown.'
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            ModelMicroGroup.objects.filter(plating_stk_no=plating_stk_no, is_active=True).update(is_active=False)
            if group_name:
                ModelMicroGroup.objects.update_or_create(
                    group_name=group_name,
                    plating_stk_no=plating_stk_no,
                    defaults={'is_active': True},
                )
        return Response({
            'success': True,
            'message': f'{plating_stk_no} moved to "{group_name}".' if group_name else f'{plating_stk_no} removed from its group.'
        })

    def _rename(self, request, ModelMicroGroup):
        old_name = (request.data.get('old_group_name') or '').strip()
        new_name = (request.data.get('new_group_name') or '').strip()
        if not old_name or not new_name:
            return Response({'success': False, 'message': 'old_group_name and new_group_name are required.'}, status=status.HTTP_400_BAD_REQUEST)
        if old_name == new_name:
            return Response({'success': True, 'message': 'No change.'})
        if ModelMicroGroup.objects.filter(group_name=new_name).exists():
            return Response({'success': False, 'message': f'Group "{new_name}" already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        updated = ModelMicroGroup.objects.filter(group_name=old_name).update(group_name=new_name)
        if updated == 0:
            return Response({'success': False, 'message': f'Group "{old_name}" not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'message': f'Renamed "{old_name}" to "{new_name}".'})

    def _remove(self, request, ModelMicroGroup):
        plating_stk_no = (request.data.get('plating_stk_no') or '').strip()
        if not plating_stk_no:
            return Response({'success': False, 'message': 'plating_stk_no is required.'}, status=status.HTTP_400_BAD_REQUEST)
        ModelMicroGroup.objects.filter(plating_stk_no=plating_stk_no, is_active=True).update(is_active=False)
        return Response({'success': True, 'message': f'{plating_stk_no} removed from its group.'})


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class LocationAPIView(APIView):
    renderer_classes = [JSONRenderer]
    throttle_classes = [UserRateThrottle]

    def get(self, request):
        """Get all Locations"""
        locations = Location.objects.all()
        serializer = LocationSerializer(locations, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Location"""
        try:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = LocationSerializer(data=data)
            if serializer.is_valid():
                location = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Location created successfully!',
                    'data': LocationSerializer(location).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Location"""
        try:
            location = get_object_or_404(Location, pk=pk)
            serializer = LocationSerializer(location, data=request.data)
            if serializer.is_valid():
                updated_location = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Location updated successfully!',
                    'data': LocationSerializer(updated_location).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Location"""
        try:
            location = get_object_or_404(Location, pk=pk)
            location.delete()
            return Response({
                'success': True,
                'message': 'Location deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class TrayIdAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Tray IDs"""
        tray_ids = TrayId.objects.all()
        serializer = TrayIdSerializer(tray_ids, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Tray ID"""
        try:
            # Convert tray_type to pk if needed (from string to int)
            data = request.data.copy()
            data['date'] = timezone.now()  # Add datetime
            
            if isinstance(data.get('tray_type'), str) and data.get('tray_type').isdigit():
                data['tray_type'] = int(data['tray_type'])
            serializer = TrayIdSerializer(data=data)
            if serializer.is_valid():
                tray_id = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Tray ID created successfully!',
                    'data': TrayIdSerializer(tray_id).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Tray ID"""
        try:
            tray_id_obj = get_object_or_404(TrayId, pk=pk)
            data = request.data.copy()
            if isinstance(data.get('tray_type'), str) and data.get('tray_type').isdigit():
                data['tray_type'] = int(data['tray_type'])
            serializer = TrayIdSerializer(tray_id_obj, data=data)
            if serializer.is_valid():
                updated_tray_id = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Tray ID updated successfully!',
                    'data': TrayIdSerializer(updated_tray_id).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Consolidated Tray Management API ─────────────────────────────────────────
TRAY_FORMAT_PATTERN = re.compile(r'^(JB-A|JR-A|JD-A|JL-A|NB-A|NR-A|ND-A|NL-A)\d{5}$')

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class TrayManageAPIView(APIView):
    """
    Single consolidated API for all Tray ID management operations.
    POST /api/tray/manage/
    Actions: add, delete, delete_all, list
    """
    renderer_classes = [JSONRenderer]

    def post(self, request):
        action = request.data.get('action', '').strip().lower()
        tray_ids = request.data.get('tray_ids', [])
        force_delete = request.data.get('force_delete', False)

        logger.info(f"[TrayManage] action={action}, tray_ids={tray_ids}, force_delete={force_delete}, user={request.user}")

        if action == 'list':
            return self._list_trays(request)
        elif action == 'add':
            return self._add_trays(request, tray_ids)
        elif action == 'delete':
            return self._delete_trays(request, tray_ids, force_delete)
        elif action == 'delete_all':
            return self._delete_all_trays(request, force_delete)
        elif action == 'restore':
            return self._restore_trays(request, tray_ids)
        else:
            return Response({
                'status': 'error',
                'message': f'Invalid action: "{action}". Valid actions: add, delete, delete_all, list, restore',
                'data': [],
                'conflicts': []
            }, status=status.HTTP_400_BAD_REQUEST)

    def _list_trays(self, request):
        """List all tray IDs with their status."""
        trays = TrayId.objects.all().order_by('tray_id')
        data = []
        for t in trays:
            is_occupied = bool(t.batch_id_id) or bool(t.lot_id)
            data.append({
                'id': t.pk,
                'tray_id': t.tray_id,
                'tray_type': t.tray_type or '',
                'tray_capacity': t.tray_capacity,
                'is_occupied': is_occupied,
                'lot_id': t.lot_id or '',
                'created': t.date.strftime('%Y-%m-%d %H:%M') if t.date else '',
            })
        logger.info(f"[TrayManage] Listed {len(data)} trays")
        return Response({
            'status': 'success',
            'message': f'{len(data)} tray(s) found',
            'data': data,
            'conflicts': []
        })

    def _add_trays(self, request, tray_ids):
        """Add one or more tray IDs with full backend validation."""
        if not tray_ids or not isinstance(tray_ids, list):
            return Response({
                'status': 'error',
                'message': 'tray_ids must be a non-empty list.',
                'data': [],
                'conflicts': []
            }, status=status.HTTP_400_BAD_REQUEST)

        # Validate all tray IDs first
        errors = []
        valid_trays = []
        duplicates_in_request = set()

        for idx, raw_id in enumerate(tray_ids):
            tid = str(raw_id).strip().upper()

            # Format validation
            if not TRAY_FORMAT_PATTERN.match(tid):
                errors.append(f'"{raw_id}" has invalid format. Expected prefix (JB-A, JR-A, JD-A, JL-A, NB-A, NR-A, ND-A, NL-A) + 5 digits.')
                continue

            # Duplicate within same request
            if tid in duplicates_in_request:
                errors.append(f'"{tid}" is duplicated in this request.')
                continue
            duplicates_in_request.add(tid)

            # Duplicate in database
            if TrayId.objects.filter(tray_id__iexact=tid).exists():
                errors.append(f'Tray ID "{tid}" already exists.')
                continue

            valid_trays.append(tid)

        if errors and not valid_trays:
            logger.warning(f"[TrayManage] All trays rejected: {errors}")
            return Response({
                'status': 'error',
                'message': 'All tray IDs failed validation.',
                'data': [],
                'conflicts': errors
            }, status=status.HTTP_400_BAD_REQUEST)

        # Determine tray_type and capacity from prefix
        created = []
        with transaction.atomic():
            for tid in valid_trays:
                prefix = tid[0]  # J or N
                if prefix == 'J':
                    tray_type_name = 'Jumbo'
                else:
                    tray_type_name = 'Normal'

                # Look up TrayType for capacity
                tray_type_obj = TrayType.objects.filter(tray_type__icontains=tray_type_name).first()
                capacity = tray_type_obj.tray_capacity if tray_type_obj else None

                tray_obj = TrayId.objects.create(
                    tray_id=tid,
                    tray_type=tray_type_name,
                    tray_capacity=capacity,
                    date=timezone.now(),
                    user=request.user,
                    new_tray=True,
                )
                created.append({
                    'id': tray_obj.pk,
                    'tray_id': tray_obj.tray_id,
                    'tray_type': tray_obj.tray_type or '',
                    'tray_capacity': tray_obj.tray_capacity,
                })

        msg_parts = [f'{len(created)} tray(s) added successfully.']
        if errors:
            msg_parts.append(f'{len(errors)} tray(s) had issues.')

        resp_status = 'success' if not errors else 'warning'
        logger.info(f"[TrayManage] Added {len(created)} trays, {len(errors)} rejected")
        return Response({
            'status': resp_status,
            'message': ' '.join(msg_parts),
            'data': created,
            'conflicts': errors
        }, status=status.HTTP_201_CREATED if created else status.HTTP_400_BAD_REQUEST)

    def _delete_trays(self, request, tray_ids, force_delete):
        """Delete specific tray IDs with allocation check. Returns deleted tray data for undo."""
        if not tray_ids or not isinstance(tray_ids, list):
            return Response({
                'status': 'error',
                'message': 'tray_ids must be a non-empty list.',
                'data': [],
                'conflicts': []
            }, status=status.HTTP_400_BAD_REQUEST)

        not_found = []
        occupied = []
        deleted = []

        for raw_id in tray_ids:
            tid = str(raw_id).strip().upper()
            tray_obj = TrayId.objects.filter(tray_id__iexact=tid).first()

            if not tray_obj:
                not_found.append(tid)
                continue

            is_occupied = bool(tray_obj.batch_id_id) or bool(tray_obj.lot_id)

            if is_occupied and not force_delete:
                occupied.append({
                    'tray_id': tray_obj.tray_id,
                    'lot_id': tray_obj.lot_id or '',
                    'message': f'Tray "{tray_obj.tray_id}" is already occupied (Lot: {tray_obj.lot_id}). Confirm to delete.'
                })
                continue

            # Capture tray details before deletion for undo
            deleted.append({
                'tray_id': tray_obj.tray_id,
                'tray_type': tray_obj.tray_type or '',
                'tray_capacity': tray_obj.tray_capacity,
            })
            logger.info(f"[TrayManage] Deleting tray {tray_obj.tray_id} (occupied={is_occupied}, force={force_delete})")
            tray_obj.delete()

        if occupied and not deleted:
            return Response({
                'status': 'warning',
                'message': f'{len(occupied)} tray(s) are occupied. Confirm to proceed with deletion.',
                'data': [],
                'conflicts': occupied
            })

        msg_parts = []
        if deleted:
            msg_parts.append(f'{len(deleted)} tray(s) deleted.')
        if not_found:
            msg_parts.append(f'{len(not_found)} tray(s) not found: {", ".join(not_found)}.')
        if occupied:
            msg_parts.append(f'{len(occupied)} occupied tray(s) skipped (not force-deleted).')

        logger.info(f"[TrayManage] Deleted={len(deleted)}, NotFound={len(not_found)}, Occupied={len(occupied)}")
        return Response({
            'status': 'success' if deleted else 'error',
            'message': ' '.join(msg_parts) or 'No trays deleted.',
            'data': deleted,
            'conflicts': occupied + [{'tray_id': t, 'message': 'Not found'} for t in not_found]
        })

    def _delete_all_trays(self, request, force_delete):
        """Delete all tray IDs with allocation check."""
        all_trays = TrayId.objects.all()
        total = all_trays.count()

        if total == 0:
            return Response({
                'status': 'error',
                'message': 'No tray IDs exist to delete.',
                'data': [],
                'conflicts': []
            })

        occupied_trays = all_trays.filter(
            Q(batch_id__isnull=False) | ~Q(lot_id__isnull=True) & ~Q(lot_id='')
        )
        occupied_count = occupied_trays.count()

        if occupied_count > 0 and not force_delete:
            occupied_list = list(occupied_trays.values_list('tray_id', flat=True)[:20])
            return Response({
                'status': 'warning',
                'message': f'{occupied_count} of {total} tray(s) are occupied. Confirm to proceed with deletion of all trays.',
                'data': [],
                'conflicts': [{'tray_id': t, 'message': 'Occupied'} for t in occupied_list]
            })

        logger.info(f"[TrayManage] Deleting ALL {total} trays (force={force_delete})")
        deleted_data = list(all_trays.values('tray_id', 'tray_type', 'tray_capacity'))
        all_trays.delete()
        return Response({
            'status': 'success',
            'message': f'All {total} tray(s) deleted successfully.',
            'data': deleted_data,
            'conflicts': []
        })

    def _restore_trays(self, request, tray_ids):
        """Re-create previously deleted tray IDs (undo operation)."""
        if not tray_ids or not isinstance(tray_ids, list):
            return Response({
                'status': 'error',
                'message': 'tray_ids must be a non-empty list of tray objects to restore.',
                'data': [],
                'conflicts': []
            }, status=status.HTTP_400_BAD_REQUEST)

        restored = []
        skipped = []

        with transaction.atomic():
            for item in tray_ids:
                tid = str(item.get('tray_id', '')).strip().upper() if isinstance(item, dict) else str(item).strip().upper()
                tray_type = item.get('tray_type', '') if isinstance(item, dict) else ''
                tray_capacity = item.get('tray_capacity') if isinstance(item, dict) else None

                # Skip if already re-created
                if TrayId.objects.filter(tray_id__iexact=tid).exists():
                    skipped.append(tid)
                    continue

                TrayId.objects.create(
                    tray_id=tid,
                    tray_type=tray_type,
                    tray_capacity=tray_capacity,
                    date=timezone.now(),
                    user=request.user,
                    new_tray=True,
                )
                restored.append(tid)

        msg_parts = []
        if restored:
            msg_parts.append(f'{len(restored)} tray(s) restored successfully.')
        if skipped:
            msg_parts.append(f'{len(skipped)} tray(s) already exist (skipped).')

        logger.info(f"[TrayManage] Restored={len(restored)}, Skipped={len(skipped)}")
        return Response({
            'status': 'success' if restored else 'error',
            'message': ' '.join(msg_parts) or 'No trays restored.',
            'data': [{'tray_id': t} for t in restored],
            'conflicts': [{'tray_id': t, 'message': 'Already exists'} for t in skipped]
        })
# ── End Consolidated Tray Management API ─────────────────────────────────────


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class CategoryAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Categories"""
        categories = Category.objects.all()
        serializer = CategorySerializer(categories, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Category"""
        try:
            # Add current datetime to the request data
            data = request.data.copy()
            data['date_time'] = timezone.now()
            
            serializer = CategorySerializer(data=data)
            if serializer.is_valid():
                category = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Category created successfully!',
                    'data': CategorySerializer(category).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Category"""
        try:
            category = get_object_or_404(Category, pk=pk)
            serializer = CategorySerializer(category, data=request.data)
            if serializer.is_valid():
                updated_category = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Category updated successfully!',
                    'data': CategorySerializer(updated_category).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Category"""
        try:
            category = get_object_or_404(Category, pk=pk)
            category.delete()
            return Response({
                'success': True,
                'message': 'Category deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class IPRejectionAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all IP Rejection Reasons"""
        rejections = IP_Rejection_Table.objects.all()
        serializer = IPRejectionSerializer(rejections, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new IP Rejection Reason"""
        try:
            data = request.data.copy()
            data['date_time'] = timezone.now()
 
            serializer = IPRejectionSerializer(data=data)
            if serializer.is_valid():
                rejection = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Rejection reason created successfully!',
                    'data': IPRejectionSerializer(rejection).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update IP Rejection Reason"""
        try:
            rejection = get_object_or_404(IP_Rejection_Table, pk=pk)
            serializer = IPRejectionSerializer(rejection, data=request.data)
            if serializer.is_valid():
                updated_rejection = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Rejection reason updated successfully!',
                    'data': IPRejectionSerializer(updated_rejection).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete IP Rejection Reason"""
        try:
            rejection = get_object_or_404(IP_Rejection_Table, pk=pk)
            rejection.delete()
            return Response({
                'success': True,
                'message': 'Rejection reason deleted successfully!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class BrassIQFRejectionAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Brass/IQF Rejection Reasons (from Brass_QC_Rejection_Table only)"""
        rejections = Brass_QC_Rejection_Table.objects.all()
        data = [
            {
                'id': obj.id,
                'rejection_reason_id': obj.rejection_reason_id,
                'rejection_reason': obj.rejection_reason,
                
            }
            for obj in rejections
        ]
        return Response({
            'success': True,
            'data': data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Brass/IQF Rejection Reason in all three tables"""
        try:
            data = request.data.copy()
            serializer = BrassIQFRejectionSerializer(data=data)
            if serializer.is_valid():
                result = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Rejection reason created successfully in all tables!',
                    'data': BrassIQFRejectionSerializer(result).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Brass/IQF Rejection Reason in all three tables by id (Brass_QC_Rejection_Table only)"""
        try:
            qc_obj = get_object_or_404(Brass_QC_Rejection_Table, pk=pk)
            serializer = BrassIQFRejectionSerializer(qc_obj, data=request.data)
            if serializer.is_valid():
                updated_qc = serializer.save()
                # Optionally update other tables if needed
                return Response({
                    'success': True,
                    'message': 'Rejection reason updated successfully!',
                    'data': BrassIQFRejectionSerializer({'qc': updated_qc, 'audit': None, 'iqf': None}).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Brass/IQF Rejection Reason from all three tables by id (Brass_QC_Rejection_Table only)"""
        try:
            qc_obj = get_object_or_404(Brass_QC_Rejection_Table, pk=pk)
            reason_id = qc_obj.rejection_reason_id
            qc_obj.delete()
            # Also delete from other tables by rejection_reason_id
            Brass_Audit_Rejection_Table.objects.filter(rejection_reason_id=reason_id).delete()
            IQF_Rejection_Table.objects.filter(rejection_reason_id=reason_id).delete()
            return Response({
                'success': True,
                'message': 'Rejection reason deleted from all tables!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class NickelAuditQCRejectionAPIView(APIView):
    renderer_classes = [JSONRenderer]

    def get(self, request):
        """Get all Nickel QC Rejection Reasons (from Nickel_QC_Rejection_Table only)"""
        rejections = Nickel_QC_Rejection_Table.objects.all()
        data = [
            {
                'id': obj.id,
                'rejection_reason': obj.rejection_reason
            }
            for obj in rejections
        ]
        return Response({
            'success': True,
            'data': data
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Create new Nickel Audit/QC Rejection Reason in both tables"""
        try:
            data = request.data.copy()
            serializer = NickelAuditQCRejectionSerializer(data=data)
            if serializer.is_valid():
                result = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Nickel Audit/QC rejection reason created successfully in both tables!',
                    'data': NickelAuditQCRejectionSerializer(result).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk):
        """Update Nickel QC Rejection Reason by id (Nickel_QC_Rejection_Table only)"""
        try:
            qc_obj = get_object_or_404(Nickel_QC_Rejection_Table, pk=pk)
            serializer = NickelAuditQCRejectionSerializer(qc_obj, data=request.data)
            if serializer.is_valid():
                updated_qc = serializer.save()
                return Response({
                    'success': True,
                    'message': 'Nickel QC rejection reason updated successfully!',
                    'data': NickelAuditQCRejectionSerializer({'qc': updated_qc, 'audit': None}).data
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'success': False,
                    'message': 'Validation failed',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk):
        """Delete Nickel QC Rejection Reason from both tables by id (Nickel_QC_Rejection_Table only)"""
        try:
            qc_obj = get_object_or_404(Nickel_QC_Rejection_Table, pk=pk)
            reason_text = qc_obj.rejection_reason
            qc_obj.delete()
            # Also delete from audit table by rejection_reason
            Nickel_Audit_Rejection_Table.objects.filter(rejection_reason=reason_text).delete()
            return Response({
                'success': True,
                'message': 'Nickel rejection reason deleted from both tables!'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# Utility view to get dropdown data
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class ModelMasterDropdownDataAPIView(APIView):
    renderer_classes = [JSONRenderer]
    
    def get(self, request):
        """Get all dropdown data for Model Master form"""
        try:
            data = {
                'polish_finishes': list(PolishFinishType.objects.values('id', 'polish_finish')),
                'plating_colors': list(Plating_Color.objects.values('id', 'plating_color')),
                'tray_types': list(TrayType.objects.values('id', 'tray_type', 'tray_capacity')),
                'vendors': list(Vendor.objects.values('id', 'vendor_name')),
                'model_images': list(ModelImage.objects.values('id', 'master_image')),
                'versions': list(Version.objects.values('id', 'version_name')),
                'locations': list(Location.objects.values('id', 'location_name')),
                'tray_ids': list(TrayId.objects.values('id', 'tray_id', 'tray_type', 'tray_capacity')),
                'categories': list(Category.objects.values('id', 'category_name')),
            }
            return Response({
                'success': True,
                'data': data
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                'success': False,
                'message': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

""" Module - User Management """
# Class for Admin Portal HTML File Navigation (Dashboard/Settings gear - Dropdown - User Creation)
@method_decorator(login_required(login_url='login-api'), name='dispatch')
@method_decorator(require_admin, name='dispatch')
class AdminPortalView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'AdminPortal/adminPortal.html'
    
    def get(self, request, format=None):
        from .services import is_admin_user

        last_user = User.objects.order_by('-id').first()
        next_user_id = (last_user.id + 1) if last_user else 1
        allowed_modules = get_allowed_modules_for_user(request.user)
        return Response({
            'next_user_id': next_user_id,
            'allowed_modules': allowed_modules,
            'is_admin': is_admin_user(request.user),
        })

# Class for Department List APIs Masters
class DepartmentListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        departments = Department.objects.all().values('id', 'name')
        return Response(list(departments))


# Class for Role List APIs Masters
class RoleListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        roles = Role.objects.all().values('id', 'name')
        return Response(list(roles))


_HTML_CHARS_RE = re.compile(r'[<>&"\']')
_USERNAME_RE = re.compile(r'^[A-Za-z0-9@.+_-]+$')
_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
_PASSWORD_RE = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]).{8,}$')
USERNAME_VALIDATION_ERROR = 'Username may contain only letters, numbers, and the characters @ . + - _.'


def _validate_user_text_field(value, field_name):
    """Reject values containing HTML characters to prevent stored HTML injection."""
    if value and _HTML_CHARS_RE.search(value):
        return f'{field_name} must not contain HTML characters (< > & \' ").'
    return None


def validate_safe_username(value):
    username = str(value or '').strip()
    if not username:
        raise ValueError('Username is required.')
    if len(username) > 150 or not _USERNAME_RE.fullmatch(username):
        raise ValueError(USERNAME_VALIDATION_ERROR)
    return username


def _validate_email(email):
    """Validate a supplied email before any account data is saved."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    if not isinstance(email, str):
        return 'Email must be a string.'
    if not email.strip():
        return 'Email is required.'
    if len(email) > User._meta.get_field('email').max_length:
        return 'Email exceeds the maximum allowed length.'
    try:
        validate_email(email)
    except ValidationError:
        return 'Invalid email format.'
    return None


def _validate_password_complexity(password, username=''):
    if not password:
        return 'Password is required.'
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    if not _PASSWORD_RE.match(password):
        return ('Password must contain at least one uppercase letter, one lowercase letter, '
                'one digit, and one special character.')
    if username and password.lower() == username.lower():
        return 'Password cannot be the same as the username.'
    return None


def _validate_account_password(password, user):
    """Validate before hashing or saving; preserve the exact supplied password."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    if password is None or password == '':
        return 'Password is required.'
    if not isinstance(password, str):
        return 'Password must be a string.'
    error = _validate_password_complexity(password, username=user.username)
    if error:
        return error
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        return ' '.join(exc.messages)
    return None


def _normalize_group_ids_from_payload(data):
    """Accept legacy single group payloads and new multi-select group payloads."""
    if not data:
        return []

    def get_values(key):
        if hasattr(data, 'getlist'):
            values = [value for value in data.getlist(key) if value not in (None, '')]
            if values:
                return values

        value = data.get(key)
        if value in (None, ''):
            return []
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith('['):
                try:
                    parsed_value = json.loads(value)
                    if isinstance(parsed_value, list):
                        return parsed_value
                except Exception:
                    pass
            return [item.strip() for item in value.split(',') if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    raw_group_ids = []
    for key in ('group_ids', 'groups', 'group'):
        raw_group_ids = get_values(key)
        if raw_group_ids:
            break

    group_ids = []
    seen_ids = set()
    for raw_group_id in raw_group_ids:
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError):
            continue
        if group_id in seen_ids:
            continue
        seen_ids.add(group_id)
        group_ids.append(group_id)
    return group_ids


def _group_ids_field_present(data):
    """True if the request payload actually included a groups field (even if empty).

    Distinguishes "admin explicitly cleared all groups" (groups: []) from
    "this request doesn't touch groups at all" (key omitted entirely), so
    callers only reset group membership when the admin meant to.
    """
    if not data:
        return False
    for key in ('group_ids', 'groups', 'group'):
        if key in data:
            return True
    return False


def _apply_user_groups(user, group_ids, apply_admin_flags=False):
    """Replace user categories with the selected, valid groups without duplicates.

    An empty group_ids list clears the user's groups. Callers must only invoke
    this when the admin actually submitted a groups field (see
    _group_ids_field_present) - otherwise a user's existing groups would be
    wiped by unrelated partial updates (e.g. a password-only change).
    """
    if not group_ids:
        user.groups.clear()
        return []

    groups_by_id = Group.objects.filter(id__in=group_ids).in_bulk()
    selected_groups = [groups_by_id[group_id] for group_id in group_ids if group_id in groups_by_id]
    if not selected_groups:
        user.groups.clear()
        return []

    user.groups.set(selected_groups)
    if apply_admin_flags and any(group.name.lower() == 'admin' for group in selected_groups):
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
    return selected_groups


def _serialize_user_groups(user):
    prefetched_groups = getattr(user, '_prefetched_objects_cache', {}).get('groups')
    if prefetched_groups is not None:
        groups = sorted(prefetched_groups, key=lambda group: group.name.lower())
    else:
        groups = list(user.groups.all().order_by('name'))
    return {
        'group_id': groups[0].id if groups else '',
        'group_ids': [group.id for group in groups],
        'group_names': [group.name for group in groups],
        'groups': [{'id': group.id, 'name': group.name} for group in groups],
        'user_category': ', '.join(group.name for group in groups),
    }


PERSON_NAME_VALIDATION_ERROR = "Only letters, spaces, apostrophes, hyphens and dots are allowed."
PERSON_NAME_ALLOWED_PUNCTUATION = {" ", "'", "-", "."}


def validate_person_name(value):
    name = str(value or "").strip()

    if not name:
        raise ValueError(PERSON_NAME_VALIDATION_ERROR)

    if not any(ch.isalpha() for ch in name):
        raise ValueError(PERSON_NAME_VALIDATION_ERROR)

    for ch in name:
        if ch.isalpha():
            continue

        if ch in PERSON_NAME_ALLOWED_PUNCTUATION:
            continue

        raise ValueError(PERSON_NAME_VALIDATION_ERROR)

    return name


# Class for User Creation API - Fixed

class UserCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['post', 'head', 'options']

    def post(self, request, *args, **kwargs):
        from .services import invalidate_user_modules_cache, sync_user_module_provisions_from_group

        data = request.data or {}
        email = data.get('email')
        if 'email' in data:
            if isinstance(email, str):
                email = email.strip()
            email_error = _validate_email(email)
            if email_error:
                return Response({'success': False, 'error': email_error}, status=400)
        else:
            email = ''  # Existing-account updates may omit email.
        try:
            first_name = validate_person_name(data.get('first_name'))
            last_name = validate_person_name(data.get('last_name'))
        except ValueError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        password = data.get('password')
        department_id = data.get('department')
        role_id = data.get('role')
        group_ids = _normalize_group_ids_from_payload(data)
        try:
            username = validate_safe_username(data.get('username') or email or f"{first_name}.{last_name}")
        except ValueError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # If user exists, update instead of failing
                existing = User.objects.filter(username=username).first()
                if existing is None and not email:
                    return Response({'success': False, 'error': 'Email is required.'}, status=400)

                # New accounts require a password. Blank updates keep the old hash.
                if existing is None or password not in (None, ''):
                    candidate = User(
                        username=username,
                        first_name=first_name or (existing.first_name if existing else ''),
                        last_name=last_name or (existing.last_name if existing else ''),
                        email=email or (existing.email if existing else ''),
                    )
                    pwd_err = _validate_account_password(password, candidate)
                    if pwd_err:
                        return Response({'success': False, 'error': pwd_err}, status=400)

                # One email = one user: reject if the email already belongs
                # to a different account (case-insensitive).
                if email:
                    email_clash = User.objects.filter(email__iexact=email)
                    if existing:
                        email_clash = email_clash.exclude(id=existing.id)
                    if email_clash.exists():
                        return Response(
                            {'success': False, 'error': 'This email is already assigned to another user.'},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                if existing:
                    user = existing
                    if first_name:
                        user.first_name = first_name
                    if last_name:
                        user.last_name = last_name
                    if email:
                        user.email = email
                    # Update password only when provided
                    if password:
                        user.set_password(password)
                    user.save()

                    if _group_ids_field_present(data):
                        _apply_user_groups(user, group_ids)
                        if not sync_user_module_provisions_from_group(user):
                            invalidate_user_modules_cache(user.id)

                    # Ensure profile exists, then update
                    profile = getattr(user, 'userprofile', None)
                    if not profile:
                        profile = UserProfile.objects.create(user=user)

                    if department_id and Department.objects.filter(id=department_id).exists():
                        profile.department_id = department_id
                    if role_id and Role.objects.filter(id=role_id).exists():
                        profile.role_id = role_id

                    profile.manager = data.get('manager', profile.manager)
                    profile.employment_status = data.get('employment_status', profile.employment_status)
                    profile.save()

                    return Response({
                        'success': True,
                        'user_id': user.id,
                        'user': {
                            'id': user.id,
                            'username': user.username,
                            'email': user.email,
                        },
                        'message': 'Existing user updated.'
                    }, status=status.HTTP_200_OK)

                # Create new user (original behaviour)
                user = User.objects.create_user(username=username, password=password, email=email)
                user.first_name = first_name or ""
                user.last_name = last_name or ""

                # Attach groups and preserve original Admin flag behaviour on creation
                selected_groups = _apply_user_groups(user, group_ids, apply_admin_flags=True)
                if selected_groups:
                    if not sync_user_module_provisions_from_group(user):
                        invalidate_user_modules_cache(user.id)

                user.save()

                # Ensure profile exists (signal may create it) and update it
                profile = getattr(user, "userprofile", None)
                if not profile:
                    profile = UserProfile.objects.create(user=user)

                # Department and role are optional fields
                if department_id and Department.objects.filter(id=department_id).exists():
                    profile.department_id = department_id
                if role_id and Role.objects.filter(id=role_id).exists():
                    profile.role_id = role_id
                profile.manager = data.get('manager')
                profile.employment_status = data.get('employment_status')
                profile.save()

            return Response({
                'success': True,
                'user_id': user.id,
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                },
                'message': 'User created successfully.'
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception('User creation failed')
            return Response({'success': False, 'error': 'An internal error occurred. Please contact the administrator.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def create_user(request):
    with transaction.atomic():
        # create or get user
        user, created = User.objects.get_or_create(
            username=request.data['username'],
            defaults={
                'first_name': request.data['first_name'],
                'last_name': request.data['last_name'],
                'email': request.data['email']
            }
        )
        
        # create profile only if it doesn't exist
        if not hasattr(user, 'userprofile'):
            UserProfile.objects.create(
                user=user,
                department_id=request.data['department'],
                role_id=request.data['role']
            )
        
        return Response({'success': True, 'user_id': user.id})
    
    
    
def _mask_email(value):
    """
    Partially redact an email address for list/table display (VAPT / Burp
    finding "Email addresses disclosed" on /adminportal/api/users/list/).

    'test@gmail.com' -> 't•••@gmail.com'

    The bullet (U+2022) is not a valid email atom character, so the result
    no longer matches an email pattern, while an admin can still recognise
    which account a row belongs to. The single-user detail endpoint keeps the
    real value because the edit form pre-fills from it.
    """
    value = (value or '').strip()
    if '@' not in value:
        return value
    local, _, domain = value.partition('@')
    masked_local = (local[0] if local else '') + '•••'
    return masked_local + '@' + domain


class UserListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        users = User.objects.select_related('account_lockout').prefetch_related('groups', 'module_provisions').all().order_by('id')
        paginator = Paginator(users, 8)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)
        user_list = []
        for user in page_obj.object_list:
            try:
                profile = user.userprofile
                department = profile.department.name if profile.department else ""
                role = profile.role.name if profile.role else ""
                manager = profile.manager
                employment_status = profile.employment_status
            except Exception:
                department = role = manager = employment_status = ""
            module_access = user.module_provisions.all()
            modules = [
                {
                    "name": access.module_name,
                    "headings": access.headings
                }
                for access in module_access
            ]
            group_data = _serialize_user_groups(user)
            created = user.date_joined.strftime("%Y-%m-%d %H:%M")
            lockout = getattr(user, 'account_lockout', None)
            is_locked = bool(lockout and lockout.is_locked)
            locked_at = (
                timezone.localtime(lockout.locked_at).strftime("%Y-%m-%d %H:%M")
                if is_locked and lockout.locked_at else ""
            )
            user_list.append({
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": _mask_email(user.email),
                "department": department,
                "user_category": group_data['user_category'],
                "user_categories": group_data['groups'],
                "group_ids": group_data['group_ids'],
                "role": role,
                "manager": manager,
                "employment_status": employment_status,
                "modules": modules,
                "created": created,
                "is_superuser": user.is_superuser,
                "is_locked": is_locked,
                "failed_login_attempts": lockout.failed_attempts if lockout else 0,
                "locked_at": locked_at
            })
        return Response({
            "results": user_list,
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "current_page": page_obj.number
        })



class UserGroupListAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        from .services import ensure_module_registry_seeded

        ensure_module_registry_seeded()
        groups = Group.objects.all().order_by('name').values('id', 'name')
        return Response(list(groups))



class GroupModulesAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['get', 'head', 'options']

    def get(self, request, group_id):
        from .services import ensure_module_registry_seeded

        ensure_module_registry_seeded()
        try:
            group = Group.objects.get(id=group_id)
            modules = Module.objects.filter(groups=group).order_by('id')
            data = [
                {
                    "id": m.id,
                    "name": m.name,
                    "menu_title": m.menu_title,
                    "headings": m.headings or [],
                    "all_headings": m.headings or [],
                    "file_name": m.html_file or "",
                }
                for m in modules
            ]
            return Response({"success": True, "modules": data})
        except Group.DoesNotExist:
            return Response({"success": False, "error": "Group not found"}, status=404)

# Function for User Visibile Modules API (checkbox/unchecked logic)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def user_allowed_modules(request):
    from .services import (
        get_user_allowed_module_payload,
        invalidate_user_modules_cache,
        is_admin_user,
        sync_user_module_provisions_from_group,
    )

    user = request.user
    if not user.is_authenticated:
        return {'allowed_modules': []}

    if not is_admin_user(user):
        logger.warning(
            'UNAUTHORIZED_ADMIN_ACCESS: path=%s method=%s ip=%s',
            request.path, request.method,
            request.META.get('REMOTE_ADDR', 'unknown'),
        )
        return Response({'error': 'Access denied. Admin privileges required.', 'code': 'ADMIN_REQUIRED'}, status=403)

    # ----- POST logic -----
    if request.method == 'POST':
        user_id = request.data.get('user_id')
        if user_id:
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                return Response({'success': False, 'error': 'User not found.'}, status=404)

        try:
            modules = request.data.get('modules', [])
            if not isinstance(modules, list):
                return Response({'success': False, 'error': 'Modules should be a list.'}, status=400)

            if sync_user_module_provisions_from_group(user):
                return Response({'success': True, 'message': 'Modules auto-assigned from user category.'})

            UserModuleProvision.objects.filter(user=user).delete()
            seen_module_names = set()
            for mod in modules:
                if not isinstance(mod, dict):
                    continue
                module_name = (mod.get('name') or '').strip()
                if not module_name or module_name in seen_module_names:
                    continue
                seen_module_names.add(module_name)
                headings = mod.get('headings', [])
                if not isinstance(headings, list):
                    headings = []
                UserModuleProvision.objects.update_or_create(
                    user=user,
                    module_name=module_name,
                    defaults={
                        'headings': headings,
                        'file_name': mod.get('file_name', '')
                    }
                )
            invalidate_user_modules_cache(user.id)
            return Response({'success': True, 'message': 'Modules saved successfully.'})
        except Exception as e:
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)

    # ----- GET logic -----
    # ----- GET logic -----
    # Check if a specific user_id is requested (for Admin editing)
    target_user = user
    requested_user_id = request.GET.get('user_id')
    
    # Permission check for fetching other users
    is_admin = is_admin_user(user)

    if requested_user_id and is_admin:
        try:
            target_user = User.objects.get(id=requested_user_id)
        except User.DoesNotExist:
            return Response({'allowed_modules': []}, status=404)

    return Response({"modules": get_user_allowed_module_payload(target_user)})


# Lightweight self-check used by the post-SSO "no modules assigned" alert to
# detect that an admin has since granted access, without a full page reload.
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_allowed_modules_status(request):
    from .services import get_user_allowed_module_names

    modules = get_user_allowed_module_names(request.user)
    return Response({'has_modules': bool(modules)})


#Class for User Deletion API (inactive — route is commented out in urls.py)
class UserDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['delete', 'head', 'options']

    def delete(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
            user.delete()
            return Response({'success': True, 'message': 'User deleted.'}, status=200)
        except User.DoesNotExist:
            return Response({'success': False, 'error': 'User not found.'}, status=404)
        except Exception as e:
            logger.exception('UserDeleteAPIView.delete error: user_id=%s', user_id)
            return Response({'success': False, 'error': 'An internal error occurred. Please contact the administrator.'}, status=500)
        

  
    
@require_admin
@csrf_exempt
def extract_headings_api(request):
    html_file = request.GET.get('html_file')
    if not html_file:
        return JsonResponse({'success': False, 'error': 'No file specified'})
    TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'templates')
    abs_path = os.path.join(TEMPLATES_DIR, html_file)
    if not os.path.exists(abs_path):
        return JsonResponse({'success': False, 'error': 'File not found'})
    headings = extract_table_headings_from_html(abs_path)
    return JsonResponse({'success': True, 'headings': headings})

@login_required(login_url='login-api')
@require_admin
@csrf_exempt
def swap_login(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            username = data.get("username")
            password = data.get("password")
            user = authenticate(request, username=username, password=password)
            if user is not None:
                # Optionally, you can log in the user or just return success
                return JsonResponse({"success": True})
            else:
                return JsonResponse({"success": False, "error": "Invalid credentials"})
        except Exception as e:
            return JsonResponse({"success": False, "error": 'Unable to process the request. Please verify the submitted data and try again.'})
    return JsonResponse({"success": False, "error": "Invalid request"}, status=400)

class UserDetailAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['get', 'head', 'options']

    def dispatch(self, request, *args, **kwargs):
        method = request.method.lower()

        # Reject disallowed verbs (DELETE, PUT, POST, OPTIONS, …) with 405 BEFORE
        # authentication so ForbiddenToLoginMiddleware cannot convert the
        # response to a browser redirect.
        if method not in self.http_method_names:
            from django.http import HttpResponseNotAllowed
            response = HttpResponseNotAllowed([m.upper() for m in self.http_method_names])
            response['Allow'] = ', '.join(m.upper() for m in self.http_method_names)
            return response

        return super().dispatch(request, *args, **kwargs)

    def get(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
            profile = getattr(user, 'userprofile', None)
            group_data = _serialize_user_groups(user)
            return Response({
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "department_id": profile.department.id if profile and profile.department else "",
                "role_id": profile.role.id if profile and profile.role else "",
                "manager": profile.manager if profile else "",
                "employment_status": profile.employment_status if profile else "",
                "group_id": group_data['group_id'],
                "group_ids": group_data['group_ids'],
                "group_names": group_data['group_names'],
                "groups": group_data['groups'],
            })
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)


class UserUpdateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['post', 'head', 'options']

    def post(self, request, user_id):
        from .services import invalidate_user_modules_cache, sync_user_module_provisions_from_group

        data = request.data or {}
        new_username = data.get('username')
        new_first_name = data.get('first_name')
        new_last_name = data.get('last_name')
        new_email = data.get('email')
        if 'email' in data:
            if isinstance(new_email, str):
                new_email = new_email.strip()
            email_error = _validate_email(new_email)
            if email_error:
                return Response({'success': False, 'error': email_error}, status=400)
        password = data.get('password')

        try:
            if new_username is not None:
                new_username = validate_safe_username(new_username)
            if new_first_name is not None:
                new_first_name = validate_person_name(new_first_name)
            if new_last_name is not None:
                new_last_name = validate_person_name(new_last_name)
        except ValueError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(id=user_id)
            if new_username is not None:
                user.username = new_username
            if new_first_name is not None:
                user.first_name = new_first_name
            if new_last_name is not None:
                user.last_name = new_last_name
            if new_email is not None:
                new_email = str(new_email).strip()
                if new_email and User.objects.filter(email__iexact=new_email).exclude(id=user.id).exists():
                    return Response(
                        {'success': False, 'error': 'This email is already assigned to another user.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                user.email = new_email

            if password not in (None, ''):
                pwd_err = _validate_account_password(password, user)
                if pwd_err:
                    return Response({'success': False, 'error': pwd_err}, status=400)
                user.set_password(password)

            user.save()

            profile = getattr(user, 'userprofile', None)
            if profile:
                department_id = data.get('department')
                role_id = data.get('role')
                if department_id:
                    profile.department_id = department_id
                if role_id:
                    profile.role_id = role_id
                profile.manager = data.get('manager', profile.manager)
                profile.employment_status = data.get('employment_status', profile.employment_status)
                profile.save()

            if _group_ids_field_present(data):
                _apply_user_groups(user, _normalize_group_ids_from_payload(data))
                if not sync_user_module_provisions_from_group(user):
                    invalidate_user_modules_cache(user.id)

            return Response({'success': True, 'message': 'User updated successfully.'})
        except User.DoesNotExist:
            return Response({'success': False, 'error': 'User not found.'}, status=404)
        except Exception as e:
            logger.exception('UserUpdateAPIView.post error: user_id=%s', user_id)
            return Response({'success': False, 'error': 'An internal error occurred. Please contact the administrator.'}, status=500)


class UserUnlockAPIView(APIView):
    """Administrator-controlled unlock for accounts locked by the lockout policy."""
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['post', 'head', 'options']

    def post(self, request, user_id):
        from .services import unlock_user_account

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'error': 'User not found.'}, status=404)

        try:
            if not unlock_user_account(user, unlocked_by=request.user):
                return Response({'success': False, 'error': 'Account is not locked.'}, status=400)
            return Response({
                'success': True,
                'message': f'Account "{user.username}" has been unlocked successfully.'
            })
        except Exception:
            logger.exception('UserUnlockAPIView.post error: user_id=%s', user_id)
            return Response({'success': False, 'error': 'An internal error occurred. Please contact the administrator.'}, status=500)


class UserDeletePostAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminPermission]
    http_method_names = ['post', 'head', 'options']

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
            user.delete()
            return Response({'success': True, 'message': 'User deleted.'}, status=200)
        except User.DoesNotExist:
            return Response({'success': False, 'error': 'User not found.'}, status=404)
        except Exception as e:
            logger.exception('UserDeletePostAPIView.post error: user_id=%s', user_id)
            return Response({'success': False, 'error': 'An internal error occurred. Please contact the administrator.'}, status=500)


# Safe class - static handling
""" @method_decorator(login_required(login_url='login-api'), name='dispatch')
class DP_PickTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'Day_Planning/DP_PickTable.html'

    def get(self, request, format=None):
        # ...your existing logic to get master_data, etc...
        user = request.user
        # Example: Assume module_name is "DayPlanningPickTable"
        module_name = "DP Pick Table"
        # Get allowed headings for this user/module
        allowed_headings = []
        provision = UserModuleProvision.objects.filter(user=user, module_name=module_name).first()
        print('Provision:', provision)
        if provision and provision.headings:
            allowed_headings = provision.headings
            print(f"User {user.username} has specific module provisions: {allowed_headings}")
        else:
            # fallback: show all headings if not restricted
            allowed_headings = [
                "S.No", "Last Updated", "Plating Stk No", "Polishing Stk No", "Plating Color",
                "Category", "Polish Finish", "Version", "Tray Cate-Capacity", "Source",
                "No of Trays", "Input Qty", "Process Status", "Action", "Lot Status",
                "Current Stage", "Remarks"
            ]
            print(f"User {user.username} has no specific module provisions, using default headings: {allowed_headings}" )
        context = {
            # ...existing context...
            'allowed_headings': allowed_headings,
            # ...other context...
        }
        return Response(context)
 """



def get_visible_headings_for_user(user, module_name):
    """
    Returns a dict: {heading: True/False} for all headings of the module.
    True = editable, False = non-editable (blurred).
    """
    module = Module.objects.filter(name=module_name).first()
    all_headings = module.headings if module else []
    provision = UserModuleProvision.objects.filter(user=user, module_name=module_name).first()
    allowed_headings = provision.headings if (provision and provision.headings) else all_headings
    return {h: h in allowed_headings for h in all_headings}


# Class for Generic Module Table View
@method_decorator(login_required(login_url='login-api'), name='dispatch')
class ModuleTableView(APIView):
    """
    Generic view for any module table.
    Usage: pass module_name as a URL kwarg or query param.
    Example URL: /adminportal/module-table/?module_name=DP Pick Table
    """
    renderer_classes = [TemplateHTMLRenderer]

    def get(self, request, *args, **kwargs):
        # 1. Get module_name from URL (query param or kwarg)
        module_name = kwargs.get('module_name') or request.GET.get('module_name')
        if not module_name:
            return Response({'error': 'Module name not specified.'}, status=400)

        # 2. Fetch the Module object
        module = Module.objects.filter(name=module_name).first()
        if not module:
            return Response({'error': f'Module "{module_name}" not found.'}, status=404)

        # 3. Get the template file name from the module
        template_name = module.html_file or 'Day_Planning/DP_PickTable.html'
        self.template_name = template_name

        # 4. Get allowed headings for this user/module from UserModuleProvision
        provision = UserModuleProvision.objects.filter(user=request.user, module_name=module_name).first()
        if provision and provision.headings:
            allowed_headings = provision.headings
        else:
            # fallback: use all headings from the Module master
            allowed_headings = module.headings or []

        visible_headings = get_visible_headings_for_user(request.user, module_name)
        context = {
            'allowed_headings': allowed_headings,
            'module_name': module_name,
            'visible_headings': visible_headings,
        }
        return Response(context)


# Function for checking if a user is an admin for heading blurred logic 
# Function for checking if a user is an admin for heading blurred logic 
def is_admin_user(user):
    """
    Returns True if the user is superuser, in Admin group, or department is Admin.
    """
    if not user.is_authenticated:
        return False
    return (
        user.is_superuser
        or user.groups.filter(name__iexact="Admin").exists()
        or (
            hasattr(user, 'userprofile')
            and user.userprofile.department
            and user.userprofile.department.name.lower() == "admin"
        )
    )

class UserPageAPIView(APIView):
    def get(self, request):
        user_id = request.GET.get('user_id')
        if not user_id:
            return Response({'error': 'User ID required'}, status=400)
        
        try:
            target_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=404)
            
        # Must match UserListAPIView ordering (id ascending)
        position = User.objects.filter(id__lt=user_id).count() 
        
        page_size = 6 # Must match UserListAPIView
        page_number = (position // page_size) + 1
        
        return Response({'page': page_number})


@method_decorator(login_required(login_url='login-api'), name='dispatch')
class LotRemarkHistoryAPIView(APIView):
    """
    GET /adminportal/api/lot_remark_history/?lot_id=<lot_id>
    Returns all pick-stage remarks for a lot in workflow order.
    Queries TotalStockModel (early stages) and JigUnloadAfterTable (post-unloading stages).
    """
    renderer_classes = [JSONRenderer]

    def get(self, request):
        lot_id = (request.GET.get('lot_id') or '').strip()
        if not lot_id:
            return Response({'success': False, 'error': 'lot_id is required'}, status=400)

        remarks = []

        # ── Early stages: TotalStockModel ──────────────────────────────────
        try:
            from modelmasterapp.models import TotalStockModel
            stock = TotalStockModel.objects.select_related('batch_id').filter(lot_id=lot_id).first()
            if stock:
                # Day Planning remark lives on ModelMasterCreation (batch)
                if stock.batch_id and stock.batch_id.dp_pick_remarks:
                    remarks.append({
                        'stage': 'Day Planning',
                        'remark': stock.batch_id.dp_pick_remarks,
                    })
                if stock.IP_pick_remarks:
                    remarks.append({'stage': 'Input Screening', 'remark': stock.IP_pick_remarks})
                if stock.IQF_pick_remarks:
                    remarks.append({'stage': 'IQF', 'remark': stock.IQF_pick_remarks})
                if stock.Bq_pick_remarks:
                    remarks.append({'stage': 'Brass QC', 'remark': stock.Bq_pick_remarks})
                if stock.BA_pick_remarks:
                    remarks.append({'stage': 'Brass Audit', 'remark': stock.BA_pick_remarks})
                if stock.jig_pick_remarks:
                    remarks.append({'stage': 'Jig Loading', 'remark': stock.jig_pick_remarks})
        except Exception as e:
            logger.warning("[LotRemarkHistory] TotalStockModel lookup failed for lot_id=%s: %s", lot_id, e)

        # ── Post-unloading stages: JigUnloadAfterTable ────────────────────
        try:
            from Jig_Unloading.models import JigUnloadAfterTable
            juat = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
            if juat:
                if juat.nq_pick_remarks:
                    remarks.append({'stage': 'Nickel Inspection', 'remark': juat.nq_pick_remarks})
                if juat.na_pick_remarks:
                    remarks.append({'stage': 'Nickel Audit', 'remark': juat.na_pick_remarks})
                if juat.spider_pick_remarks:
                    remarks.append({'stage': 'Spider Spindle', 'remark': juat.spider_pick_remarks})
        except Exception as e:
            logger.warning("[LotRemarkHistory] JigUnloadAfterTable lookup failed for lot_id=%s: %s", lot_id, e)

        return Response({
            'success': True,
            'lot_id': lot_id,
            'remarks': remarks,
        })
