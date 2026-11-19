"""Chain registry: the one place that knows about each blockchain we support.

WHY THIS FILE EXISTS
--------------------
Every data provider spells chain names differently. DEX Screener calls BNB
Chain "bsc"; GeckoTerminal calls it "bsc" too, but calls Avalanche "avax"
while DEX Screener calls it "avalanche". Addresses differ as well: Ethereum
addresses are hex and case-insensitive, Solana addresses are base58 and
case-SENSITIVE.

Rather than scatter those differences through the scanner, every chain fact
lives in one `ChainSpec` below. To add a new chain later you add one entry
here. No scanner code changes.

THE CASE-SENSITIVITY TRAP
-------------------------
If you store the same Ethereum token twice, once as `0xABC...` and once as
`0xabc...`, you get duplicate tokens and duplicate alerts. So EVM addresses
are lowercased on the way in. Solana addresses must NOT be lowercased, since
base58 uses upper and lower case as distinct characters - lowercasing one
would produce a different, wrong address. `normalize_address` handles both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

AddressStyle = Literal["evm", "solana"]

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Base58 excludes the easily-confused characters 0, O, I and l.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class UnknownChainError(KeyError):
    """Raised when code asks for a chain that is not in the registry."""


class InvalidAddressError(ValueError):
    """Raised when an address does not match the chain's address format."""


@dataclass(frozen=True)
class ChainSpec:
    """Everything the app needs to know about one blockchain."""

    key: str
    """Our internal name. Used in the database and in .env (ENABLED_CHAINS)."""

    display_name: str
    """Human-readable name used in Telegram messages."""

    native_symbol: str
    """Ticker of the gas token, e.g. ETH, SOL, BNB."""

    address_style: AddressStyle
    """Which address format and normalization rules apply."""

    dexscreener_chain_id: str
    """The value DEX Screener uses in its `chainId` field and URLs.

    VERIFIED 2026-11-15 against DEX Screener API documentation. The chainId
    also matches the URL path on dexscreener.com, which is a handy check:
    dexscreener.com/<chainId>/<pairAddress>.
    """

    geckoterminal_network: str
    """The network id GeckoTerminal uses in its URL paths.

    VERIFIED 2026-11-15 against the GeckoTerminal /networks endpoint docs.
    Careful: these are NOT the CoinGecko asset-platform ids. Ethereum is
    `eth` here but `ethereum` on CoinGecko, and Avalanche is `avax` here but
    `avalanche` on DEX Screener. Mixing them up returns empty results rather
    than an error, which is the nastiest kind of bug.
    """

    goplus_chain_id: str | None
    """Chain id for the GoPlus security API, or None if it uses a separate
    endpoint (Solana does).

    These are the standard EVM chain ids: Ethereum 1, BSC 56, Base 8453,
    Avalanche 43114. Solana is not an EVM chain and has its own path.
    """

    explorer_token_url: str
    """Template for a token page. Use `.format(address=...)`."""

    explorer_tx_url: str
    """Template for a transaction page. Use `.format(tx_hash=...)`."""

    def token_explorer_link(self, address: str) -> str:
        """Public block-explorer link for a token, used as an alert source link."""
        return self.explorer_token_url.format(address=address)

    def tx_explorer_link(self, tx_hash: str) -> str:
        return self.explorer_tx_url.format(tx_hash=tx_hash)

    def dexscreener_token_link(self, address: str) -> str:
        """Public DEX Screener page for a token on this chain."""
        return f"https://dexscreener.com/{self.dexscreener_chain_id}/{address}"


# ---------------------------------------------------------------------------
# THE REGISTRY
#
# To add a chain: copy an entry, fill in the provider ids and explorer URLs,
# then add its key to ENABLED_CHAINS in .env. Nothing else needs editing.
# ---------------------------------------------------------------------------

