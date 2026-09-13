#!/usr/bin/env python3
import difflib
import html
import ipaddress
import math
import multiprocessing
import re
import textwrap
import unicodedata
from collections import Counter
from typing import Any, Callable

from operation_common import (
    MAX_PATTERN,
    MAX_RESULTS,
    REGEX_TIMEOUT_SECONDS,
    Operation,
    bounded_int,
    choice,
    optional_bool,
    optional_string,
    output_text,
    require_text,
    text_properties,
    typed,
    word_tokens,
)


def naming_words(text: str) -> list[str]:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated)
    return [item.lower() for item in re.findall(r"[^\W_]+", separated, re.UNICODE)]


def case_convert(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    style = choice(arguments, "style", {"camel", "snake", "kebab", "pascal", "constant", "title", "lower", "upper"})
    words = naming_words(text)
    if style == "camel":
        value = "" if not words else words[0] + "".join(word.capitalize() for word in words[1:])
    elif style == "pascal":
        value = "".join(word.capitalize() for word in words)
    elif style == "snake":
        value = "_".join(words)
    elif style == "kebab":
        value = "-".join(words)
    elif style == "constant":
        value = "_".join(words).upper()
    elif style == "title":
        value = " ".join(word.capitalize() for word in words)
    elif style == "lower":
        value = text.lower()
    else:
        value = text.upper()
    return output_text(value)


def slugify(arguments: dict[str, Any]) -> dict[str, Any]:
    separator = optional_string(arguments, "separator", "-", 1)
    if len(separator) != 1 or separator.isspace():
        raise ValueError("separator must be one non-whitespace character")
    value = unicodedata.normalize("NFKD", require_text(arguments)).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", separator, value).strip(separator)
    return output_text(value.lower() if optional_bool(arguments, "lowercase", True) else value)


def identifier_normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    style = choice(arguments, "style", {"camel", "snake", "pascal", "constant"}, "snake")
    value = case_convert({"text": require_text(arguments), "style": style})["text"]
    prefix = optional_string(arguments, "prefix", "_", 32)
    if not value:
        return output_text(prefix)
    return output_text(prefix + value if value[0].isdigit() else value)


def whitespace_normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    mode = choice(arguments, "mode", {"trim", "collapse", "dedent", "indent"})
    if mode == "trim":
        value = "\n".join(line.strip() for line in text.strip().splitlines())
    elif mode == "collapse":
        value = " ".join(text.split())
    elif mode == "dedent":
        value = textwrap.dedent(text)
    else:
        value = textwrap.indent(text, optional_string(arguments, "indent", "  ", 32))
    return output_text(value)


def unicode_normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    form = choice(arguments, "form", {"NFC", "NFD", "NFKC", "NFKD"})
    return output_text(unicodedata.normalize(form, require_text(arguments)))


def punctuation_normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    replacements = str.maketrans({
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u2026": "...", "\u00a0": " ", "\u2007": " ", "\u202f": " ",
    })
    return output_text(require_text(arguments).translate(replacements))


def deduplicate(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    unit = choice(arguments, "unit", {"lines", "paragraphs"}, "lines")
    case_sensitive = optional_bool(arguments, "caseSensitive", True)
    items = text.splitlines() if unit == "lines" else re.split(r"\n\s*\n", text)
    separator = "\n" if unit == "lines" else "\n\n"
    seen: set[str] = set()
    kept = []
    for item in items:
        key = item if case_sensitive else item.casefold()
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return {"text": separator.join(kept), "removed": len(items) - len(kept)}


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def line_sort(arguments: dict[str, Any]) -> dict[str, Any]:
    lines = require_text(arguments).splitlines()
    mode = choice(arguments, "mode", {"lexical", "natural", "numeric"}, "lexical")
    descending = optional_bool(arguments, "descending", False)
    if optional_bool(arguments, "unique", False):
        lines = list(dict.fromkeys(lines))
    try:
        key: Callable[[str], Any]
        if mode == "numeric":
            key = lambda value: float(value.strip())
        elif mode == "natural":
            key = natural_key
        else:
            key = str.casefold
        lines.sort(key=key, reverse=descending)
    except ValueError as error:
        raise ValueError("numeric line sorting requires every line to be a number") from error
    return {"text": "\n".join(lines), "lines": len(lines)}


def regex_line_filter_worker(connection: Any, lines: list[str], pattern: str, flags: int) -> None:
    try:
        compiled = re.compile(pattern, flags)
        connection.send((True, [compiled.search(line) is not None for line in lines]))
    except Exception as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def line_filter(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    pattern = optional_string(arguments, "pattern", "", MAX_PATTERN)
    if not pattern:
        raise ValueError("pattern must be non-empty")
    action = choice(arguments, "action", {"include", "exclude"}, "include")
    ignore_case = optional_bool(arguments, "ignoreCase", False)
    lines = text.splitlines()
    if optional_bool(arguments, "regex", False):
        parent, child = multiprocessing.Pipe(False)
        process = multiprocessing.Process(target=regex_line_filter_worker, args=(child, lines, pattern, re.IGNORECASE if ignore_case else 0))
        process.start()
        child.close()
        try:
            if not parent.poll(REGEX_TIMEOUT_SECONDS):
                process.kill()
                process.join()
                raise ValueError(f"regex line filtering exceeded {REGEX_TIMEOUT_SECONDS:g} seconds")
            response = parent.recv()
        finally:
            parent.close()
            if process.is_alive():
                process.kill()
            process.join()
        if not response[0]:
            raise ValueError(f"invalid regular expression: {response[1]}")
        matches = response[1]
    else:
        needle = pattern.casefold() if ignore_case else pattern
        matches = [needle in (line.casefold() if ignore_case else line) for line in lines]
    kept = [line for line, matched in zip(lines, matches) if matched == (action == "include")]
    return {"text": "\n".join(kept), "matchedLines": len(kept), "inputLines": len(lines)}


def wrap_text(arguments: dict[str, Any]) -> dict[str, Any]:
    value = textwrap.fill(
        require_text(arguments),
        width=bounded_int(arguments, "width", 80, 10, 1_000),
        initial_indent=optional_string(arguments, "initialIndent", "", 100),
        subsequent_indent=optional_string(arguments, "subsequentIndent", "", 100),
    )
    return output_text(value)


def split_join(arguments: dict[str, Any]) -> dict[str, Any]:
    input_delimiter = optional_string(arguments, "inputDelimiter", "\n", 100)
    output_delimiter = optional_string(arguments, "outputDelimiter", ",", 100)
    if not input_delimiter:
        raise ValueError("inputDelimiter must be non-empty")
    items = require_text(arguments).split(input_delimiter)
    if optional_bool(arguments, "trimItems", True):
        items = [item.strip() for item in items]
    if optional_bool(arguments, "omitEmpty", False):
        items = [item for item in items if item]
    if len(items) > MAX_RESULTS:
        raise ValueError(f"split result exceeds {MAX_RESULTS} items")
    return {"text": output_delimiter.join(items), "items": len(items)}


def template_fill(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    values = arguments.get("values")
    if not isinstance(values, dict) or len(values) > 100:
        raise ValueError("values must be an object with at most 100 entries")
    if not all(isinstance(key, str) and isinstance(value, (str, int, float, bool)) for key, value in values.items()):
        raise ValueError("template values must be scalar strings, numbers, or booleans")
    missing = choice(arguments, "missing", {"error", "keep", "empty"}, "error")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return str(values[name])
        if missing == "keep":
            return match.group(0)
        if missing == "empty":
            return ""
        raise ValueError(f"missing template value: {name}")

    return output_text(re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, text))


def bounded_items(values: list[Any], limit: int) -> dict[str, Any]:
    return {"items": values[:limit], "count": min(len(values), limit), "truncated": len(values) > limit}


def extract_content(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    operation = arguments["operation"]
    limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    if operation == "emails":
        values: list[Any] = re.findall(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])", text)
    elif operation == "urls":
        values = [value.rstrip(".,;:!?)]}") for value in re.findall(r"https?://[^\s<>\"']+", text)]
    elif operation == "numbers":
        values = re.findall(r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])", text)
    elif operation == "ip_addresses":
        values = []
        for candidate in re.findall(r"(?<![\w])(?:[0-9A-Fa-f:.]{2,})(?![\w])", text):
            try:
                values.append(str(ipaddress.ip_address(candidate.strip(".:"))))
            except ValueError:
                continue
    elif operation == "datetimes":
        values = re.findall(r"\b(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?|\d{2}:\d{2}(?::\d{2})?)\b", text)
    elif operation == "code_blocks":
        values = []
        occupied = []
        for match in re.finditer(r"```([^\n`]*)\n([\s\S]*?)```|~~~([^\n~]*)\n([\s\S]*?)~~~", text):
            values.append({"kind": "fenced", "language": (match.group(1) or match.group(3)).strip(), "text": match.group(2) or match.group(4), "start": match.start(), "end": match.end()})
            occupied.append((match.start(), match.end()))
        for match in re.finditer(r"(?<!`)`([^`\n]+)`(?!`)", text):
            if not any(start <= match.start() < end for start, end in occupied):
                values.append({"kind": "inline", "language": "", "text": match.group(1), "start": match.start(), "end": match.end()})
        values.sort(key=lambda item: item["start"])
    else:
        quote_pattern = r"\"([^\"\n]+)\"|'([^'\n]+)'|\u201c([^\u201d\n]+)\u201d|\u2018([^\u2019\n]+)\u2019"
        values = [next(group for group in match.groups() if group is not None) for match in re.finditer(quote_pattern, text)]
    return bounded_items(values, limit)


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text.strip()) if part.strip()]


