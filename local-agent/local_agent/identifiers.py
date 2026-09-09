"""Detect source-aligned dash substitutions, without rewriting or inferring facts."""

from collections.abc import Iterable
import re


_DASHES = '-‐‑‒–—―−﹣－'
_DASH_CLASS = '[' + re.escape(_DASHES) + ']'
_WORDS = re.compile(r'(?<!\w)\w+(?:' + _DASH_CLASS + r'\w+)+')
_FOLD_DASHES = str.maketrans({char: '-' for char in _DASHES})


def _bounded(pattern: str) -> re.Pattern:
    # Chinese prose may touch a name; do not match inside another code or dash chain.
    continuation = '[A-Za-z0-9_' + re.escape(_DASHES) + ']'
    return re.compile(r'(?<!' + continuation + ')' + pattern + r'(?!' + continuation + ')')


def has_identifier_mismatch(answer: str, sources: Iterable[str]) -> bool:
    spellings: dict[str, set[str]] = {}
    for source in sources:
        for match in _WORDS.finditer(source):
            token = match.group()
            if not (any(char.isalpha() for char in token) and any(char.isdigit() for char in token)):
                continue
            folded = token.translate(_FOLD_DASHES)
            # Also leave dates immediately following Chinese prose outside this guard.
            if re.search(r'(?<![A-Za-z0-9_-])\d{4}-\d{1,2}-\d{1,2}$', folded):
                continue
            spellings.setdefault(folded, set()).add(token)

    exact_spans = [match.span() for originals in spellings.values() for original in originals
                   for match in _bounded(re.escape(original)).finditer(answer)]
    for folded, originals in spellings.items():
        pattern = _DASH_CLASS.join(re.escape(part) for part in folded.split('-'))
        for match in _bounded(pattern).finditer(answer):
            if match.group() in originals:
                continue
            # A longer exact source spelling must not be rejected as a shorter variant.
            if any(start <= match.start() and match.end() <= end for start, end in exact_spans):
                continue
            return True
    return False
