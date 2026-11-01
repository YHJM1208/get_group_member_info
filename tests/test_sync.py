"""Unit tests for sync.py's pure row mapper."""

from __future__ import annotations

import copy

import pytest


FULL_MEMBER = {
    "user_id": 10001,
    "nickname": "昵称",
    "card": "名片",
    "sex": "male",
    "age": 25,
    "area": "上海",
    "join_time": 1_700_000_000,
    "last_sent_time": 1_700_000_001,
    "level": "30",
    "role": "admin",
    "unfriendly": True,
    "title": "头衔",
    "title_expire_time": 1_700_000_002,
    "card_changeable": False,
    "shut_up_timestamp": 1_700_000_003,
    "is_robot": True,
}


def _expected_full(group_id=123):
    return {
        "group_id": group_id,
        "user_id": 10001,
        "nickname": "昵称",
        "card": "名片",
        "sex": "male",
        "age": 25,
        "area": "上海",
        "join_time": 1_700_000_000,
        "last_sent_time": 1_700_000_001,
        "level": "30",
        "role": "admin",
        "unfriendly": True,
        "title": "头衔",
        "title_expire_time": 1_700_000_002,
        "card_changeable": False,
        "shut_up_timestamp": 1_700_000_003,
        "is_robot": True,
    }


def test_row_maps_all_fields(sync):
    assert sync._row(123, FULL_MEMBER) == _expected_full()


def test_row_does_not_mutate_input(sync):
    original = copy.deepcopy(FULL_MEMBER)
    sync._row(123, FULL_MEMBER)
    assert FULL_MEMBER == original


def test_row_ignores_unknown_fields(sync):
    member = dict(FULL_MEMBER, unknown_field="ignored", in_group=True)
    result = sync._row(123, member)
    assert "unknown_field" not in result
    assert "in_group" not in result
    assert result == _expected_full()


def test_row_defaults_only_user_id(sync):
    result = sync._row(456, {"user_id": 20002})
    assert result == {
        "group_id": 456,
        "user_id": 20002,
        "nickname": "",
        "card": "",
        "sex": "unknown",
        "age": 0,
        "area": "",
        "join_time": 0,
        "last_sent_time": 0,
        "level": "",
        "role": "member",
        "unfriendly": False,
        "title": "",
        "title_expire_time": 0,
        "card_changeable": True,
        "shut_up_timestamp": 0,
        "is_robot": False,
    }


def test_row_none_strings_become_empty(sync):
    result = sync._row(
        789,
        {
            "user_id": 30003,
            "nickname": None,
            "card": None,
            "sex": None,
            "age": None,
            "area": None,
            "level": None,
            "role": None,
            "title": None,
        },
    )
    assert result["nickname"] == ""
    assert result["card"] == ""
    assert result["sex"] == "unknown"
    assert result["age"] == 0
    assert result["area"] == ""
    assert result["level"] == ""
    assert result["role"] == "member"
    assert result["title"] == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, False), (1, True), (False, False), (True, True)],
)
def test_row_boolean_fields(sync, value, expected):
    result = sync._row(
        1,
        {
            "user_id": 40004,
            "unfriendly": value,
            "card_changeable": value,
            "is_robot": value,
        },
    )
    assert result["unfriendly"] is expected
    assert result["card_changeable"] is expected
    assert result["is_robot"] is expected


def test_row_boolean_defaults(sync):
    result = sync._row(1, {"user_id": 50005})
    assert result["unfriendly"] is False
    assert result["card_changeable"] is True
    assert result["is_robot"] is False


def test_row_numeric_zero_is_preserved(sync):
    result = sync._row(
        1,
        {
            "user_id": 60006,
            "age": 0,
            "join_time": 0,
            "last_sent_time": 0,
            "title_expire_time": 0,
            "shut_up_timestamp": 0,
        },
    )
    assert result["age"] == 0
    assert result["join_time"] == 0
    assert result["last_sent_time"] == 0
    assert result["title_expire_time"] == 0
    assert result["shut_up_timestamp"] == 0


def test_row_requires_user_id(sync):
    with pytest.raises(KeyError):
        sync._row(1, {"nickname": "缺少 QQ"})
