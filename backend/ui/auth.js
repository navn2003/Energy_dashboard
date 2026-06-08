/* auth.js — shared login gate.
   Include this FIRST in <head> on every protected page:
     <script src="auth.js"></script>
   It (1) redirects to login.html if not signed in, (2) attaches the token to
   every fetch, and (3) logs out on a 401. Expose logout() for a logout button. */
(function () {
  var LOGIN_PAGE = "login.html";
  var onLogin = location.pathname.split("/").pop() === LOGIN_PAGE;
  var token = sessionStorage.getItem("auth_token");

  // Gate: not signed in and not on the login page -> go to login.
  if (!token && !onLogin) {
    location.replace(LOGIN_PAGE);
    return;
  }

  // Attach Bearer token to all fetch calls; handle 401 by logging out.
  var _fetch = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    opts = opts || {};
    var t = sessionStorage.getItem("auth_token");
    if (t) {
      opts.headers = Object.assign({}, opts.headers || {}, { Authorization: "Bearer " + t });
    }
    return _fetch(url, opts).then(function (res) {
      if (res.status === 401) {
        sessionStorage.removeItem("auth_token");
        if (location.pathname.split("/").pop() !== LOGIN_PAGE) location.replace(LOGIN_PAGE);
      }
      return res;
    });
  };

  window.logout = function () {
    sessionStorage.removeItem("auth_token");
    location.replace(LOGIN_PAGE);
  };
})();
