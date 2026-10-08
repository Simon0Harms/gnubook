from decimal import Decimal as D

import pytest

from gnubook.money import AmountError, fmt, parse_amount


@pytest.mark.parametrize("text,expected", [
    ("12,50", D("12.50")), ("1.234,56", D("1234.56")), ("1234,5", D("1234.50")), ("12.50", D("12.50")),
    ("1.500", D("1500.00")), ("0.125", D("0.13")), ("-3,99", D("-3.99")), ("10+2,40", D("12.40")),
    ("3*4,99", D("14.97")), ("(10+5)/3", D("5.00")), ("100 - 0,01", D("99.99")), ("−5", D("-5.00")),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["abc", "1,2,3", "12,,5", "5/0", "(3", "1.23.4,5"])
def test_parse_amount_errors(text):
    with pytest.raises(AmountError):
        parse_amount(text)


def test_empty_is_none():
    assert parse_amount("") is None and parse_amount("  ") is None and parse_amount(None) is None


def test_fmt():
    assert fmt(D("1234.5")) == "1.234,50"
    assert fmt(D("-0.001")) == "0,00"
    assert fmt(D("-1234567.891")) == "−1.234.567,89"
    assert fmt(D("5"), symbol="€") == "5,00 €"
    assert fmt(D("5"), sign=True) == "+5,00"
