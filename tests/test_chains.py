"""Tests for the chain registry.

These matter more than they look: an address-normalization bug produces
duplicate tokens and duplicate Telegram alerts, which is the single most
annoying failure mode of a scanner like this.
"""

from __future__ import annotations

import pytest

from mihq.chains import (
    CHAINS,
    InvalidAddressError,
    UnknownChainError,
    all_chain_keys,
    chain_from_dexscreener,
    chain_from_geckoterminal,
    enabled_chains,
    get_chain,
    is_valid_address,
    normalize_address,
    unknown_chain_keys,
)

# A real-looking EVM address and a real-looking Solana mint, used throughout.
EVM = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_all_five_required_chains_present():
    assert set(all_chain_keys()) == {"solana", "ethereum", "base", "bsc", "avalanche"}


def test_registry_keys_match_their_spec_key():
    for key, spec in CHAINS.items():
        assert key == spec.key


def test_provider_ids_are_unique():
    """Two chains sharing a provider id would silently cross-contaminate data."""
    ds = [c.dexscreener_chain_id for c in CHAINS.values()]
    gt = [c.geckoterminal_network for c in CHAINS.values()]
    assert len(ds) == len(set(ds))
    assert len(gt) == len(set(gt))


def test_get_chain_is_case_insensitive_on_the_key():
    assert get_chain("BASE").key == "base"
    assert get_chain("  solana  ").key == "solana"


def test_get_chain_rejects_unknown():
    with pytest.raises(UnknownChainError):
        get_chain("dogechain")


def test_unknown_chain_keys_reports_only_the_bad_ones():
    assert unknown_chain_keys(["base", "nope", "solana"]) == ("nope",)
    assert unknown_chain_keys(["base", "solana"]) == ()


def test_enabled_chains_returns_specs_in_registry_order():
    specs = enabled_chains(["base", "solana"])
    assert [s.key for s in specs] == ["solana", "base"]


def test_enabled_chains_raises_on_unknown():
    with pytest.raises(UnknownChainError):
        enabled_chains(["base", "fakechain"])


# --- Address normalization: the duplicate-prevention logic ------------------

def test_evm_address_is_lowercased():
    """Same token in mixed case must collapse to one canonical form."""
    assert normalize_address("ethereum", EVM) == EVM.lower()
    assert normalize_address("ethereum", EVM.lower()) == EVM.lower()
    assert normalize_address("ethereum", EVM.upper().replace("0X", "0x")) == EVM.lower()


def test_evm_mixed_case_variants_all_produce_one_key():
    variants = [EVM, EVM.lower(), "0x" + EVM[2:].upper()]
    assert len({normalize_address("base", v) for v in variants}) == 1


def test_solana_address_case_is_preserved():
    """Lowercasing a base58 address would change it into a different address."""
    assert normalize_address("solana", SOL) == SOL
    assert normalize_address("solana", SOL) != SOL.lower()


def test_whitespace_is_trimmed():
    assert normalize_address("base", f"  {EVM}  ") == EVM.lower()
    assert normalize_address("solana", f"\t{SOL}\n") == SOL


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "0x123",                                      # too short
        EVM[:-1],                                     # one char short
        EVM + "a",                                    # one char long
        "0xZZb86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # non-hex
        EVM[2:],                                      # missing 0x prefix
        SOL,                                          # base58 is not valid EVM
    ],
)
def test_invalid_evm_addresses_are_rejected(bad):
    with pytest.raises(InvalidAddressError):
        normalize_address("ethereum", bad)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "short",
        "0OIl" * 10,       # base58 excludes 0, O, I and l
        SOL + "x" * 10,    # too long
    ],
)
def test_invalid_solana_addresses_are_rejected(bad):
    with pytest.raises(InvalidAddressError):
        normalize_address("solana", bad)


def test_is_valid_address_never_raises():
    assert is_valid_address("base", EVM) is True
    assert is_valid_address("base", "nonsense") is False
    assert is_valid_address("not_a_chain", EVM) is False


# --- Provider id mapping ---------------------------------------------------

def test_dexscreener_mapping_round_trips():
    for spec in CHAINS.values():
        assert chain_from_dexscreener(spec.dexscreener_chain_id) == spec.key


def test_geckoterminal_mapping_round_trips():
    for spec in CHAINS.values():
        assert chain_from_geckoterminal(spec.geckoterminal_network) == spec.key


def test_unsupported_provider_chain_returns_none_rather_than_raising():
    """Discovery sees every chain the provider covers and must skip ours quietly."""
    assert chain_from_dexscreener("sui") is None
    assert chain_from_dexscreener(None) is None
    assert chain_from_dexscreener("") is None
    assert chain_from_geckoterminal("ton") is None


# --- Link building ---------------------------------------------------------

def test_explorer_and_dexscreener_links():
    base = get_chain("base")
    assert base.token_explorer_link(EVM.lower()) == f"https://basescan.org/token/{EVM.lower()}"
    assert base.tx_explorer_link("0xdead") == "https://basescan.org/tx/0xdead"
    assert base.dexscreener_token_link(EVM.lower()) == f"https://dexscreener.com/base/{EVM.lower()}"


def test_every_chain_has_working_url_templates():
    """A template with the wrong placeholder name would raise at alert time."""
    for spec in CHAINS.values():
        assert spec.token_explorer_link("ADDR").endswith("ADDR")
        assert spec.tx_explorer_link("HASH").endswith("HASH")
        assert spec.token_explorer_link("ADDR").startswith("https://")
