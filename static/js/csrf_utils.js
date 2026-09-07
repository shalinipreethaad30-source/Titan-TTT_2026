/*
 * csrf_utils.js — CSRF token delivery for a HttpOnly csrftoken cookie.
 *
 * Background (VAPT / Burp finding: "Cookie without HttpOnly flag set" — the
 * csrftoken cookie). settings.py now sets CSRF_COOKIE_HTTPONLY = True, so
 * JavaScript can no longer read the token with document.cookie. Django still
 * validates every state-changing request by comparing the X-CSRFToken header
 * (or the csrfmiddlewaretoken form field) against the cookie the browser sends
 * automatically, so nothing on the server changes.
 *
 * This file is deliberately self-contained so that NO existing page code has
 * to be touched. Dozens of templates/scripts read the token with a variety of
 * broken-after-HttpOnly helpers (getCookie('csrftoken'), getCsrf(),
 * window.getCsrfToken(), inline document.cookie parsing, …). Instead of
 * editing all of them, this shim:
 *
 *   1. Exposes window.getCSRFToken() — reads the token from the DOM
 *      (<meta name="csrf-token"> in base.html, or the {% csrf_token %} hidden
 *      <input name="csrfmiddlewaretoken"> that every page already renders).
 *
 *   2. Transparently sets a correct X-CSRFToken header on every same-origin
 *      POST / PUT / PATCH / DELETE made with fetch() or XMLHttpRequest,
 *      overwriting the now-empty value the legacy helpers produce.
 *
 * Cross-origin requests (reCAPTCHA, Microsoft SSO, CDNs) are never touched, so
 * the token is not leaked off-site. GET/HEAD/OPTIONS are left alone.
 *
 * MUST be loaded early in base.html — before session_guard.js and before any
 * page script — so it wraps the genuine native fetch / XMLHttpRequest first.
 */
(function () {
  'use strict';

  function readToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.getAttribute('content')) {
      return meta.getAttribute('content');
    }
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : '';
  }

  // Public helper for any future / refactored code.
  window.getCSRFToken = readToken;

  var UNSAFE_METHOD = /^(POST|PUT|PATCH|DELETE)$/i;

  function isSameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) {
      // Relative path with no scheme/host — same origin by definition.
      return true;
    }
  }

  // ---- fetch() ------------------------------------------------------------
  if (typeof window.fetch === 'function') {
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        init = init || {};
        var method = init.method ||
          (input && typeof input === 'object' && input.method) || 'GET';
        var url = (typeof input === 'string')
          ? input
          : (input && input.url) || '';

        if (UNSAFE_METHOD.test(method) && isSameOrigin(url)) {
          var token = readToken();
          if (token) {
            var headers = new Headers(
              init.headers ||
              (input && typeof input === 'object' && input.headers) ||
              {}
            );
            // .set() REPLACES — clears any stale/empty value from a
            // legacy getCookie('csrftoken') call. Content-Type is left
            // untouched, so FormData auto-detection still works.
            headers.set('X-CSRFToken', token);
            init.headers = headers;
          }
        }
      } catch (e) {
        /* Never block a request because of this shim. */
      }
      return nativeFetch.call(this, input, init);
    };
  }

  // ---- XMLHttpRequest ---------------------------------------------------
  var nativeOpen = XMLHttpRequest.prototype.open;
  var nativeSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__csrfMethod = method;
    this.__csrfUrl = url;
    return nativeOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (!this.__csrfHeaderSet &&
          UNSAFE_METHOD.test(this.__csrfMethod || '') &&
          isSameOrigin(this.__csrfUrl || '')) {
        var token = readToken();
        if (token) {
          this.setRequestHeader('X-CSRFToken', token);
          this.__csrfHeaderSet = true;
        }
      }
    } catch (e) {
      /* Header list already sent / locked — ignore. */
    }
    return nativeSend.apply(this, arguments);
  };
})();
