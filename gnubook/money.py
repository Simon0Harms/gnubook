"""Amounts: exact GnuCash numerics, German formatting and parsing (with simple arithmetic)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ZERO = Decimal(0)


def gnc_decimal(num: int, denom: int) -> Decimal:
    """GnuCash numeric (num/denom) as Decimal. Exact for the usual power-of-ten denominators."""
    if denom == 0:
        return ZERO
    return Decimal(int(num)) / Decimal(int(denom))


def sum_numerics(pairs) -> Decimal:
    """Sum (denom, sum_of_nums) pairs exactly per denominator."""
    total = ZERO
    for denom, num in pairs:
        total += gnc_decimal(num, denom)
    return total


def fraction_digits(fraction: int) -> int:
    """Number of decimal places for a GnuCash commodity fraction (100 -> 2)."""
    digits = 0
    f = int(fraction or 1)
    while f > 1 and f % 10 == 0:
        f //= 10
        digits += 1
    return digits


def quantize(value: Decimal, fraction: int = 100) -> Decimal:
    exp = Decimal(1).scaleb(-fraction_digits(fraction))
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def fmt(value: Decimal | None, places: int = 2, symbol: str | None = None, sign: bool = False,
        lang: str | None = None) -> str:
    """Number format of the UI language: 1.234,56 (de) or 1,234.56 (en), optionally with currency symbol."""
    if lang is None:
        from .i18n import current_lang

        lang = current_lang()
    if value is None:
        return ""
    q = Decimal(1).scaleb(-places)
    v = Decimal(value).quantize(q, rounding=ROUND_HALF_UP)
    if v == 0:
        v = abs(v)  # no "-0,00"
    neg = v < 0
    s = f"{abs(v):,.{places}f}"  # 1,234.56
    if lang != "en":
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if neg:
        s = "−" + s if not sign else "−" + s
    elif sign and v > 0:
        s = "+" + s
    if symbol:
        s = f"{s} {symbol}"
    return s


CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF", "DEM": "DM"}


def symbol_for(mnemonic: str) -> str:
    return CURRENCY_SYMBOLS.get(mnemonic, mnemonic)


class AmountError(ValueError):
    pass


re_thousands_en = re.compile(r"\d{1,3}(,\d{3})+|\d*")
_NUM_RE = re.compile(r"\d[\d.,']*|[.,]\d+")


def parse_number(text: str, lang: str = "de") -> Decimal:
    """Parse one number written the German way ("1.234,56", "1234,56") or plainly ("1234.56")."""
    s = text.strip().replace("'", "").replace(" ", "").replace("\u00a0", "")
    if lang == "en":  # 1,234.56 -> German notation, then the same rules
        if "," in s and not re_thousands_en.fullmatch(s.split(".")[0]):
            raise AmountError(f"ungültiger Betrag: {text}")
        s = s.replace(",", "").replace(".", ",")
    if not s:
        raise AmountError("leerer Betrag")
    if "," in s:
        if s.count(",") > 1:
            raise AmountError(f"ungültiger Betrag: {text}")
        int_part, frac = s.split(",")
        if "." in int_part and not re.fullmatch(r"\d{1,3}(\.\d{3})+", int_part):
            raise AmountError(f"ungültiger Betrag: {text}")
        s = int_part.replace(".", "") + "." + frac
    elif s.count(".") == 1:
        int_part, frac = s.split(".")
        # "1.500" is German thousands notation, "12.50" a decimal point
        if len(frac) == 3 and int_part and int_part != "0":
            s = int_part + frac
    elif s.count(".") > 1:
        if not re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            raise AmountError(f"ungültiger Betrag: {text}")
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise AmountError(f"ungültiger Betrag: {text}") from exc


class _Parser:
    """Tiny recursive-descent evaluator for + - * / and parentheses (like GnuCash amount fields)."""

    def __init__(self, text: str, lang: str = "de"):
        self.tokens = []
        pos = 0
        text = text.replace("−", "-").replace("×", "*")
        if lang != "en":
            text = text.replace(":", "/")
        while pos < len(text):
            ch = text[pos]
            if ch.isspace():
                pos += 1
                continue
            if ch in "+-*/()":
                self.tokens.append(ch)
                pos += 1
                continue
            m = _NUM_RE.match(text, pos)
            if not m:
                raise AmountError(f"unerwartetes Zeichen „{ch}“")
            self.tokens.append(parse_number(m.group(), lang))
            pos = m.end()
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expr(self):
        value = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            rhs = self.term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term(self):
        value = self.factor()
        while self.peek() in ("*", "/"):
            op = self.take()
            rhs = self.factor()
            if op == "*":
                value = value * rhs
            else:
                if rhs == 0:
                    raise AmountError("Division durch 0")
                value = value / rhs
        return value

    def factor(self):
        tok = self.take()
        if tok == "-":
            return -self.factor()
        if tok == "+":
            return self.factor()
        if tok == "(":
            value = self.expr()
            if self.take() != ")":
                raise AmountError("fehlende Klammer")
            return value
        if isinstance(tok, Decimal):
            return tok
        raise AmountError("unvollständiger Ausdruck")


def parse_amount(text: str | None, fraction: int = 100, lang: str | None = None) -> Decimal | None:
    """Parse user input such as "1.234,56", "12,5", "10+2,40" or "3*4,99". Empty -> None."""
    if text is None or not str(text).strip():
        return None
    if lang is None:
        from .i18n import current_lang

        lang = current_lang()
    p = _Parser(str(text), lang)
    if not p.tokens:
        return None
    value = p.expr()
    if p.peek() is not None:
        raise AmountError(f"ungültiger Ausdruck: {text}")
    return quantize(value, fraction)


def to_api_string(value: Decimal, places: int = 2) -> str:
    """Firefly-style plain decimal string ("1234.56")."""
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
