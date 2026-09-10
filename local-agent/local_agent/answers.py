"""Validate answers and resolve citation text from successful reads in this run."""

import json

from .identifiers import has_identifier_mismatch


class AnswerError(ValueError):
    def __init__(self, code: str, message: str, *, repairable: bool = False):
        super().__init__(message)
        self.code = code
        self.repairable = repairable


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnswerError("INVALID_ANSWER", "JSON fields must be unique.", repairable=True)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AnswerError("INVALID_ANSWER", "Nonstandard JSON constants are not permitted.", repairable=True)


def _check_identifiers(answer: str, evidence: dict[str, dict]) -> None:
    if has_identifier_mismatch(answer, (snapshot['content'] for snapshot in evidence.values())):
        raise AnswerError('IDENTIFIER_MISMATCH', 'Identifier separators differ from the current read evidence.',
                          repairable=True)


def validate_answer(text: str, snapshots: dict[str, dict], target_path: str | None,
                    read_attempted: bool, *, coverage: dict | None = None,
                    task_failure: bool = False) -> dict:
    if not isinstance(text, str):
        raise AnswerError("INVALID_ANSWER", "The final answer must be JSON text.")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except AnswerError:
        raise
    except (ValueError, RecursionError) as exc:
        raise AnswerError("INVALID_ANSWER", "The final answer must be one strict JSON object.",
                          repairable=True) from exc
    if not isinstance(value, dict) or set(value) != {"status", "answer", "citations"}:
        raise AnswerError("INVALID_ANSWER", "Expected exactly status, answer, and citations.")
    status, answer, citations = value["status"], value["answer"], value["citations"]
    if (not isinstance(status, str) or status not in {"answered", "not_found", "unable"}
            or not isinstance(answer, str) or not answer.strip() or not isinstance(citations, list)):
        raise AnswerError("INVALID_ANSWER", "The answer fields have invalid values or types.")

    directory_mode = target_path is None
    coverage = coverage or {}
    permitted = coverage.get('read_files', []) if directory_mode else [target_path]
    evidence = {path: snapshot for path, snapshot in snapshots.items()
                if isinstance(path, str) and path in permitted and isinstance(snapshot, dict)
                and snapshot.get('ok') is True and snapshot.get('path') == path
                and isinstance(snapshot.get('content'), str)}
    has_read = bool(evidence)
    complete = (coverage.get('complete') is True and not coverage.get('unlisted_directories')
                and not coverage.get('unread_files') and '.' in coverage.get('listed_directories', [])
                and all(path in evidence for path in coverage.get('discovered_files', [])))
    if status == "unable":
        if not task_failure and not (coverage.get('attempted') if directory_mode else read_attempted):
            raise AnswerError("MISSING_READ", "A file read must be attempted before returning unable.")
        if not task_failure and ((directory_mode and (complete or not coverage.get('had_error')))
                                 or (not directory_mode and has_read)):
            raise AnswerError("INVALID_ANSWER", "Unable requires an actual tool failure and incomplete evidence.")
        if citations:
            raise AnswerError("INVALID_CITATION", "Unable must not include citations.")
        _check_identifiers(answer, evidence)
        return value
    if directory_mode and status == 'not_found':
        if not complete:
            raise AnswerError('INCOMPLETE_SEARCH', 'All discovered directories and files must be checked before not_found.')
    elif not has_read:
        raise AnswerError("MISSING_READ", "A successful target read is required.")
    if status == "answered" and not citations:
        raise AnswerError("MISSING_CITATION", "An answered result requires at least one citation.")

    anchor_fields = {"path", "start_line", "end_line"}
    for citation in citations:
        if (not isinstance(citation, dict)
                or set(citation) not in (anchor_fields, anchor_fields | {"quote"})):
            raise AnswerError("INVALID_CITATION", "Citation fields do not match the required schema.")
        start, end = citation["start_line"], citation["end_line"]
        path = citation['path']
        if not isinstance(path, str) or path not in evidence:
            raise AnswerError('INVALID_CITATION', 'Citation requires a current successful read of that path.')
        lines = evidence[path]['content'].splitlines()
        if (type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= len(lines)):
            raise AnswerError("INVALID_CITATION", "Citation path or line range is invalid.")
        quote = "\n".join(lines[start - 1:end])
        if "quote" in citation and citation["quote"] != quote:
            raise AnswerError("INVALID_CITATION", "The citation must quote the complete referenced lines exactly.")
        # Resolve omitted text from this run's evidence; never repair an incorrect supplied quote.
        citation["quote"] = quote
    _check_identifiers(answer, evidence)
    return value
