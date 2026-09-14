#!/usr/bin/env python3
import difflib
import html
import ipaddress
import math
import multiprocessing
import re
import textwrap
import unicodedata
import uuid as uuid_module
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
    optional_module,
    optional_string,
    output_text,
    require_text,
    string_list,
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


def line_slice(arguments: dict[str, Any]) -> dict[str, Any]:
    lines = require_text(arguments).splitlines()
    total = len(lines)
    tail = arguments.get("tail")
    if tail is not None:
        if "start" in arguments or "end" in arguments:
            raise ValueError("tail cannot be combined with start or end")
        count = bounded_int(arguments, "tail", 10, 1, MAX_RESULTS)
        start = max(1, total - count + 1)
        end = total
    else:
        start = bounded_int(arguments, "start", 1, 1, 1_000_000)
        end = bounded_int(arguments, "end", total, 1, 1_000_000) if "end" in arguments else total
        if end < start:
            raise ValueError("end must be greater than or equal to start")
    selected = lines[start - 1:end]
    if len(selected) > MAX_RESULTS:
        raise ValueError(f"line range exceeds {MAX_RESULTS} lines")
    if optional_bool(arguments, "numbered", False):
        width = len(str(start + len(selected) - 1)) if selected else 1
        rendered = "\n".join(f"{start + offset:>{width}} | {line}" for offset, line in enumerate(selected))
    else:
        rendered = "\n".join(selected)
    return {"text": rendered, "start": start, "end": start + len(selected) - 1 if selected else start, "lines": len(selected), "inputLines": total}


