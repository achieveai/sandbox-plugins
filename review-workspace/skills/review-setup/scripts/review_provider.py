"""The single dispatch point for everything review-setup asks a hosting provider.

Azure DevOps was assumed structurally throughout the scripts. This module gathers the six
provider-shaped calls behind one interface so a second provider is an addition rather than
an edit. ``AdoProvider`` delegates to the existing functions verbatim -- no logic moved --
so the ADO path is byte-stable.

``GitHubProvider`` is a deliberate, visible stub: every method refuses, so selecting it can
never produce a half-prepared workspace. The adapter lands in Slice 3.
"""
from __future__ import annotations

from review_common import AdoClient, ReviewError, canonical_ado_metadata_url, parse_ado_url

GITHUB_REFUSAL = (
    "GitHub provider is not implemented in review-setup 0.1.x; use --provider ado"
)
GITHUB_EFFECT_DETAIL = "github adapter lands in Slice 3"


class AdoProvider:
    """Azure DevOps, the only implemented provider."""

    name = "ado"

    def ensure_available(self) -> None:
        """Called before any filesystem or Git work; ADO is always available."""

    def parse_repo_url(self, url: str) -> dict:
        return parse_ado_url(url)

    def canonical_metadata_url(self, url: str, org_url: str) -> str:
        return canonical_ado_metadata_url(url, org_url)

    def make_client(self, org_url: str):
        return AdoClient(org_url)

    def resolve_pr(self, client, pr_number: int, iteration: int | None = None) -> dict:
        from review_context import resolve_pr

        return resolve_pr(client, pr_number, iteration)

    def select_module(self, client, snapshot: dict, modules: list[dict]) -> dict:
        # Imported lazily: review_setup imports this module, so a top-level import here
        # would close the cycle.
        from review_setup import select_module

        return select_module(client, snapshot, modules)


class GitHubProvider:
    """Every method refuses, before it can touch anything."""

    name = "github"

    @staticmethod
    def _refuse():
        raise ReviewError(
            GITHUB_REFUSAL, 1,
            effect_type="unsupported_provider", effect_detail=GITHUB_EFFECT_DETAIL,
        )

    def ensure_available(self) -> None:
        self._refuse()

    def parse_repo_url(self, url: str) -> dict:
        self._refuse()

    def canonical_metadata_url(self, url: str, org_url: str) -> str:
        self._refuse()

    def make_client(self, org_url: str):
        self._refuse()

    def resolve_pr(self, client, pr_number: int, iteration: int | None = None) -> dict:
        self._refuse()

    def select_module(self, client, snapshot: dict, modules: list[dict]) -> dict:
        self._refuse()


PROVIDERS = {"ado": AdoProvider, "github": GitHubProvider}


def get_provider(name: str | None):
    """Resolve a ``--provider`` value; ``None`` means the default, Azure DevOps."""
    try:
        return PROVIDERS[name or "ado"]()
    except KeyError:
        raise ReviewError(f"Unknown provider {name!r}.") from None