def syllable_count(word: str) -> int:
    normalized = re.sub(r"[^a-z]", "", word.casefold())
    if not normalized:
        return 0
    groups = len(re.findall(r"[aeiouy]+", normalized))
    if normalized.endswith("e") and not normalized.endswith(("le", "ye")) and groups > 1:
        groups -= 1
    return max(groups, 1)


def levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[right_index] + 1, previous[right_index - 1] + (left_character != right_character)))
        previous = current
    return previous[-1]


def analyze_text(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    operation = arguments["operation"]
    tokens = word_tokens(text)
    sentence_values = split_sentences(text)
    if operation == "statistics":
        return {"characters": len(text), "utf8Bytes": len(text.encode()), "words": len(tokens), "lines": len(text.splitlines()), "sentences": len(sentence_values), "nonWhitespaceCharacters": len(re.sub(r"\s", "", text))}
    if operation == "readability":
        if not tokens or not sentence_values:
            raise ValueError("readability requires at least one word and one sentence")
        syllables = sum(syllable_count(word) for word in tokens)
        complex_words = sum(syllable_count(word) >= 3 for word in tokens)
        word_count = len(tokens)
        sentence_count = len(sentence_values)
        return {
            "fleschReadingEase": round(206.835 - 1.015 * word_count / sentence_count - 84.6 * syllables / word_count, 2),
            "fleschKincaidGrade": round(0.39 * word_count / sentence_count + 11.8 * syllables / word_count - 15.59, 2),
            "gunningFog": round(0.4 * (word_count / sentence_count + 100 * complex_words / word_count), 2),
            "smogGrade": round(1.043 * math.sqrt(complex_words * 30 / sentence_count) + 3.1291, 2) if complex_words else 0,
            "words": word_count, "sentences": sentence_count, "syllables": syllables,
            "note": "English-oriented heuristic scores.",
        }
    if operation == "keyword_frequency":
        minimum = bounded_int(arguments, "minLength", 3, 1, 100)
        limit = bounded_int(arguments, "maxResults", 25, 1, MAX_RESULTS)
        stopwords = arguments.get("stopwords", [])
        if not isinstance(stopwords, list) or len(stopwords) > MAX_RESULTS or not all(isinstance(item, str) for item in stopwords):
            raise ValueError(f"stopwords must be an array of at most {MAX_RESULTS} strings")
        ignored = {item.casefold() for item in stopwords}
        counts = Counter(token.casefold() for token in tokens if len(token) >= minimum and token.casefold() not in ignored)
        return {"items": [{"keyword": word, "count": count} for word, count in counts.most_common(limit)], "uniqueKeywords": len(counts), "truncated": len(counts) > limit}
    if operation == "similarity":
        other = require_text(arguments, "otherText")
        metric = choice(arguments, "metric", {"jaccard", "levenshtein", "sequence"}, "jaccard")
        if metric == "jaccard":
            left_set = {word.casefold() for word in tokens}
            right_set = {word.casefold() for word in word_tokens(other)}
            union = left_set | right_set
            return {"metric": metric, "score": round(len(left_set & right_set) / len(union), 6) if union else 1.0}
        if metric == "sequence":
            if len(text) + len(other) > 50_000:
                raise ValueError("sequence similarity inputs exceed 50000 characters combined")
            return {"metric": metric, "score": round(difflib.SequenceMatcher(None, text, other, autojunk=False).ratio(), 6)}
        if len(text) * len(other) > 2_000_000:
            raise ValueError("Levenshtein input product exceeds 2000000 characters")
        distance = levenshtein(text, other)
        denominator = max(len(text), len(other))
        return {"metric": metric, "distance": distance, "score": round(1 - distance / denominator, 6) if denominator else 1.0}
    if operation == "ngrams":
        size = bounded_int(arguments, "n", 2, 1, 5)
        limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
        return bounded_items([tokens[index:index + size] for index in range(max(0, len(tokens) - size + 1))], limit)
    if operation == "sentence_split":
        return bounded_items(sentence_values, bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS))
    return {"estimatedTokens": math.ceil(len(text) / 4), "charactersPerToken": 4, "words": len(tokens), "note": "Heuristic only; tokenizer-specific counts vary."}


