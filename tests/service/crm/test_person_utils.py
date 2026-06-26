from __future__ import annotations

from src.service.crm.utils.person import split_full_name


def test_split_full_name_splits_first_space_only() -> None:
    assert split_full_name("Jane Mary Doe") == ("Jane", "Mary Doe")


def test_split_full_name_returns_none_last_name_for_single_name() -> None:
    assert split_full_name("Madonna") == ("Madonna", None)


def test_split_full_name_trims_outer_whitespace() -> None:
    assert split_full_name("  Jane Doe  ") == ("Jane", "Doe")
