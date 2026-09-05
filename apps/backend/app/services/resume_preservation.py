"""Deterministic preservation at the final AI-authored resume seam."""

from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Any

GROUNDING_REVIEW_CODE = "GROUNDING_REVIEW_REQUIRED"
_GROUNDING_REVIEW_THRESHOLD = 0.45

_ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "workExperience": ("company", "title"),
    "education": ("institution", "degree"),
    "personalProjects": ("name", "role"),
    "customItems": ("title", "subtitle"),
}
_PROTECTED_FIELDS: dict[str, tuple[str, ...]] = {
    "workExperience": ("id", "title", "company", "location", "years"),
    "education": ("id", "institution", "degree", "years"),
    "personalProjects": ("id", "name", "role", "years", "github", "website"),
    "customItems": ("id", "title", "subtitle", "location", "years"),
}
_TOKEN_RE = re.compile(r"[\w+#./-]+", re.UNICODE)
_NUMBER_RE = re.compile(
    r"(?<!\w)(?:[$€£])?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:thousand|million|billion|percent|times|[kmb%]|x))?(?!\w)",
    re.IGNORECASE,
)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "the",
        "to",
        "using",
        "with",
    }
)


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _identity(entry: dict[str, Any], section: str) -> tuple[str, ...]:
    entry_id = entry.get("id")
    if isinstance(entry_id, int) and entry_id != 0:
        return ("id", str(entry_id))
    return (
        "fields",
        *(_normalized(entry.get(field)) for field in _ENTRY_FIELDS[section]),
    )


def _field_identity(entry: dict[str, Any], section: str) -> tuple[str, ...] | None:
    identity = tuple(_normalized(entry.get(field)) for field in _ENTRY_FIELDS[section])
    return identity if any(identity) else None


def _matching_source_index(
    candidate: dict[str, Any],
    source_entries: list[dict[str, Any]],
    available: set[int],
    section: str,
) -> int | None:
    candidate_id = candidate.get("id")
    if isinstance(candidate_id, int) and candidate_id != 0:
        for index in sorted(available):
            if source_entries[index].get("id") == candidate_id:
                return index

    candidate_identity = _field_identity(candidate, section)
    if candidate_identity is None:
        return None
    matches = [
        index
        for index in sorted(available)
        if _field_identity(source_entries[index], section) == candidate_identity
    ]
    return matches[0] if len(matches) == 1 else None


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(text)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


def _similarity(left: str, right: str) -> float:
    left_normalized = _normalized(left)
    right_normalized = _normalized(right)
    if left_normalized == right_normalized:
        return 1.0
    left_tokens = _tokens(left_normalized)
    right_tokens = _tokens(right_normalized)
    overlap = len(left_tokens & right_tokens) / max(
        1, min(len(left_tokens), len(right_tokens))
    )
    sequence = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    return max(overlap, sequence)


def _novel_numbers(source: str, candidate: str) -> bool:
    def numeric_value(value: str) -> str:
        normalized = value.casefold().replace(",", "").replace(" ", "")
        normalized = normalized.translate(str.maketrans("", "", "$€£"))
        for word, suffix in (
            ("thousand", "k"),
            ("million", "m"),
            ("billion", "b"),
            ("percent", ""),
            ("times", ""),
        ):
            normalized = normalized.replace(word, suffix)
        return normalized.removesuffix("%").removesuffix("x")

    source_numbers = {numeric_value(value) for value in _NUMBER_RE.findall(source)}
    candidate_numbers = {
        numeric_value(value) for value in _NUMBER_RE.findall(candidate)
    }
    return bool(candidate_numbers - source_numbers)


def _best_source_row(
    candidate: str,
    source_rows: list[str],
    available: set[int],
) -> tuple[int, float] | None:
    if not available:
        return None
    scored = [
        (_similarity(source_rows[index], candidate), index) for index in available
    ]
    score, index = max(scored, key=lambda item: (item[0], -item[1]))
    return index, score