def replace_literal(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    search = optional_string(arguments, "search", "", 16_384)
    if not search:
        raise ValueError("search must be non-empty")
    replacement = optional_string(arguments, "replacement", "", 16_384)
    count = bounded_int(arguments, "count", 0, 0, MAX_RESULTS)
    if optional_bool(arguments, "ignoreCase", False):
        # re.escape keeps this a literal search; a lambda keeps backslashes in the replacement literal.
        value, replaced = re.subn(re.escape(search), lambda _match: replacement, text, count=count)
    else:
        occurrences = text.count(search)
        replaced = min(occurrences, count) if count else occurrences
        value = text.replace(search, replacement, count if count else -1)
    return {"text": value, "replacements": replaced}


def truncate(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxCharacters", 280, 1, 100_000)
    suffix = optional_string(arguments, "suffix", "\u2026", 32)
    if len(text) <= limit:
        return {"text": text, "truncated": False, "characters": len(text)}
    keep = max(0, limit - len(suffix))
    clipped = text[:keep]
    if choice(arguments, "boundary", {"character", "word"}, "word") == "word":
        match = re.search(r"\s\S*$", clipped)
        if match and match.start() > 0:
            clipped = clipped[:match.start()]
    value = clipped.rstrip() + suffix
    return {"text": value, "truncated": True, "characters": len(value)}


def pad_align(arguments: dict[str, Any]) -> dict[str, Any]:
    width = bounded_int(arguments, "width", 20, 1, 1_000)
    align = choice(arguments, "align", {"left", "right", "center"}, "left")
    fill = optional_string(arguments, "fill", " ", 1)
    if len(fill) != 1:
        raise ValueError("fill must be exactly one character")
    method = {"left": str.ljust, "right": str.rjust, "center": str.center}[align]
    lines = [method(line, width, fill) for line in require_text(arguments).splitlines()]
    return {"text": "\n".join(lines), "lines": len(lines), "width": width}


# NFKD leaves these intact, so they need an explicit mapping to stay useful for ASCII folding.
ASCII_FOLD_EXTRAS = str.maketrans({
    "\u00df": "ss", "\u00e6": "ae", "\u00c6": "AE", "\u0153": "oe", "\u0152": "OE",
    "\u00f8": "o", "\u00d8": "O", "\u0111": "d", "\u0110": "D", "\u0142": "l", "\u0141": "L",
    "\u00fe": "th", "\u00de": "TH", "\u00f0": "d", "\u00d0": "D", "\u0131": "i",
})


def ascii_fold(arguments: dict[str, Any]) -> dict[str, Any]:
    decomposed = unicodedata.normalize("NFKD", require_text(arguments).translate(ASCII_FOLD_EXTRAS))
    folded = "".join(character for character in decomposed if not unicodedata.combining(character))
    if optional_bool(arguments, "asciiOnly", False):
        folded = folded.encode("ascii", "ignore").decode("ascii")
    return output_text(unicodedata.normalize("NFC", folded))


def number_format(arguments: dict[str, Any]) -> dict[str, Any]:
    value = arguments.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value or value in (float("inf"), float("-inf")):
        raise ValueError("value must be a finite number")
    decimals = bounded_int(arguments, "decimals", 2 if isinstance(value, float) else 0, 0, 10)
    grouped = f"{value:,.{decimals}f}"
    separator = optional_string(arguments, "groupSeparator", ",", 4)
    decimal_separator = optional_string(arguments, "decimalSeparator", ".", 4)
    if separator != "," or decimal_separator != ".":
        grouped = grouped.replace(",", "\x00").replace(".", decimal_separator).replace("\x00", separator)
    return {"text": grouped, "value": value}


BYTE_UNITS = {"binary": (1024, ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]), "decimal": (1000, ["B", "kB", "MB", "GB", "TB", "PB"])}


def bytes_humanize(arguments: dict[str, Any]) -> dict[str, Any]:
    value = arguments.get("bytes")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("bytes must be a non-negative integer")
    base, units = BYTE_UNITS[choice(arguments, "standard", set(BYTE_UNITS), "binary")]
    decimals = bounded_int(arguments, "decimals", 1, 0, 3)
    size = float(value)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < base or candidate == units[-1]:
            break
        size /= base
    rendered = f"{size:.{decimals}f}".rstrip("0").rstrip(".") if unit != units[0] else str(value)
    return {"text": f"{rendered} {unit}", "value": value, "unit": unit}


def pluralize(arguments: dict[str, Any]) -> dict[str, Any]:
    inflect = optional_module("inflect", "python3-inflect", "pluralization")
    word = require_text(arguments)
    if len(word) > 200:
        raise ValueError("text exceeds 200 characters")
    engine = inflect.engine()
    action = choice(arguments, "action", {"plural", "singular"}, "plural")
    if action == "plural":
        count = arguments.get("count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
            raise ValueError("count must be an integer")
        value = engine.plural(word, count) if count is not None else engine.plural(word)
    else:
        singular = engine.singular_noun(word)
        value = word if singular is False else singular
    return {"text": value, "action": action}



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
    elif operation == "uuids":
        values = []
        for candidate in re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text):
            try:
                parsed = uuid_module.UUID(candidate)
            except ValueError:
                continue
            values.append({"value": str(parsed), "version": parsed.version})
    elif operation == "hashes":
        widths = {32: "md5", 40: "sha1", 64: "sha256", 96: "sha384", 128: "sha512"}
        values = [
            {"value": candidate, "bits": len(candidate) * 4, "likelyAlgorithm": widths[len(candidate)]}
            for candidate in re.findall(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,128}(?![0-9A-Za-z])", text)
            if len(candidate) in widths
        ]
    elif operation == "semvers":
        # An optional "v" prefix is conventional in tags and is stripped from the result.
        values = re.findall(r"(?<![\w.-])[vV]?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)(?![\w.-])", text)
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


ZERO_WIDTH = {"\u200b": "ZERO WIDTH SPACE", "\u200c": "ZERO WIDTH NON-JOINER", "\u200d": "ZERO WIDTH JOINER", "\u2060": "WORD JOINER", "\u00ad": "SOFT HYPHEN", "\ufeff": "ZERO WIDTH NO-BREAK SPACE"}
BIDI_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def text_inspect(arguments: dict[str, Any]) -> dict[str, Any]:
    """Surface the characters and whitespace an agent cannot see in a rendered chat message."""
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", 20, 1, MAX_RESULTS)
    crlf = text.count("\r\n")
    bare_cr = len(re.findall(r"\r(?!\n)", text))
    bare_lf = len(re.findall(r"(?<!\r)\n", text))
    styles = [name for name, present in (("crlf", crlf), ("lf", bare_lf), ("cr", bare_cr)) if present]
    lines = text.splitlines()

    def positions(predicate: Callable[[str], bool]) -> list[dict[str, Any]]:
        found = []
        for index, character in enumerate(text):
            if predicate(character):
                found.append({
                    "offset": index,
                    "character": f"U+{ord(character):04X}",
                    "name": ZERO_WIDTH.get(character) or unicodedata.name(character, "UNNAMED"),
                })
                if len(found) >= limit:
                    break
        return found

    indents = Counter("tab" if line.startswith("\t") else "space" for line in lines if line[:1] in (" ", "\t"))
    normalized = unicodedata.normalize("NFC", text)
    return {
        "lineEndings": styles[0] if len(styles) == 1 else ("mixed" if styles else "none"),
        "lineEndingCounts": {"crlf": crlf, "lf": bare_lf, "cr": bare_cr},
        "byteOrderMark": text.startswith("\ufeff"),
        "lines": len(lines),
        "trailingWhitespaceLines": [index + 1 for index, line in enumerate(lines) if line != line.rstrip()][:limit],
        "indentation": {"tabs": indents.get("tab", 0), "spaces": indents.get("space", 0), "mixed": len(indents) > 1},
        "nonAsciiCharacters": sum(1 for character in text if ord(character) > 127),
        "controlCharacters": positions(lambda character: unicodedata.category(character) == "Cc" and character not in "\t\n\r"),
        "zeroWidthCharacters": positions(lambda character: character in ZERO_WIDTH),
        "bidiControlCharacters": positions(lambda character: character in BIDI_CONTROLS),
        "replacementCharacters": text.count("\ufffd"),
        "normalizedForm": "NFC" if text == normalized else "not NFC",
    }


def duplicate_lines(arguments: dict[str, Any]) -> dict[str, Any]:
    text = require_text(arguments)
    limit = bounded_int(arguments, "maxResults", 100, 1, MAX_RESULTS)
    case_sensitive = optional_bool(arguments, "caseSensitive", True)
    ignore_whitespace = optional_bool(arguments, "ignoreWhitespace", False)
    occurrences: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        key = line.strip() if ignore_whitespace else line
        if not case_sensitive:
            key = key.casefold()
        entry = occurrences.setdefault(key, {"text": line, "count": 0, "lines": []})
        entry["count"] += 1
        if len(entry["lines"]) < 50:
            entry["lines"].append(number)
    repeated = sorted((entry for entry in occurrences.values() if entry["count"] > 1), key=lambda entry: (-entry["count"], entry["lines"][0]))
    return {
        "items": repeated[:limit],
        "count": min(len(repeated), limit),
        "duplicateLines": sum(entry["count"] - 1 for entry in repeated),
        "uniqueLines": len(occurrences),
        "truncated": len(repeated) > limit,
    }


CHUNK_SEPARATORS = {"paragraph": "\n\n", "line": "\n", "sentence": " "}


def chunk(arguments: dict[str, Any]) -> dict[str, Any]:
    """Split text on natural boundaries into size-bounded pieces, optionally overlapping."""
    text = require_text(arguments)
    size = bounded_int(arguments, "maxCharacters", 2_000, 100, 20_000)
    overlap = bounded_int(arguments, "overlapCharacters", 0, 0, 2_000)
    if overlap >= size:
        raise ValueError("overlapCharacters must be smaller than maxCharacters")
    boundary = choice(arguments, "boundary", {"paragraph", "sentence", "line", "character"}, "paragraph")
    limit = bounded_int(arguments, "maxResults", MAX_RESULTS, 1, MAX_RESULTS)

    if boundary == "character":
        units = [text[index:index + size] for index in range(0, len(text), size)] or [""]
        separator = ""
    else:
        separator = CHUNK_SEPARATORS[boundary]
        if boundary == "paragraph":
            units = re.split(r"\n\s*\n", text)
        elif boundary == "line":
            units = text.splitlines()
        else:
            units = split_sentences(text)
        units = [unit for unit in units if unit.strip()] or [text]
        # A single unit larger than the budget is split on characters rather than silently overflowing.
        units = [piece for unit in units for piece in ([unit] if len(unit) <= size else [unit[index:index + size] for index in range(0, len(unit), size)])]

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = unit if not current else current + separator + unit
        if current and len(candidate) > size:
            chunks.append(current)
            carry = current[-overlap:] if overlap else ""
            current = (carry + separator + unit) if carry else unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > limit:
        raise ValueError(f"chunking produced more than {limit} chunks; raise maxCharacters or maxResults")

    items = []
    cursor = 0
    for index, value in enumerate(chunks):
        start = text.find(value, max(0, cursor - overlap)) if value else cursor
        if start < 0:
            start = cursor
        cursor = start + len(value)
        items.append({"index": index, "text": value, "start": start, "characters": len(value), "estimatedTokens": math.ceil(len(value) / 4)})
    return {"items": items, "count": len(items), "boundary": boundary, "truncated": False}


def tfidf(arguments: dict[str, Any]) -> dict[str, Any]:
    """TF-IDF needs a corpus, so the corpus is the caller-supplied document array."""
    documents = string_list(arguments, "documents", 2, 50, 262_144)
    minimum = bounded_int(arguments, "minLength", 3, 1, 100)
    limit = bounded_int(arguments, "maxResults", 10, 1, 100)
    stopwords = {item.casefold() for item in (arguments.get("stopwords") or [])} if "stopwords" in arguments else set()
    if "stopwords" in arguments:
        string_list(arguments, "stopwords", 0, MAX_RESULTS, 100)

    tokenized = [[token.casefold() for token in word_tokens(document) if len(token) >= minimum and token.casefold() not in stopwords] for document in documents]
    total = len(tokenized)
    frequencies = [Counter(tokens) for tokens in tokenized]
    document_frequency = Counter(term for counts in frequencies for term in counts)
    results = []
    for index, counts in enumerate(frequencies):
        length = sum(counts.values()) or 1
        scored = [
            {"term": term, "score": round((count / length) * (math.log(total / (1 + document_frequency[term])) + 1), 6), "count": count, "documentFrequency": document_frequency[term]}
            for term, count in counts.items()
        ]
        scored.sort(key=lambda item: (-item["score"], item["term"]))
        results.append({"document": index, "terms": scored[:limit], "uniqueTerms": len(counts)})
    return {"items": results, "count": len(results), "documents": total, "note": "Smoothed IDF: ln(N / (1 + df)) + 1."}



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
    "line_slice": Operation("Return a numbered or plain range of lines.", text_properties(start=typed("integer", minimum=1, maximum=1_000_000), end=typed("integer", minimum=1, maximum=1_000_000), tail=typed("integer", minimum=1, maximum=MAX_RESULTS), numbered=typed("boolean")), ("text",), line_slice),
    "replace_literal": Operation("Replace literal text without regular-expression syntax.", text_properties(search=typed("string", minLength=1, maxLength=16_384), replacement=typed("string", maxLength=16_384), count=typed("integer", minimum=0, maximum=MAX_RESULTS), ignoreCase=typed("boolean")), ("text", "search"), replace_literal),
    "truncate": Operation("Clip text to a length on a word or character boundary.", text_properties(maxCharacters=typed("integer", minimum=1, maximum=100_000), boundary={"enum": ["character", "word"]}, suffix=typed("string", maxLength=32)), ("text",), truncate),
    "pad_align": Operation("Pad every line to a width, left, right, or centered.", text_properties(width=typed("integer", minimum=1, maximum=1_000), align={"enum": ["left", "right", "center"]}, fill=typed("string", minLength=1, maxLength=1)), ("text",), pad_align),
    "ascii_fold": Operation("Remove diacritics and fold common ligatures to ASCII equivalents.", text_properties(asciiOnly=typed("boolean")), ("text",), ascii_fold),
    "number_format": Operation("Format a number with grouping and fixed decimals.", {"value": typed("number"), "decimals": typed("integer", minimum=0, maximum=10), "groupSeparator": typed("string", maxLength=4), "decimalSeparator": typed("string", maxLength=4)}, ("value",), number_format),
    "bytes_humanize": Operation("Render a byte count in binary or decimal units.", {"bytes": typed("integer", minimum=0), "standard": {"enum": ["binary", "decimal"]}, "decimals": typed("integer", minimum=0, maximum=3)}, ("bytes",), bytes_humanize),
    "pluralize": Operation("Pluralize or singularize an English noun (needs python3-inflect).", text_properties(action={"enum": ["plural", "singular"]}, count=typed("integer")), ("text",), pluralize),
}

