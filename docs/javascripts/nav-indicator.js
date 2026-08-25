/*
 * Sliding active-item indicator for the table of contents and the page nav.
 *
 * The bar used to be a `::before` on whichever link was active, so changing
 * sections destroyed one element and created another - the bar blinked from one
 * place to the next. A single bar per list, moved with a transform, slides
 * instead, which reads as one indicator tracking the reader rather than several
 * appearing and disappearing.
 *
 * Position and height come from CSS custom properties so the animation itself
 * stays in the stylesheet; this file only measures. `left` is tracked too, so
 * the bar lines up with a nested table-of-contents entry's indent rather than
 * sitting detached from it.
 *
 * Two rules keep this from wedging the page, both learned the hard way:
 *
 *   1. The observer must ignore the list's own class changes. It watches the
 *      subtree for `class`, and the subtree includes the list - so writing a
 *      class onto the list re-triggered the observer, which wrote it again. The
 *      loop pegged the main thread and the page stopped responding to clicks.
 *   2. Nothing measures or writes styles inside an observer callback. Work is
 *      deferred to an animation frame and guarded by `pending`, so a burst of
 *      mutations costs one measurement rather than one each.
 *
 * The whole thing is wrapped so a failure here can never take the theme's own
 * navigation down with it.
 */
(function () {
  "use strict";

  var LISTS = [
    ".md-sidebar--secondary .md-nav--secondary > .md-nav__list",
    ".md-sidebar--primary .md-nav--primary > .md-nav__list",
  ];

  function track(list) {
    var pending = 0;
    var settled = false;

    /* Measure the active link and write its geometry onto the list. Called only
       from an animation frame, never from an observer callback. */
    function place(animate) {
      var active = list.querySelector(".md-nav__link--active");

      /* No active link (a page with no headings, or one scrolled above the
         first): fade the bar out where it stands rather than parking it at the
         top, which would read as a jump to an item that is not selected. */
      if (!active || !active.offsetParent) {
        list.classList.remove("veloce-nav--tracking");
        return;
      }

      var listBox = list.getBoundingClientRect();
      var linkBox = active.getBoundingClientRect();

      /* Relative to the list, so the bar is unaffected by page scroll and by
         the sidebar's own scroll position. */
      list.style.setProperty("--veloce-nav-y", (linkBox.top - listBox.top).toFixed(2) + "px");
      list.style.setProperty("--veloce-nav-h", linkBox.height.toFixed(2) + "px");
      list.style.setProperty("--veloce-nav-x", (linkBox.left - listBox.left).toFixed(2) + "px");

      /* The first placement lands without travelling to itself from the top.
         Done by class rather than by toggling a style so there is exactly one
         class write, on the frame the bar becomes visible. */
      if (!animate && !settled) list.classList.add("veloce-nav--instant");
      list.classList.add("veloce-nav--tracking");
      settled = true;
    }

    function schedule(animate) {
      if (pending) return;
      pending = requestAnimationFrame(function () {
        pending = 0;
        place(animate);
        if (list.classList.contains("veloce-nav--instant")) {
          /* Re-enable the transition on the frame *after* the placement, so the
             browser cannot coalesce the two and animate from the old position. */
          requestAnimationFrame(function () {
            list.classList.remove("veloce-nav--instant");
          });
        }
      });
    }

    /* Tells the stylesheet a driver is present, so it swaps the static
       per-item bar for the one this script moves. Without it the CSS keeps the
       static bar and the page is never worse off for this file failing. */
    list.classList.add("veloce-nav--js");
    schedule(false);

    /* Material toggles `--active` on the links as the reader scrolls, so watch
       the class rather than the scroll position - no work per frame. */
    var observer = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        /* Ignore our own writes. The list is inside the observed subtree, so
           reacting to its class changes would re-enter this observer forever. */
        if (records[i].target !== list) {
          schedule(true);
          return;
        }
      }
    });
    observer.observe(list, { subtree: true, attributes: true, attributeFilter: ["class"] });

    /* A resize changes wrapping, and therefore every item's height. */
    var onResize = function () {
      schedule(false);
    };
    window.addEventListener("resize", onResize, { passive: true });

    return function () {
      observer.disconnect();
      window.removeEventListener("resize", onResize);
      if (pending) cancelAnimationFrame(pending);
    };
  }

  var teardown = [];

  function setup() {
    try {
      teardown.forEach(function (fn) {
        fn();
      });
      teardown = [];
      LISTS.forEach(function (selector) {
        var list = document.querySelector(selector);
        if (list) teardown.push(track(list));
      });
    } catch (error) {
      /* Decorative: never let it break the page it decorates. */
      if (window.console) window.console.warn("nav-indicator disabled:", error);
    }
  }

  /* `navigation.instant` swaps the document without a page load, so the theme's
     own observable is what says a new page is ready. Fall back to the plain
     event when it is unavailable. */
  try {
    if (typeof window.document$ !== "undefined") {
      window.document$.subscribe(setup);
    } else if (document.readyState !== "loading") {
      setup();
    } else {
      document.addEventListener("DOMContentLoaded", setup);
    }
  } catch (error) {
    if (window.console) window.console.warn("nav-indicator disabled:", error);
  }
})();
