// swarm/web/static/auth.js — shared auth-token resolution for the dashboard.
//
// Pre-unification (before Phase A of the duplication sweep) the dashboard
// page (dashboard.js) and the config page (config.html inline script) each
// resolved the WS-auth / Bearer-auth token independently:
//
//   - dashboard.js read `_swarmCfg.wsToken` (server-injected) → falling
//     back to sessionStorage['swarm_api_password'] → falling back to ''
//   - config.html read sessionStorage['swarm_api_password'] only — which
//     is empty for cookie-authenticated logins, so the config page's WS
//     opened with `token: ''` and tripped the per-IP lockout, blocking
//     the dashboard's WS too. Real bug, shipped as the fix in 2026.5.5.7.
//
// This module is the canonical resolver.  Both pages now read the token
// through `window.swarmAuth.getToken()`.  Adding a new authenticated
// page or path means using the same helper — no second copy to drift.
//
// Loaded synchronously from base.html before any page body script runs.
// Exposes a `window.swarmAuth` namespace; nothing else.

(function () {
    'use strict';

    var SESSION_STORAGE_KEY = 'swarm_api_password';

    // Server-injected token, set once at page load by:
    //   - dashboard.html: from `<script id="swarm-config">{...,wsToken: "..."}</script>`
    //     parsed by dashboard.js after this module loads
    //   - config.html: from `{{ ws_token|tojson }}` injected by the config
    //     page handler (since 2026.5.5.7)
    //
    // Pages call `window.swarmAuth.setServerToken(value)` in their inline
    // script after parsing their server-injected config.  Until that call,
    // `getToken()` falls through to sessionStorage.
    var _serverToken = '';

    function _getSession() {
        try {
            return sessionStorage.getItem(SESSION_STORAGE_KEY) || '';
        } catch (e) {
            return '';
        }
    }

    function _setSession(value) {
        try {
            if (value) {
                sessionStorage.setItem(SESSION_STORAGE_KEY, value);
            } else {
                sessionStorage.removeItem(SESSION_STORAGE_KEY);
            }
        } catch (e) {
            // Private browsing / storage disabled — silently degrade.
        }
    }

    window.swarmAuth = {
        // Set the server-injected token.  Call once per page load from the
        // page's inline script, after parsing the server-rendered config
        // (which embeds the token).  Clears any stale sessionStorage entry
        // that disagrees with the server's value.
        setServerToken: function (value) {
            _serverToken = value || '';
            // If a saved session token disagrees with the server's token,
            // clear it.  A stale user-entered token can break WS auth and
            // make terminal input look dead.  This was the
            // `maybeClearStaleSessionToken` logic in dashboard.js,
            // generalized so config.html benefits too.
            if (_serverToken) {
                var saved = _getSession();
                if (saved && saved !== _serverToken) {
                    _setSession('');
                }
            }
        },

        // Canonical token resolution — server-injected wins, sessionStorage
        // fallback for password-prompt flows, empty string if neither.
        getToken: function () {
            return _serverToken || _getSession() || '';
        },

        // For the password-prompt flow: store the user-entered password
        // so subsequent requests can use it.  Currently only invoked from
        // the login page; exposed here so future flows have one place to
        // call.
        setSessionToken: function (value) {
            _setSession(value);
        },

        // Force-clear the saved sessionStorage token.  Used after the
        // server returns 401 — the token we have is dead.
        clearSessionToken: function () {
            _setSession('');
        },

        // Re-run the stale-clear check at runtime — useful from
        // ``ws.onclose`` / ``ws.onerror`` handlers where a previously-
        // valid sessionStorage token may have been invalidated by a
        // concurrent login from another tab.  Returns true if it
        // cleared a stale entry.
        clearStaleSessionToken: function () {
            if (!_serverToken) return false;
            var saved = _getSession();
            if (!saved || saved === _serverToken) return false;
            _setSession('');
            return true;
        },
    };
})();