def _merge_description_rows(
    source_entry: dict[str, Any],
    candidate_entry: dict[str, Any],
    *,
    allow_review_claims: bool,
) -> tuple[list[str], list[str]]:
    source_rows = source_entry.get("description")
    candidate_rows = candidate_entry.get("description")
    if not isinstance(source_rows, list):
        return [], []
    source_text = " ".join(str(row) for row in source_rows)
    candidate_list = candidate_rows if isinstance(candidate_rows, list) else []
    source_styles = source_entry.get("descriptionStyles")
    styles = source_styles if isinstance(source_styles, list) else []
    available = set(range(len(source_rows)))
    merged_rows: list[str] = []
    merged_styles: list[str] = []

    for candidate_index, raw_candidate in enumerate(candidate_list[: len(source_rows)]):
        if not isinstance(raw_candidate, str):
            continue
        match = _best_source_row(raw_candidate, source_rows, available)
        if match is None:
            break
        source_index, score = match
        candidate_row = raw_candidate
        requires_restore = _novel_numbers(source_text, candidate_row) or (
            not allow_review_claims and score < _GROUNDING_REVIEW_THRESHOLD
        )
        if requires_restore and candidate_index in available:
            source_index = candidate_index
        available.remove(source_index)
        source_row = str(source_rows[source_index])
        if requires_restore:
            candidate_row = source_row
        merged_rows.append(candidate_row)
        merged_styles.append(
            styles[source_index]
            if source_index < len(styles)
            and styles[source_index] in {"bullet", "plain"}
            else "bullet"
        )

    for source_index in sorted(available):
        merged_rows.append(str(source_rows[source_index]))
        merged_styles.append(
            styles[source_index]
            if source_index < len(styles)
            and styles[source_index] in {"bullet", "plain"}
            else "bullet"
        )
    return merged_rows, merged_styles


def _description_contract_preserved(
    source_entry: dict[str, Any], candidate_entry: dict[str, Any]
) -> bool:
    source_rows = source_entry.get("description")
    candidate_rows = candidate_entry.get("description")
    if not isinstance(source_rows, list):
        return True
    if not isinstance(candidate_rows, list) or len(candidate_rows) != len(source_rows):
        return False
    if "descriptionStyles" not in source_entry:
        return True
    source_styles = source_entry.get("descriptionStyles")
    candidate_styles = candidate_entry.get("descriptionStyles")
    if not isinstance(source_styles, list) or not isinstance(candidate_styles, list):
        return False
    if len(candidate_styles) != len(candidate_rows):
        return False

    available = set(range(len(source_rows)))
    for candidate_index, candidate_row in enumerate(candidate_rows):
        if not isinstance(candidate_row, str):
            return False
        match = _best_source_row(candidate_row, source_rows, available)
        if match is None:
            return False
        source_index, _ = match
        available.remove(source_index)
        expected_style = (
            source_styles[source_index]
            if source_index < len(source_styles)
            and source_styles[source_index] in {"bullet", "plain"}
            else "bullet"
        )
        if candidate_styles[candidate_index] != expected_style:
            return False
    return True


