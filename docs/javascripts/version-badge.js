// Show the package version as a small badge next to the site title in the
// header. The version is emitted into a <meta> tag by the MkDocs hook so the
// value tracks pyproject.toml rather than being hard-coded here.
document.addEventListener("DOMContentLoaded", function () {
  var meta = document.querySelector('meta[name="veloce-version"]');
  var version = meta && meta.getAttribute("content");
  if (!version) return;
  var topic = document.querySelector(".md-header__topic");
  if (!topic || topic.querySelector(".md-version-badge")) return;
  var badge = document.createElement("span");
  badge.className = "md-version-badge";
  badge.textContent = "v" + version;
  topic.appendChild(badge);
});
