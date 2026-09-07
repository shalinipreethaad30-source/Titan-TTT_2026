/**
 * session_guard.js — Global idle-session expiry handler.
 *
 * Backend counterpart: adminportal.middleware.SessionExpiredAjaxMiddleware
 * returns HTTP 401 with { code: 'SESSION_EXPIRED' } when an expired session
 * makes a background fetch/AJAX call (e.g. tray-ID scan validation).
 *
 * Two detection layers, no business logic (frontend displays, backend decides):
 *  1. Reactive  — every fetch()/jQuery AJAX response is inspected; a
 *     session-expired 401 shows one professional alert and returns the user
 *     to the login page, instead of a misleading "Validation Error".
 *  2. Proactive — an idle timer (driven by the server-provided
 *     <meta name="session-expiry-seconds">) pings a lightweight authenticated
 *     endpoint shortly after the inactivity window elapses. If the server says
 *     the session is gone, the same alert appears without the user having to
 *     scan or click anything.
 */
(function () {
  'use strict';

  var LOGIN_URL = '/accounts/login/';
  var PING_URL = '/adminportal/api/shortcuts/'; // cheap, login_required, cached
  var HEARTBEAT_URL = '/adminportal/api/session-heartbeat/';
  var HEARTBEAT_INTERVAL_MS = 5000;             // fast enough that a takeover on another device shows up almost immediately
  var IDLE_BUFFER_MS = 1000;                    // grace period past cookie age
  var RECHECK_INTERVAL_MS = 30000;              // fallback re-check cadence
  var alertShown = false;
  var idleTimerId = null;
  var lastActivityAt = Date.now();              // last genuine user activity
  var checkInFlight = false;

  function redirectToLogin() {
    var next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = LOGIN_URL + '?next=' + next;
  }

  function showSessionExpiredAlert(reason) {
    if (alertShown) { return; }
    alertShown = true;
    if (idleTimerId) { clearTimeout(idleTimerId); }

    var title = 'Session Expired';
    var message = 'Your session has expired due to inactivity. Please log in again to continue.';
    if (reason === 'takeover') {
      title = 'Logged Out';
      message = 'This account was just signed in from another device, so you have been logged out here. Please log in again if this was not you.';
    }

    if (typeof window.Swal !== 'undefined' && window.Swal && window.Swal.fire) {
      window.Swal.fire({
        icon: 'warning',
        title: title,
        text: message,
        confirmButtonText: 'Re-login',
        allowOutsideClick: false,
        allowEscapeKey: false
      }).then(redirectToLogin);
    } else {
      window.alert(title + '\n\n' + message);
      redirectToLogin();
    }
  }

  function isSessionExpiredResponse(response) {
    if (!response) { return false; }
    if (response.status === 401) { return true; }
    // Fallback: a fetch that was silently redirected to the login page.
    return !!(response.redirected && response.url && response.url.indexOf(LOGIN_URL) !== -1);
  }

  function handlePossiblyExpired(response) {
    if (response.status === 401) {
      // Only treat as expired when the backend says so.
      response.clone().json().then(function (data) {
        if (data && data.code === 'SESSION_TAKEOVER') {
          showSessionExpiredAlert('takeover');
        } else if (data && (data.code === 'SESSION_EXPIRED' || data.code === 'NOT_AUTHENTICATED')) {
          showSessionExpiredAlert();
        } else if (data && typeof data.detail === 'string' &&
                   data.detail.toLowerCase().indexOf('logged in elsewhere') !== -1) {
          showSessionExpiredAlert();
        }
      }).catch(function () { /* non-JSON 401: leave to the page's own handler */ });
    } else {
      showSessionExpiredAlert();
    }
  }

  // ── Reactive layer: patch window.fetch ────────────────────────────────────
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  if (nativeFetch) {
    window.fetch = function () {
      return nativeFetch.apply(null, arguments).then(function (response) {
        if (isSessionExpiredResponse(response)) {
          handlePossiblyExpired(response);
        }
        // Do NOT treat background/server traffic as user activity.
        // Automatic polling/fetch calls must not postpone the 15-minute
        // inactivity alert. Only genuine user interaction resets the timer.
        return response;
      });
    };
  }

  // ── Reactive layer: jQuery-based AJAX calls ───────────────────────────────
  if (window.jQuery) {
    window.jQuery(document).ajaxError(function (event, jqXHR) {
      if (jqXHR && jqXHR.status === 401) {
        var data = null;
        try { data = JSON.parse(jqXHR.responseText); } catch (e) { /* ignore */ }
        if (data && (data.code === 'SESSION_EXPIRED' || data.code === 'NOT_AUTHENTICATED')) {
          showSessionExpiredAlert();
        }
      }
    });
  }

  // ── Presence layer: heartbeat so a closed tab is detected within seconds,
  // not the full idle-session timeout (backend: services.get_active_session_
  // conflict_message / ACTIVE_SESSION_STALE_SECONDS) ────────────────────────
  function getCsrfToken() {
    var tokenInput = document.querySelector('[name="csrfmiddlewaretoken"]');
    return tokenInput ? tokenInput.value : '';
}

  function sendHeartbeat() {
    if (alertShown || !nativeFetch) { return; }
    nativeFetch(HEARTBEAT_URL, {
      method: 'POST',
      headers: { 'X-CSRFToken': getCsrfToken() },
      credentials: 'same-origin'
    }).then(function (response) {
      // Uses nativeFetch (not the patched fetch) so a healthy heartbeat
      // doesn't reset the idle-expiry countdown; a 401 here still means
      // this device's session was taken over or expired, so it must be
      // reported the same way a real page request's 401 would be.
      if (isSessionExpiredResponse(response)) {
        handlePossiblyExpired(response);
      }
    }).catch(function () { /* best-effort; next tick retries */ });
  }

  function startHeartbeat() {
    sendHeartbeat();
    setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS);
  }

  // ── Proactive layer: idle-expiry detection ────────────────────────────────
  function getExpirySeconds() {
    var meta = document.querySelector('meta[name="session-expiry-seconds"]');
    var value = meta ? parseInt(meta.getAttribute('content'), 10) : NaN;
    return (isNaN(value) || value <= 0) ? 900 : value;
  }

  function scheduleIdleCheck(delayMs) {
    if (alertShown) { return; }
    if (idleTimerId) { clearTimeout(idleTimerId); }
    idleTimerId = setTimeout(checkIdleTimeout, delayMs);
  }

  function checkIdleTimeout() {
    if (alertShown) { return; }

    var expiryMs = getExpirySeconds() * 1000;
    var idleMs = Date.now() - lastActivityAt;

    if (idleMs >= expiryMs) {
      // The inactivity policy is based on genuine user interaction, not on
      // background polling. Show the re-login alert as soon as the configured
      // inactivity window has elapsed.
      showSessionExpiredAlert();
      return;
    }

    // Timer throttling/background-tab delays can make this callback run early
    // or after user activity. Schedule only the remaining idle duration.
    scheduleIdleCheck((expiryMs - idleMs) + IDLE_BUFFER_MS);
  }

  function recordUserActivity() {
    if (alertShown) { return; }
    lastActivityAt = Date.now();
    scheduleIdleCheck(getExpirySeconds() * 1000 + IDLE_BUFFER_MS);
  }

  // Browsers heavily throttle setTimeout in background tabs, so the timer
  // alone can fire minutes late. The moment the user comes back to the tab,
  // verify immediately if the idle window has already elapsed.
  function checkNowIfOverdue() {
    if (alertShown) { return; }
    var idleMs = Date.now() - lastActivityAt;
    if (idleMs >= getExpirySeconds() * 1000) {
      showSessionExpiredAlert();
    }
  }

  function startIdleWatch() {
    // Only watch on pages rendered for an authenticated user (base.html);
    // the login page does not include this script.
    lastActivityAt = Date.now();
    scheduleIdleCheck(getExpirySeconds() * 1000 + IDLE_BUFFER_MS);
    startHeartbeat();

    // Reset the inactivity window only for genuine user interaction.
    // Passive/background fetches, polling and heartbeat traffic are excluded.
    ['mousedown', 'keydown', 'touchstart', 'scroll'].forEach(function (eventName) {
      document.addEventListener(eventName, recordUserActivity, { passive: true });
    });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { checkNowIfOverdue(); }
    });
    window.addEventListener('focus', checkNowIfOverdue);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startIdleWatch);
  } else {
    startIdleWatch();
  }
})();
