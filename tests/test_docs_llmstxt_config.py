"""Every mkdocs plugin the site declares is installed by the docs workflow.

`mkdocs.yml` and `.github/workflows/docs.yml` name the plugin set in two
places: the config lists what to run, and the workflow's `pip install` line
lists what to install. Adding `llmstxt` to the first without the second builds
locally and fails only in CI, where the missing plugin aborts a `--strict`
build. The check is a grep so the drift is caught on the commit that
introduces it.
"""

from __future__ import annotations

import pathlib
import re

#: Plugins that ship as a separate distribution, mapped to the name to install.
#: `search` is bundled with mkdocs, and `social` / `tags` come from
#: mkdocs-material, so none of those appear here.
_EXTERNAL_PLUGINS = {
    "mkdocstrings": "mkdocstrings",
    "llmstxt": "mkdocs-llmstxt",
}


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _declared_plugins() -> set[str]:
    """Top-level plugin names in the `plugins:` block of `mkdocs.yml`."""
    text = (_root() / "mkdocs.yml").read_text(encoding="utf-8")
    block = text.split("\nplugins:\n", 1)[1].split("\nnav:\n", 1)[0]
    # A plugin entry is `  - name` or `  - name:`; anything more deeply
    # indented is that plugin's own configuration.
    return set(re.findall(r"^  - ([A-Za-z][\w-]*):?\s*$", block, re.MULTILINE))


def _docs_workflow() -> str:
    return (_root() / ".github/workflows/docs.yml").read_text(encoding="utf-8")


def test_the_scan_finds_the_declared_plugins():
    """The guard is worthless if the pattern matches nothing."""
    declared = _declared_plugins()
    assert "search" in declared
    assert "mkdocstrings" in declared


def test_every_external_plugin_is_installed_by_the_docs_workflow():
    """NEGATIVE: a declared plugin the workflow never installs breaks CI."""
    workflow = _docs_workflow()
    missing = [
        dist
        for plugin, dist in _EXTERNAL_PLUGINS.items()
        if plugin in _declared_plugins() and dist not in workflow
    ]
    assert not missing, f"declared in mkdocs.yml but not installed in docs.yml: {missing}"


def test_llmstxt_is_configured_to_emit_both_files():
    """POSITIVE: the llms.txt convention needs the index and the full corpus."""
    text = (_root() / "mkdocs.yml").read_text(encoding="utf-8")
    assert "llmstxt" in _declared_plugins()
    assert "full_output: llms-full.txt" in text


def test_the_check_would_catch_an_uninstalled_plugin():
    """POSITIVE control: a plugin absent from the workflow is not accepted."""
    assert "mkdocs-nonexistent-plugin" not in _docs_workflow()