EXTRACT_OPERATIONS = {
    name: Operation(description, text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), extract_content)
    for name, description in {
        "emails": "Extract email-like addresses.", "urls": "Extract HTTP and HTTPS URLs.",
        "numbers": "Extract decimal and scientific-notation numbers.", "ip_addresses": "Extract validated IPv4 and IPv6 addresses.",
        "datetimes": "Extract ISO-like dates and times.", "code_blocks": "Extract fenced and inline Markdown code.",
        "quoted_text": "Extract straight or curly quoted text.",
        "uuids": "Extract and validate UUIDs with their version.",
        "hashes": "Extract hex digests and report their likely algorithm.",
        "semvers": "Extract strict Semantic Versioning 2.0 versions.",
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
    "text_inspect": Operation("Report line endings, indentation, and invisible or control characters.", text_properties(maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), text_inspect),
    "duplicate_lines": Operation("Report repeated lines with their counts and line numbers.", text_properties(caseSensitive=typed("boolean"), ignoreWhitespace=typed("boolean"), maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), duplicate_lines),
    "chunk": Operation("Split text into size-bounded chunks on paragraph, sentence, line, or character boundaries.", text_properties(maxCharacters=typed("integer", minimum=100, maximum=20_000), overlapCharacters=typed("integer", minimum=0, maximum=2_000), boundary={"enum": ["paragraph", "sentence", "line", "character"]}, maxResults=typed("integer", minimum=1, maximum=MAX_RESULTS)), ("text",), chunk),
    "tfidf": Operation("Rank terms by TF-IDF across a caller-supplied document set.", {"documents": typed("array", minItems=2, maxItems=50, items=typed("string")), "stopwords": typed("array", maxItems=MAX_RESULTS, items=typed("string")), "minLength": typed("integer", minimum=1, maximum=100), "maxResults": typed("integer", minimum=1, maximum=100)}, ("documents",), tfidf),
}
