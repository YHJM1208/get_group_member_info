"""Unit tests for the pure helpers in handlers.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# _resolve_limit(raw: str | None, mode: str) -> int
# ---------------------------------------------------------------------------
def test_resolve_limit_default_modes(handlers):
    assert handlers._resolve_limit(None, "all") == 20
    assert handlers._resolve_limit(None, "default") == 50
    assert handlers._resolve_limit(None, "count") == 50


@pytest.mark.parametrize(
    ("raw", "mode", "expected"),
    [
        ("1", "default", 1),
        ("5", "default", 5),
        ("20", "all", 20),
        ("50", "all", 50),
        ("100", "default", 100),
        ("101", "default", 100),
        ("999", "all", 100),
        (" 30 ", "default", 30),
    ],
)
def test_resolve_limit_values(handlers, raw, mode, expected):
    assert handlers._resolve_limit(raw, mode) == expected


@pytest.mark.parametrize("raw", ["abc", "1.5", "", "  "])
def test_resolve_limit_rejects_non_integer(handlers, raw):
    with pytest.raises(ValueError) as excinfo:
        handlers._resolve_limit(raw, "default")
    assert str(excinfo.value) == "limit 必须是整数"


@pytest.mark.parametrize("raw", ["0", "-1", "-100"])
def test_resolve_limit_rejects_non_positive(handlers, raw):
    with pytest.raises(ValueError) as excinfo:
        handlers._resolve_limit(raw, "default")
    assert str(excinfo.value) == "limit 必须大于 0"


# ---------------------------------------------------------------------------
# SEX_ALIAS
# ---------------------------------------------------------------------------
def test_sex_alias_mapping(handlers):
    assert handlers.SEX_ALIAS == {
        "m": "male",
        "male": "male",
        "f": "female",
        "female": "female",
        "u": "unknown",
        "unknown": "unknown",
    }


# ---------------------------------------------------------------------------
# _parse_number_condition(raw) -> tuple[str, int, int | None] | None
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30", ("=", 30, None)),
        ("0", ("=", 0, None)),
        ("=30", ("=", 30, None)),
        (">30", (">", 30, None)),
        (">=30", (">=", 30, None)),
        ("<30", ("<", 30, None)),
        ("<=30", ("<=", 30, None)),
        ("> 30", (">", 30, None)),
        (">= 30", (">=", 30, None)),
        ("  >30  ", (">", 30, None)),
        ("30-40", ("range", 30, 40)),
        ("40-30", ("range", 30, 40)),
        ("30~40", ("range", 30, 40)),
        ("0-0", ("range", 0, 0)),
        ("＞30", (">", 30, None)),
        ("＜30", ("<", 30, None)),
        ("≥30", (">=", 30, None)),
        ("≤30", ("<=", 30, None)),
    ],
)
def test_parse_number_condition_valid(handlers, raw, expected):
    assert handlers._parse_number_condition(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "30.5",
        "-30",
        "30-",
        "-40",
        ">",
        ">=",
        "30-40-50",
        "90 100",
    ],
)
def test_parse_number_condition_invalid(handlers, raw):
    assert handlers._parse_number_condition(raw) is None


# ---------------------------------------------------------------------------
# _split_args(text) -> list[str]
# ---------------------------------------------------------------------------
def test_split_args_plain(handlers):
    assert handlers._split_args("card=技术 count") == [
        "card=技术",
        "count",
    ]
    assert handlers._split_args("") == []


def test_split_args_quoted_spaces(handlers):
    assert handlers._split_args('card="技术 部" count') == [
        "card=技术 部",
        "count",
    ]
    assert handlers._split_args("card='技术 部' role=admin") == [
        "card=技术 部",
        "role=admin",
    ]


def test_split_args_falls_back_on_unbalanced_quote(handlers):
    assert handlers._split_args("card=Don't") == ["card=Don't"]


# ---------------------------------------------------------------------------
# _member_compact_line(member, conditions) -> str
# ---------------------------------------------------------------------------
@pytest.fixture
def member():
    return SimpleNamespace(
        user_id=12345,
        nickname="小张",
        card="技术部",
        sex="male",
        role="admin",
        level="95",
        age=25,
    )


def test_member_compact_default_fields(handlers, member):
    assert (
        handlers._member_compact_line(member, {})
        == "12345 技术部 (sex=male role=admin)"
    )


@pytest.mark.parametrize(
    ("conditions", "suffix"),
    [
        ({"level": ">=90"}, "level=95"),
        ({"age": ">20"}, "age=25"),
        ({"role": "admin"}, "role=admin"),
        ({"sex": "male"}, "sex=male"),
        ({"card": "技术"}, "card=技术部"),
        ({"nickname": "张"}, "nickname=小张"),
    ],
)
def test_member_compact_shows_queried_field(
    handlers, member, conditions, suffix
):
    assert handlers._member_compact_line(member, conditions) == (
        f"12345 技术部 ({suffix})"
    )


def test_member_compact_multiple_conditions_keep_order(handlers, member):
    line = handlers._member_compact_line(
        member, {"level": ">=90", "role": "admin", "user_id": ">1"}
    )
    assert line == "12345 技术部 (level=95 role=admin)"


def test_member_compact_user_id_only_has_no_suffix(handlers, member):
    assert (
        handlers._member_compact_line(member, {"user_id": ">1000"})
        == "12345 技术部"
    )


def test_member_compact_card_fallback_and_empty_card(handlers):
    empty_card = SimpleNamespace(
        user_id=1,
        nickname="无名片",
        card="",
        sex="unknown",
        role="member",
        level="1",
        age=0,
    )
    assert (
        handlers._member_compact_line(empty_card, {"card": "无名"})
        == "1 无名片 (card=无)"
    )
    assert (
        handlers._member_compact_line(empty_card, {})
        == "1 无名片 (sex=unknown role=member)"
    )


def test_member_compact_none_card_falls_back_to_nickname(handlers):
    member = SimpleNamespace(
        user_id=2,
        nickname="昵称",
        card=None,
        sex="female",
        role="member",
        level="2",
        age=18,
    )
    assert (
        handlers._member_compact_line(member, {"card": "昵"})
        == "2 昵称 (card=无)"
    )