TRANSFORM_OPERATIONS = {
    "case_convert": Operation("Convert text between common case conventions.", text_properties(style={"enum": ["camel", "snake", "kebab", "pascal", "constant", "title", "lower", "upper"]}), ("text", "style"), case_convert),
    "slugify": Operation("Create an ASCII URL slug.", text_properties(separator=typed("string", minLength=1, maxLength=1), lowercase=typed("boolean")), ("text",), slugify),
    "identifier_normalize": Operation("Create a safe programming identifier.", text_properties(style={"enum": ["camel", "snake", "pascal", "constant"]}, prefix=typed("string", maxLength=32)), ("text",), identifier_normalize),
    "whitespace_normalize": Operation("Trim, collapse, dedent, or indent whitespace.", text_properties(mode={"enum": ["trim", "collapse", "dedent", "indent"]}, indent=typed("string", maxLength=32)), ("text", "mode"), whitespace_normalize),
    "unicode_normalize": Operation("Apply a Unicode normalization form.", text_properties(form={"enum": ["NFC", "NFD", "NFKC", "NFKD"]}), ("text", "form"), unicode_normalize),
    "punctuation_normalize": Operation("Convert smart punctuation and special spaces to ASCII equivalents.", text_properties(), ("text",), punctuation_normalize),
    "deduplicate": Operation("Remove duplicate lines or paragraphs.", text_properties(unit={"enum": ["lines", "paragraphs"]}, caseSensitive=typed("boolean")), ("text",), deduplicate),
    "line_sort": Operation("Sort lines lexically, naturally, or numerically.", text_properties(mode={"enum": ["lexical", "natural", "numeric"]}, descending=typed("boolean"), unique=typed("boolean")), ("text",), line_sort),
    "line_filter": Operation("Include or exclude lines by literal text or regex.", text_properties(pattern=typed("string", minLength=1, maxLength=MAX_PATTERN), action={"enum": ["include", "exclude"]}, regex=typed("boolean"), ignoreCase=typed("boolean")), ("text", "pattern"), line_filter),
    "wrap": Operation("Wrap text to a bounded width.", text_properties(width=typed("integer", minimum=10, maximum=1_000), initialIndent=typed("string", maxLength=100), subsequentIndent=typed("string", maxLength=100)), ("text",), wrap_text),
    "split_join": Operation("Split text and join with another delimiter.", text_properties(inputDelimiter=typed("string", minLength=1, maxLength=100), outputDelimiter=typed("string", maxLength=100), trimItems=typed("boolean"), omitEmpty=typed("boolean")), ("text", "inputDelimiter", "outputDelimiter"), split_join),
    "template_fill": Operation("Substitute ${name} placeholders without evaluation.", text_properties(values=typed("object", maxProperties=100), missing={"enum": ["error", "keep", "empty"]}), ("text", "values"), template_fill),
}

