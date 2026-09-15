"""
URL configuration for watchcase_tracker project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import sys
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from django.contrib.auth import views as auth_views
from adminportal.views import IndexView, TimedLoginView
from django.conf.urls import handler404, handler500, handler403, handler400
from importlib.util import find_spec
import logging

_urls_logger = logging.getLogger(__name__)
try:
    from watchcase_tracker import sso as _sso_module
except ImportError:
    # Never fail silently: an import problem (e.g. missing "msal" package in
    # the active environment) previously made the Microsoft button bounce
    # straight back to the login page with no explanation.
    _sso_module = None
    _urls_logger.exception(
        'watchcase_tracker.sso failed to import - Microsoft SSO routes are '
        'disabled. Fix the import error below (usually a missing dependency).'
    )
_social_django_available = find_spec('social_django') is not None
from django.shortcuts import render
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.generic.base import RedirectView


def root_redirect(request):
    return redirect('login')


def sso_unavailable(request):
    _urls_logger.error('SSO route hit but SSO module is unavailable: %s', request.path)
    return redirect(settings.LOGIN_URL + '?sso_error=unavailable')


urlpatterns = [
    
    #path('admin/', admin.site.urls),
    path('', root_redirect, name='root'),

    # Browsers and scanners auto-request /favicon.ico. Without a route it fell
    # through to FastCGI and returned an IIS 500 page with no HSTS / charset
    # (VAPT / Burp). Redirect it to the real icon so it is a clean 301.
    path(
        'favicon.ico',
        RedirectView.as_view(url=settings.STATIC_URL + 'assets/images/favicon.png', permanent=True),
    ),
    
    path('accounts/profile/', lambda request: redirect('home')),
    
    # Use your custom login template here:    
    path('accounts/login/', TimedLoginView.as_view(template_name='login.html'), name='login'),
    # Alias used by adminportal API views that require authentication.
    path('accounts/login/', TimedLoginView.as_view(template_name='login.html'), name='login-api'),
     
    path('home/', IndexView.as_view(), name="home"),  # Dashboard with permission-filtered stats
    path('admin/', admin.site.urls),
    path('',include('modelmasterapp.urls')),
    path('adminportal/',include('adminportal.urls')),
    path('dayplanning/',include('DayPlanning.urls')),
    path('inputscreening/',include('InputScreening.urls')),
    path('recovery_dp/',include('Recovery_DP.urls')),
    path('recovery_is/',include('Recovery_IS.urls')),
    path('recovery_brassqc/',include('Recovery_Brass_QC.urls')),
    path('recovery_brass_audit/',include('Recovery_BrassAudit.urls')),
    path('recovery_iqf/',include('Recovery_IQF.urls')),
    path('brass_qc/',include('Brass_QC.urls')),
    path('brass_audit/',include('BrassAudit.urls')),
    path('iqf/',include('IQF.urls')),
    path('jig_loading/',include('Jig_Loading.urls')),
    path('jig_unloading/',include('Jig_Unloading.urls')),
    path('JigUnloading_Zone2/',include('JigUnloading_Zone2.urls')),
    path('inprocess_inspection/',include('Inprocess_Inspection.urls')),
    path('nickle_inspection/',include('Nickel_Inspection.urls')),
    path('nickle_inspection_zone_two/',include('nickel_inspection_zone_two.urls')),

    path('nickel_audit/',include('Nickel_Audit.urls')),
    path('nickel_audit_zone_two/',include('nickel_audit_zone_two.urls')),

    path('reports_module/', include('ReportsModule.urls', namespace='reports_module')),
    path('spider_spindle/', include('SpiderSpindle_Z1.urls')),
    path('spider_spindle_zone_two/', include('SpiderSpindle_Z2.urls')),
    path('sop_management/', include('SOPManagement.urls', namespace='sop_management')),
    
    
    
    
]

# Optional social-auth route package. Username/password login must not depend on it.
if _social_django_available:
    urlpatterns += [
        path('auth/', include('social_django.urls', namespace='social')),
    ]

# Keep these URL names registered because login.html reverses microsoft_login.
urlpatterns += [
    path(
        'auth/microsoft/login/',
        _sso_module.microsoft_login if _sso_module else sso_unavailable,
        name='microsoft_login',
    ),
    path(
        'auth/microsoft/callback',
        _sso_module.microsoft_callback if _sso_module else sso_unavailable,
        name='microsoft_callback',
    ),
    path(
        'auth/microsoft/callback/',
        _sso_module.microsoft_callback if _sso_module else sso_unavailable,
    ),
]

serve_local_media = settings.DEBUG or 'runserver' in sys.argv

if serve_local_media:
    urlpatterns += [
        re_path(
            r'^media/(?P<path>.*)$',
            serve,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]

if settings.DEBUG:
    urlpatterns += static(
        settings.STATIC_URL,
        document_root=settings.STATICFILES_DIRS[0],
    )
def custom_404(request, exception):
    return render(request, "pages/samples/error-404.html", status=404)

def custom_500(request):
    return render(request, "pages/samples/error-500.html", status=500)

def custom_403(request, exception):
    return render(request, "pages/samples/error-404.html", status=403)  # You can use a separate 403 template

def custom_400(request, exception):
    return render(request, "pages/samples/error-404.html", status=400)  # You can use a separate 400 template

handler404 = 'watchcase_tracker.urls.custom_404'
handler500 = 'watchcase_tracker.urls.custom_500'
handler403 = 'watchcase_tracker.urls.custom_403'
handler400 = 'watchcase_tracker.urls.custom_400'



from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import HttpResponseServerError
def test_500(request):
    raise Exception("Test 500 error")

def test_403(request):
    raise PermissionDenied("Test 403 error")

def test_400(request):
    raise SuspiciousOperation("Test 400 error")

urlpatterns += [
    path('test500/', test_500),
    path('test403/', test_403),
    path('test400/', test_400),
]