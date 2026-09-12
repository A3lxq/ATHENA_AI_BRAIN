"""Git automation for the Obsidian vault repository (docs/design/
git-automation.md), implementing ADR-0005's subprocess-wrapper decision.

Distinct from ATHENA AI-BRAIN's own software repository -- see
`docs/GIT_WORKFLOW.md`'s "Repository model": this package only ever
operates on `VaultRoot`, never on ATHENA AI-BRAIN's own checkout.
"""

from __future__ import annotations
