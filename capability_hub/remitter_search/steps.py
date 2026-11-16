"""Pure name normalization, deterministic ranking and untrusted selection checks."""

from __future__ import annotations

import unicodedata

from domain.task_execution import ExecutionFailure

from .schemas import AgentSelection, SearchInput, SearchOutput, ValidationInput


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def normalize_search(values: dict) -> dict:
    args = SearchInput.model_validate(values)
    result = {"query": normalize_name(args.query), "limit": args.limit, "candidates": [
        {"id": candidate.id, "name": normalize_name(candidate.name),
         "aliases": sorted({name for alias in candidate.aliases if (name := normalize_name(alias))})}
        for candidate in args.candidates
    ]}
    # Normalization can remove punctuation-only names or expand Unicode text.
    # Reject such inputs consistently before either a tool or model invocation.
    return SearchInput.model_validate(result).model_dump(mode="json")


def _similarity(query: str, name: str) -> int:
    if not query or not name:
        return 0
    if query == name:
        return 1000
    query_tokens, name_tokens = set(query.split()), set(name.split())
    token_score = 900 * len(query_tokens & name_tokens) // len(query_tokens | name_tokens)
    query_grams = {query[index:index + 3] for index in range(max(1, len(query) - 2))}
    name_grams = {name[index:index + 3] for index in range(max(1, len(name) - 2))}
    gram_score = 1500 * len(query_grams & name_grams) // (len(query_grams) + len(name_grams))
    substring_score = (700 + 300 * len(query) // len(name)) if query in name else 0
    score = max(token_score, gram_score, substring_score)
    return score if score >= 400 else 0


def rank_candidates(values: dict) -> dict:
    search = normalize_search(values)
    matches = []
    if search["query"]:
        for candidate in search["candidates"]:
            score = max(_similarity(search["query"], name) for name in (candidate["name"], *candidate["aliases"]))
            if score:
                matches.append({"candidate_id": candidate["id"], "normalized_name": candidate["name"], "score": score})
    matches.sort(key=lambda match: (-match["score"], match["candidate_id"]))
    return {"query": search["query"], "candidate_count": len(search["candidates"]), "matches": matches[:search["limit"]]}


def validate_selection(values: dict) -> dict:
    args = ValidationInput.model_validate(values)
    search = normalize_search(args.search.model_dump())
    selection = AgentSelection.model_validate(args.selection.model_dump())
    ranked = rank_candidates(search)
    supplied = {candidate["id"] for candidate in search["candidates"]}
    allowed = {match["candidate_id"] for match in ranked["matches"]}
    selected = set(selection.selected_candidate_ids)
    if not selected <= supplied or not selected <= allowed or len(selected) > search["limit"]:
        raise ExecutionFailure("REMITTER_SELECTION_INVALID")
    matches = [match for match in ranked["matches"] if match["candidate_id"] in selected]
    result = {**ranked, "matches": matches,
              "selected_candidate_ids": [match["candidate_id"] for match in matches],
              "selection_method": "agent_assisted" if search["query"] and supplied else "empty_input"}
    return SearchOutput.model_validate(result).model_dump(mode="json")


async def normalize_step(_context, values: dict) -> dict:
    return normalize_search(values)


async def validate_selection_step(_context, values: dict) -> dict:
    return validate_selection(values)
