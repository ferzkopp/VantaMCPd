"""Parse a bounded, Google-style search string into a safe SQLite FTS5 MATCH expression.

Callers never reach FTS5 syntax directly: every operator in the generated expression comes from this
parser, and user text only ever appears inside an escaped double-quoted string literal.
"""
import re
import unicodedata

MAX_CLAUSES = 32
MAX_PHRASE = 200

# FTS5 column names created in corpus.initialize, keyed by the field prefixes a caller may type.
FIELDS = {
    "title": "title",
    "abstract": "abstract",
    "author": "authors",
    "authors": "authors",
    "category": "categories",
    "categories": "categories",
}

RAW_CLAUSE = re.compile(r'[+-]?(?:[A-Za-z]+:)?"[^"]*"\*?|\S+')
MEANINGFUL = re.compile(r"[^\W_]", re.UNICODE)


def fold(text: str) -> str:
    """Apply the same normalization as the unicode61 remove_diacritics=2 tokenizer."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


class Clause:
    __slots__ = ("text", "field", "prefix", "negated")

    def __init__(self, text: str, field: str | None, prefix: bool, negated: bool) -> None:
        self.text = text
        self.field = field
        self.prefix = prefix
        self.negated = negated

    @property
    def single_word(self) -> bool:
        return " " not in self.text.strip()

    def render(self) -> str:
        literal = '"' + self.text.replace('"', '""') + '"'
        if self.prefix:
            literal += "*"
        return f"{{{self.field}}} : {literal}" if self.field else literal


class ParsedQuery:
    def __init__(self, required: list[list[Clause]], excluded: list[Clause]) -> None:
        self.required = required
        self.excluded = excluded

    def clauses(self) -> list[Clause]:
        return [clause for group in self.required for clause in group] + self.excluded

    def correctable(self) -> list[Clause]:
        """Required single words that a spelling retry may replace, in query order."""
        return [group[0] for group in self.required if len(group) == 1 and group[0].single_word and not group[0].prefix]

    def render(self) -> str:
        groups = []
        for group in self.required:
            rendered = " OR ".join(clause.render() for clause in group)
            groups.append(f"({rendered})" if len(group) > 1 else rendered)
        expression = " AND ".join(groups)
        if self.excluded:
            excluded = " OR ".join(clause.render() for clause in self.excluded)
            expression = f"({expression}) NOT ({excluded})"
        return expression


def build_clause(raw: str) -> Clause | None:
    negated = False
    if raw[:1] in "+-":
        negated = raw[0] == "-"
        raw = raw[1:]
    field = None
    separator = raw.find(":")
    if separator > 0 and raw[:separator].casefold() in FIELDS:
        field = FIELDS[raw[:separator].casefold()]
        raw = raw[separator + 1:]
    prefix = False
    if raw.startswith('"'):
        closing = raw.rfind('"')
        prefix = raw.endswith('*')
        text = raw[1:closing] if closing > 0 else raw[1:]
    else:
        prefix = raw.endswith("*")
        text = raw[:-1] if prefix else raw
    text = text[:MAX_PHRASE].strip()
    # A clause of pure punctuation tokenizes to nothing and would make FTS5 reject the whole expression.
    if not MEANINGFUL.search(text):
        return None
    return Clause(text, field, prefix, negated)


def parse(query: str) -> ParsedQuery:
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must be a non-empty string of at most 500 characters")

    required: list[list[Clause]] = []
    excluded: list[Clause] = []
    pending_or = False
    pending_not = False
    for raw in RAW_CLAUSE.findall(query):
        # Bare boolean words are accepted in any case: writing "or" and meaning a literal word is far
        # less likely than meaning the operator, and quoting the word still forces a literal match.
        keyword = raw.upper()
        if keyword == "OR" and required:
            pending_or = True
            continue
        if keyword == "AND":
            continue
        if keyword == "NOT":
            pending_not = True
            continue
        clause = build_clause(raw)
        if clause is None:
            continue
        if pending_not:
            clause.negated = True
            pending_not = False
        if clause.negated:
            excluded.append(clause)
            pending_or = False
            continue
        if pending_or:
            required[-1].append(clause)
        else:
            required.append([clause])
        pending_or = False

    total = len(required) + sum(len(group) - 1 for group in required) + len(excluded)
    if total > MAX_CLAUSES:
        raise ValueError(f"query must contain at most {MAX_CLAUSES} searchable terms")
    if not required:
        raise ValueError("query must contain at least one term to match; exclusions alone are not a search")
    return ParsedQuery(required, excluded)