EXTRACT_OPERATIONS = {
    name: Operation(description, text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), extract_content)
    for name, description in {
        "emails": "Extract email-like addresses.", "urls": "Extract HTTP and HTTPS URLs.",
        "numbers": "Extract decimal and scientific-notation numbers.", "ip_addresses": "Extract validated IPv4 and IPv6 addresses.",
        "datetimes": "Extract ISO-like dates and times.", "code_blocks": "Extract fenced and inline Markdown code.",
        "quoted_text": "Extract straight or curly quoted text.",
    }.items()
}

ANALYZE_OPERATIONS = {
    "statistics": Operation("Count characters, bytes, words, lines, and sentences.", text_properties(), ("text",), analyze_text),
    "readability": Operation("Calculate English-oriented readability heuristics.", text_properties(), ("text",), analyze_text),
    "keyword_frequency": Operation("Count frequent words with custom stopwords.", text_properties(stopwords=typed("array", maxItems=MAX_RESULTS, items=typed("string")), minLength=typed("integer", minimum=1, maximum=100), maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), analyze_text),
    "similarity": Operation("Compare two texts with Jaccard, Levenshtein, or sequence similarity.", text_properties(otherText={"type": "string"}, metric={"enum": ["jaccard", "levenshtein", "sequence"]}), ("text", "otherText"), analyze_text),
    "ngrams": Operation("Generate bounded word n-grams.", text_properties(n=typed("integer", minimum=1, maximum=5), maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), analyze_text),
    "sentence_split": Operation("Split text with a deterministic sentence heuristic.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), analyze_text),
    "token_estimate": Operation("Estimate tokens using a documented character heuristic.", text_properties(), ("text",), analyze_text),
}
