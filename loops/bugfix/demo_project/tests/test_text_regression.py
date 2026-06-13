"""Regression fence for text helpers. These PASS against the buggy code and
must STAY green after a fix — they pin the cases the buggy version already
gets right so a fix can't regress them."""

from buggy_lib.text import slugify


def test_simple_single_word():
    assert slugify("Hello") == "hello"


def test_single_space_between_two_words():
    assert slugify("foo bar") == "foo-bar"


def test_digits_are_kept():
    assert slugify("Route 66") == "route-66"


def test_already_a_slug_is_unchanged():
    assert slugify("already-a-slug") == "already-a-slug"
