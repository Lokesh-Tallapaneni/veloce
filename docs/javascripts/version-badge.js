// Show the package version as a small badge next to the site title in the
// header. The version is emitted into a <meta> tag by the MkDocs hook so the
// value tracks pyproject.toml rather than being hard-coded here.
//
// Runs on every page, not just the first. `navigation.instant` swaps the
// document without a page load, and the header topic is one of the components
// it replaces - so a `DOMContentLoaded` listener fired once, the badge was
// destroyed by the first client-side navigation, and nothing put it back. The
// theme's `document$` emits per page, which is what "every page" means here.
//
// Appending is guarded, so running again on a header that already has a badge
// is a no-op rather than a duplicate.
(function () {
  "use strict";

  function addBadge() {
    try {
      var meta = document.querySelector('meta[name="veloce-version"]');
      var version = meta && meta.getAttribute("content");
      if (!version) return;
      var topic = document.querySelector(".md-header__topic");
      if (!topic || topic.querySelector(".md-version-badge")) return;
      var badge = document.createElement("span");
      badge.className = "md-version-badge";
      badge.textContent = "v" + version;
      topic.appendChild(badge);
    } catch (error) {
      /* Decorative: never let it break the page it decorates. */
      if (window.console) window.console.warn("version-badge disabled:", error);
    }
  }

  if (typeof window.document$ !== "undefined") {
    window.document$.subscribe(addBadge);
  } else if (document.readyState !== "loading") {
    addBadge();
  } else {
    document.addEventListener("DOMContentLoaded", addBadge);
  }
})();