def _merge_entries(
    source_entries: Any,
    candidate_entries: Any,
    section: str,
    *,
    allow_review_claims: bool,
) -> list[dict[str, Any]]:
    if not isinstance(source_entries, list):
        return []
    source_dicts = [entry for entry in source_entries if isinstance(entry, dict)]
    if not source_dicts:
        return []
    result: list[dict[str, Any]] = []
    available = set(range(len(source_dicts)))
    candidates = candidate_entries if isinstance(candidate_entries, list) else []
    for candidate_entry in candidates:
        if not isinstance(candidate_entry, dict):
            continue
        source_index = _matching_source_index(
            candidate_entry,
            source_dicts,
            available,
            section,
        )
        if source_index is None:
            continue
        available.remove(source_index)
        source_entry = source_dicts[source_index]
        merged = copy.deepcopy(candidate_entry)
        for field in _PROTECTED_FIELDS[section]:
            if field in source_entry:
                merged[field] = copy.deepcopy(source_entry[field])
            else:
                merged.pop(field, None)
        if section != "education":
            has_style_metadata = (
                "descriptionStyles" in source_entry or "descriptionStyles" in merged
            )
            rows, styles = _merge_description_rows(
                source_entry,
                merged,
                allow_review_claims=allow_review_claims,
            )
            merged["description"] = rows
            if has_style_metadata:
                merged["descriptionStyles"] = styles
            else:
                merged.pop("descriptionStyles", None)
        else:
            source_description = source_entry.get("description")
            candidate_description = merged.get("description")
            if not isinstance(candidate_description, str):
                merged["description"] = copy.deepcopy(source_description)
            elif isinstance(source_description, str) and _novel_numbers(
                source_description, candidate_description
            ):
                merged["description"] = source_description
            elif (
                isinstance(source_description, str)
                and not allow_review_claims
                and _similarity(source_description, candidate_description)
                < _GROUNDING_REVIEW_THRESHOLD
            ):
                merged["description"] = source_description
        result.append(merged)

    for source_index in sorted(available):
        result.append(copy.deepcopy(source_dicts[source_index]))
    return result


def _merge_additional(source: Any, candidate: Any) -> dict[str, list[str]]:
    source_dict = source if isinstance(source, dict) else {}
    candidate_dict = candidate if isinstance(candidate, dict) else {}
    result: dict[str, list[str]] = {}
    for field in ("technicalSkills", "languages", "certificationsTraining", "awards"):
        source_items = [
            item for item in source_dict.get(field, []) if isinstance(item, str)
        ]
        candidate_items = [
            item for item in candidate_dict.get(field, []) if isinstance(item, str)
        ]
        if field != "technicalSkills":
            source_keys = {_normalized(item) for item in source_items}
            candidate_items = [
                item for item in candidate_items if _normalized(item) in source_keys
            ]
        seen = {_normalized(item) for item in candidate_items}
        result[field] = list(candidate_items)
        for item in source_items:
            if _normalized(item) not in seen:
                result[field].append(item)
                seen.add(_normalized(item))
    return result


