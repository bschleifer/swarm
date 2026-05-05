// swarm/web/static/ws-auth.js — shared authenticated-WebSocket helper.
//
// Pre-unification (before Phase B of the duplication sweep) every page
// that opened a WebSocket re-implemented the same three steps inline:
//
//   1. Build the ws:// or wss:// URL from location.host
//   2. Open the WebSocket
//   3. In ``onopen``, send ``{type: 'auth', token: <token>}`` so the
//      server's first-message auth gate accepts the connection
//
// Three call sites today:
//   - dashboard.js main /ws            (line 407 — connect())
//   - dashboard.js terminal /ws/term   (line 2156 — terminal reconnect)
//   - config.html  /ws                 (line 3389 — connectWs())
//
// The pre-Phase-A drift between dashboard.js and config.html is what
// produced the 2026.5.5.7 WS lockout (config.html read the auth token
// from sessionStorage only).  Phase A fixed *which token is read*;
// Phase B unifies *the act of opening + authenticating* itself, so a
// new authenticated-WS endpoint added to the app gets the same behavior
// without copying boilerplate.
//
// This module depends on ``swarmAuth`` (static/auth.js) for the token.
// Both must be loaded in the same page; auth.js must come first.
//
// Loaded synchronously from base.html before any page body script runs.
// Exposes a ``window.swarmWS`` namespace; nothing else.

(function () {
    'use strict';

    if (!window.swarmAuth) {
        // Hard fail loudly — opening a WS without an auth token is
        // exactly the bug Phase A fixed.  The script-tag order in
        // base.html should make this impossible, but guard anyway.
        throw new Error(
            'ws-auth.js loaded before auth.js — fix the script ordering ' +
            'in base.html (auth.js must come first)'
        );
    }

    function _wsUrl(path) {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        return proto + '//' + location.host + path;
    }

    window.swarmWS = {
        // Build a wss:// or ws:// URL for ``path`` (e.g. ``/ws`` or
        // ``/ws/terminal/foo``) using the current page's host and
        // protocol.  Exposed so callers that need just the URL (e.g.
        // for a manually-constructed WebSocket they want to inject
        // mid-flight) can use the same logic.
        url: _wsUrl,

        // Open a WebSocket against ``path`` and attach a one-shot
        // ``open`` listener that sends the JSON auth message the
        // server's first-message gate expects.  Caller installs its
        // own ``onmessage`` / ``onclose`` / ``onerror`` (and may also
        // install its own ``onopen`` — it will fire after our auth
        // listener thanks to event-listener registration order).
        //
        // The token is resolved through ``swarmAuth.getToken()`` at
        // open time, not at call time, so a token that arrives via a
        // late ``setServerToken`` (e.g. a config-page response that
        // refreshes the token mid-session) still gets used.
        //
        // Returns the WebSocket instance.  Does not handle
        // reconnection — that policy belongs to the caller (the
        // dashboard backs off exponentially, the config page polls
        // every 3s, terminals have their own first-payload watchdog).
        openAuthenticated: function (path) {
            var ws = new WebSocket(_wsUrl(path));
            ws.addEventListener('open', function () {
                try {
                    ws.send(JSON.stringify({
                        type: 'auth',
                        token: window.swarmAuth.getToken(),
                    }));
                } catch (e) {
                    // The most likely cause is the socket flipped to
                    // CLOSING between ``open`` firing and our send.
                    // Caller's ``onclose`` will pick up the cleanup.
                    console.error('[swarm-ws] auth send failed:', e);
                }
            });
            return ws;
        },
    };
})();