CHAINS: dict[str, ChainSpec] = {
    "solana": ChainSpec(
        key="solana",
        display_name="Solana",
        native_symbol="SOL",
        address_style="solana",
        dexscreener_chain_id="solana",
        geckoterminal_network="solana",
        goplus_chain_id=None,
        explorer_token_url="https://solscan.io/token/{address}",
        explorer_tx_url="https://solscan.io/tx/{tx_hash}",
    ),
    "ethereum": ChainSpec(
        key="ethereum",
        display_name="Ethereum",
        native_symbol="ETH",
        address_style="evm",
        dexscreener_chain_id="ethereum",
        geckoterminal_network="eth",
        goplus_chain_id="1",
        explorer_token_url="https://etherscan.io/token/{address}",
        explorer_tx_url="https://etherscan.io/tx/{tx_hash}",
    ),
    "base": ChainSpec(
        key="base",
        display_name="Base",
        native_symbol="ETH",
        address_style="evm",
        dexscreener_chain_id="base",
        geckoterminal_network="base",
        goplus_chain_id="8453",
        explorer_token_url="https://basescan.org/token/{address}",
        explorer_tx_url="https://basescan.org/tx/{tx_hash}",
    ),
    "bsc": ChainSpec(
        key="bsc",
        display_name="BNB Chain",
        native_symbol="BNB",
        address_style="evm",
        dexscreener_chain_id="bsc",
        geckoterminal_network="bsc",
        goplus_chain_id="56",
        explorer_token_url="https://bscscan.com/token/{address}",
        explorer_tx_url="https://bscscan.com/tx/{tx_hash}",
    ),
    "avalanche": ChainSpec(
        key="avalanche",
        display_name="Avalanche",
        native_symbol="AVAX",
        address_style="evm",
        dexscreener_chain_id="avalanche",
        geckoterminal_network="avax",
        goplus_chain_id="43114",
        explorer_token_url="https://snowtrace.io/token/{address}",
        explorer_tx_url="https://snowtrace.io/tx/{tx_hash}",
    ),
}

# Reverse lookups so provider responses can be mapped back to our keys.
_BY_DEXSCREENER: dict[str, str] = {c.dexscreener_chain_id: c.key for c in CHAINS.values()}
_BY_GECKOTERMINAL: dict[str, str] = {c.geckoterminal_network: c.key for c in CHAINS.values()}


def all_chain_keys() -> tuple[str, ...]:
    """Every chain key the registry knows about, in registry order."""
    return tuple(CHAINS.keys())


def get_chain(key: str) -> ChainSpec:
    """Look up a chain by our internal key. Raises `UnknownChainError`."""
    try:
        return CHAINS[key.strip().lower()]
    except KeyError as exc:
        raise UnknownChainError(
            f"Unknown chain {key!r}. Known chains: {', '.join(all_chain_keys())}"
        ) from exc


def unknown_chain_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Which of `keys` are not in the registry. Used to validate .env."""
    return tuple(k for k in keys if k.strip().lower() not in CHAINS)


def enabled_chains(keys: Iterable[str]) -> tuple[ChainSpec, ...]:
    """Resolve configured chain keys into `ChainSpec` objects, in registry order."""
    wanted = {k.strip().lower() for k in keys}
    unknown = unknown_chain_keys(wanted)
    if unknown:
        raise UnknownChainError(
            f"Unknown chains in configuration: {', '.join(sorted(unknown))}. "
            f"Known chains: {', '.join(all_chain_keys())}"
        )
    return tuple(spec for key, spec in CHAINS.items() if key in wanted)


def chain_from_dexscreener(chain_id: str | None) -> str | None:
    """Map a DEX Screener `chainId` to our key, or None if we do not support it.

    Returning None (rather than raising) is deliberate: the discovery scanner
    receives tokens from every chain DEX Screener covers and must quietly skip
    the ones we do not track.
    """
    if not chain_id:
        return None
    return _BY_DEXSCREENER.get(chain_id.strip().lower())


def chain_from_geckoterminal(network: str | None) -> str | None:
    """Map a GeckoTerminal network id to our key, or None if unsupported."""
    if not network:
        return None
    return _BY_GECKOTERMINAL.get(network.strip().lower())


def normalize_address(chain_key: str, address: str) -> str:
    """Return the canonical form of `address` for this chain.

    EVM addresses are lowercased so the same token is never stored twice.
    Solana addresses are returned unchanged because base58 is case-sensitive.

    Raises
    ------
    InvalidAddressError
        If the address does not match the chain's expected format. Rejecting
        early keeps malformed data out of the database entirely.
    """
    spec = get_chain(chain_key)
    candidate = address.strip()

    if not candidate:
        raise InvalidAddressError(f"Empty address for chain {spec.key}.")

    if spec.address_style == "evm":
        if not _EVM_ADDRESS_RE.match(candidate):
            raise InvalidAddressError(
                f"{candidate!r} is not a valid {spec.display_name} address "
                "(expected 0x followed by 40 hex characters)."
            )
        return candidate.lower()

    # Solana / base58
    if not _BASE58_RE.match(candidate):
        raise InvalidAddressError(
            f"{candidate!r} is not a valid {spec.display_name} address "
            "(expected 32-44 base58 characters)."
        )
    return candidate


def is_valid_address(chain_key: str, address: str) -> bool:
    """Non-raising version of `normalize_address`, for filtering provider data."""
    try:
        normalize_address(chain_key, address)
    except (InvalidAddressError, UnknownChainError):
        return False
    return True