def _merge_custom_sections(
    source: Any,
    candidate: Any,
    *,
    allow_review_claims: bool,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    candidate_dict = candidate if isinstance(candidate, dict) else {}
    result: dict[str, Any] = {}
    for key, source_section in source.items():
        if not isinstance(source_section, dict):
            result[key] = copy.deepcopy(source_section)
            continue
        candidate_section = candidate_dict.get(key)
        if not isinstance(candidate_section, dict):
            result[key] = copy.deepcopy(source_section)
            continue
        if source_section.get("sectionType") != "itemList":
            result[key] = copy.deepcopy(source_section)
            continue
        merged_section = copy.deepcopy(candidate_section)
        merged_section["sectionType"] = source_section.get("sectionType")
        merged_section["items"] = _merge_entries(
            source_section.get("items"),
            candidate_section.get("items"),
            "customItems",
            allow_review_claims=allow_review_claims,
        )
        result[key] = merged_section
    return result


def finalize_ai_resume(
    source: dict[str, Any],
    candidate: dict[str, Any],
    *,
    allow_review_claims: bool = True,
) -> dict[str, Any]:
    """Return a non-mutating AI result that preserves the source contract.

    Weakly grounded rewrites remain when ``allow_review_claims`` is true so a
    preview can present them for explicit confirmation. Definite new metrics,
    extra rows, missing sections and identity drift are always repaired.
    """
    result = copy.deepcopy(candidate) if isinstance(candidate, dict) else {}
    result["personalInfo"] = copy.deepcopy(source.get("personalInfo", {}))
    source_summary = source.get("summary")
    candidate_summary = result.get("summary")
    if isinstance(source_summary, str):
        if not isinstance(candidate_summary, str) or not candidate_summary.strip():
            result["summary"] = source_summary
        elif _novel_numbers(source_summary, candidate_summary) or (
            not allow_review_claims
            and _similarity(source_summary, candidate_summary)
            < _GROUNDING_REVIEW_THRESHOLD
        ):
            result["summary"] = source_summary

    for section in ("workExperience", "education", "personalProjects"):
        result[section] = _merge_entries(
            source.get(section),
            result.get(section),
            section,
            allow_review_claims=allow_review_claims,
        )
    result["additional"] = _merge_additional(
        source.get("additional"), result.get("additional")
    )
    result["customSections"] = _merge_custom_sections(
        source.get("customSections"),
        result.get("customSections"),
        allow_review_claims=allow_review_claims,
    )
    if "sectionMeta" in source:
        result["sectionMeta"] = copy.deepcopy(source["sectionMeta"])
    return result


def _entry_map(
    entries: Any, section: str
) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    result: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                result[_identity(entry, section)].append(entry)
    return result


def validate_confirmed_resume(
    source: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    """Return stable source-contract violation codes for a confirm payload."""
    violations: list[str] = []
    if (
        isinstance(source.get("summary"), str)
        and source["summary"].strip()
        and not _normalized(candidate.get("summary"))
    ):
        violations.append("summary")

    for section in ("workExperience", "education", "personalProjects"):
        source_map = _entry_map(source.get(section), section)
        candidate_map = _entry_map(candidate.get(section), section)
        if Counter({key: len(value) for key, value in source_map.items()}) != Counter(
            {key: len(value) for key, value in candidate_map.items()}
        ):
            violations.append(f"{section}.entries")
            continue
        for key, source_entries in source_map.items():
            candidate_entries = candidate_map[key]
            for source_entry, candidate_entry in zip(source_entries, candidate_entries):
                if any(
                    field in source_entry
                    and source_entry.get(field) != candidate_entry.get(field)
                    for field in _PROTECTED_FIELDS[section]
                ):
                    violations.append(f"{section}.identity")
                    break
                if not _description_contract_preserved(source_entry, candidate_entry):
                    violations.append(f"{section}.descriptions")
                    break

    source_custom = source.get("customSections")
    candidate_custom = candidate.get("customSections")
    if isinstance(source_custom, dict):
        candidate_custom_dict = (
            candidate_custom if isinstance(candidate_custom, dict) else {}
        )
        if set(source_custom) - set(candidate_custom_dict):
            violations.append("customSections")
        for key, source_section in source_custom.items():
            candidate_section = candidate_custom_dict.get(key)
            if not isinstance(source_section, dict) or not isinstance(
                candidate_section, dict
            ):
                continue
            if source_section.get("sectionType") != candidate_section.get(
                "sectionType"
            ):
                violations.append(f"customSections.{key}.sectionType")
                continue
            if source_section.get("sectionType") != "itemList":
                source_value = source_section.get("strings", source_section.get("text"))
                candidate_value = candidate_section.get(
                    "strings", candidate_section.get("text")
                )
                if source_value != candidate_value:
                    violations.append(f"customSections.{key}.content")
                continue
            source_map = _entry_map(source_section.get("items"), "customItems")
            candidate_map = _entry_map(candidate_section.get("items"), "customItems")
            if Counter(
                {identity: len(entries) for identity, entries in source_map.items()}
            ) != Counter(
                {identity: len(entries) for identity, entries in candidate_map.items()}
            ):
                violations.append(f"customSections.{key}.entries")
                continue
            for identity, source_entries in source_map.items():
                for source_entry, candidate_entry in zip(
                    source_entries, candidate_map[identity]
                ):
                    if any(
                        field in source_entry
                        and source_entry.get(field) != candidate_entry.get(field)
                        for field in _PROTECTED_FIELDS["customItems"]
                    ):
                        violations.append(f"customSections.{key}.identity")
                        break
                    if not _description_contract_preserved(
                        source_entry, candidate_entry
                    ):
                        violations.append(f"customSections.{key}.descriptions")
                        break

    source_additional = source.get("additional")
    candidate_additional = candidate.get("additional")
    if isinstance(source_additional, dict):
        candidate_dict = (
            candidate_additional if isinstance(candidate_additional, dict) else {}
        )
        for field in (
            "technicalSkills",
            "languages",
            "certificationsTraining",
            "awards",
        ):
            source_items = Counter(
                _normalized(item)
                for item in source_additional.get(field, [])
                if isinstance(item, str)
            )
            candidate_items = Counter(
                _normalized(item)
                for item in candidate_dict.get(field, [])
                if isinstance(item, str)
            )
            if source_items - candidate_items:
                violations.append(f"additional.{field}")
    return list(dict.fromkeys(violations))


def _grounding_warning(path: str) -> str:
    return f"{GROUNDING_REVIEW_CODE}: Review {path} against the source resume."


def _entry_grounding_warnings(
    source_entries: Any,
    candidate_entries: Any,
    section: str,
    path_prefix: str,
) -> list[str]:
    warnings: list[str] = []
    source_map = _entry_map(source_entries, section)
    if not isinstance(candidate_entries, list):
        return warnings
    for candidate_index, candidate_entry in enumerate(candidate_entries):
        if not isinstance(candidate_entry, dict):
            continue
        bucket = source_map.get(_identity(candidate_entry, section))
        if not bucket:
            continue
        source_entry = bucket.pop(0)
        source_description = source_entry.get("description")
        candidate_description = candidate_entry.get("description")
        description_path = f"{path_prefix}[{candidate_index}].description"
        if isinstance(source_description, str) and isinstance(
            candidate_description, str
        ):
            if (
                _normalized(source_description) != _normalized(candidate_description)
                and _similarity(source_description, candidate_description)
                < _GROUNDING_REVIEW_THRESHOLD
            ):
                warnings.append(_grounding_warning(description_path))
            continue
        if not isinstance(source_description, list) or not isinstance(
            candidate_description, list
        ):
            continue
        for row_index, row in enumerate(candidate_description):
            if not isinstance(row, str):
                continue
            match = _best_source_row(
                row,
                source_description,
                set(range(len(source_description))),
            )
            if match is None:
                continue
            source_index, score = match
            if (
                _normalized(row) != _normalized(source_description[source_index])
                and score < _GROUNDING_REVIEW_THRESHOLD
            ):
                warnings.append(_grounding_warning(f"{description_path}[{row_index}]"))
    return warnings


def grounding_review_warnings(
    source: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    """Return stable warnings for narrative rewrites with weak source overlap."""
    warnings: list[str] = []
    source_summary = source.get("summary")
    candidate_summary = candidate.get("summary")
    if (
        isinstance(source_summary, str)
        and isinstance(candidate_summary, str)
        and _normalized(source_summary) != _normalized(candidate_summary)
        and _similarity(source_summary, candidate_summary) < _GROUNDING_REVIEW_THRESHOLD
    ):
        warnings.append(_grounding_warning("summary"))

    for section in ("workExperience", "personalProjects", "education"):
        warnings.extend(
            _entry_grounding_warnings(
                source.get(section),
                candidate.get(section),
                section,
                section,
            )
        )

    source_custom = source.get("customSections")
    candidate_custom = candidate.get("customSections")
    if isinstance(source_custom, dict) and isinstance(candidate_custom, dict):
        for key, source_section in source_custom.items():
            candidate_section = candidate_custom.get(key)
            if (
                not isinstance(source_section, dict)
                or not isinstance(candidate_section, dict)
                or source_section.get("sectionType") != "itemList"
            ):
                continue
            warnings.extend(
                _entry_grounding_warnings(
                    source_section.get("items"),
                    candidate_section.get("items"),
                    "customItems",
                    f"customSections.{key}.items",
                )
            )
    return warnings
