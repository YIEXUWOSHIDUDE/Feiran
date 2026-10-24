"use strict";

// The public entry page: sign in, or one deliberate click for a guest workspace. The click sends
// back the start token the server put in this page and in a cookie, so the server makes at most
// one workspace for it however often it arrives. Nothing is started by just opening the page.
(() => {
  const I18n = window.WorkbenchI18n;
  const t = I18n.text;
  const meta = (name) => (document.querySelector(`meta[name="${name}"]`) || {}).content || "";
  const start = meta("guest-start");
  const resumable = meta("guest-state") === "guest";
  const ends = meta("guest-ends");
  const lifetime = Number(meta("guest-lifetime")) || 120;
  const message = document.getElementById("message");
  const button = document.getElementById("guest-start");
  const status = document.getElementById("guest-status");

  function show(text, kind = "error") {
    I18n.setText(message, text);
    message.className = `message ${kind}`;
    message.hidden = false;
  }

  I18n.mount();
  // Signing in comes back to the part of the app the visitor asked for.
  document.getElementById("sign-in").setAttribute("href", `/login?return_to=${encodeURIComponent("/" + location.hash)}`);
  I18n.setText(document.getElementById("guest-lifetime"),
    t`It ends ${lifetime} minutes after it starts, however much you use it, and everything in it is then deleted.`);
  const label = resumable ? t("Continue your guest workspace") : t("Try a guest workspace");
  I18n.setText(button, label);
  if (resumable && ends) {
    status.hidden = false;
    I18n.setText(status, t`This browser has a guest workspace open until ${I18n.date(ends, true)}.`);
  }
  if (new URLSearchParams(location.search).has("ended")) {
    show(t("Your guest workspace has ended, and everything in it was deleted. Start a new one or sign in."), "ok");
  }

  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    I18n.setText(button, t("Preparing your guest workspace…"));
    try {
      const response = await fetch("/guest/start", { method: "POST", headers: { "X-Guest-Start": start } });
      const body = await response.json().catch(() => ({}));
      // Signed in already: the account is opened as it is, never replaced by a guest.
      if (response.ok || body.code === "signed_in") {
        location.href = body.next || "/";
        return;
      }
      show(I18n.serverText(body.error || `HTTP ${response.status}`));
    } catch (error) {
      show(t("The guest workspace could not be started. Check your connection and try again."));
    }
    button.disabled = false;
    I18n.setText(button, label);
  });
})();
