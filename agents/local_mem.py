"""Local on-disk cache for vision pre-scan and extraction results."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

LOCAL_MEM_DIR = Path("local_mem")


def ensure_local_mem_dir() -> Path:
    LOCAL_MEM_DIR.mkdir(parents=True, exist_ok=True)
    return LOCAL_MEM_DIR


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s/-]", "", text)
    text = re.sub(r"[\s/]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown_assignment"


def detect_assignment_test(filename: str, exam_name: str | None = None) -> str:
    """Detect assignment/test type from filename or extracted exam name."""
    if exam_name:
        normalized = re.sub(r"\s+\d+$", "", exam_name).strip()
        return slugify(normalized)

    stem = Path(filename).stem
    has_jee = re.search(r"jee[\s_-]*main", stem, re.IGNORECASE)
    
    wtm_match = re.search(r"wtm[\s_-]*(\d+)", stem, re.IGNORECASE)
    if has_jee and wtm_match:
        return f"jee_main_wtm_{wtm_match.group(1)}"
    if wtm_match:
        return f"wtm_{wtm_match.group(1)}"

    wta_match = re.search(r"wta[\s_-]*(\d+)", stem, re.IGNORECASE)
    if has_jee and wta_match:
        return f"jee_main_wta_{wta_match.group(1)}"
    if wta_match:
        return f"wta_{wta_match.group(1)}"

    unit_match = re.search(r"(?:unit[\s_-]*test|ut)[\s_-]*(\d+)", stem, re.IGNORECASE)
    if unit_match:
        return f"unit_test_{unit_match.group(1)}"

    finals_match = re.search(r"(?:finals|final|term)[\s_-]*(\d*)", stem, re.IGNORECASE)
    if finals_match:
        suffix = finals_match.group(1)
        return f"finals_{suffix}" if suffix else "finals"

    return slugify(stem[:64])


def file_fingerprint(file_path: str) -> dict:
    stat = os.stat(file_path)
    return {
        "mtime": int(stat.st_mtime),
        "size": int(stat.st_size),
    }


def _cache_path(assignment_test: str) -> Path:
    return ensure_local_mem_dir() / f"{assignment_test}.json"


def _load_cache(assignment_test: str) -> dict:
    path = _cache_path(assignment_test)
    if not path.exists():
        return {
            "assignment_test": assignment_test,
            "updated_at": None,
            "files": {},
        }
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _save_cache(assignment_test: str, cache: dict) -> None:
    cache["assignment_test"] = assignment_test
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _cache_path(assignment_test)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2)


def _fingerprint_matches(entry: dict, file_path: str) -> bool:
    stored = entry.get("fingerprint", {})
    current = file_fingerprint(file_path)
    return stored.get("mtime") == current["mtime"] and stored.get("size") == current["size"]


def get_cached_prescan(filename: str, file_path: str, assignment_test: str) -> dict | None:
    cache = _load_cache(assignment_test)
    entry = cache.get("files", {}).get(filename)
    if not entry:
        return None
    if not _fingerprint_matches(entry, file_path):
        return None
    prescan = entry.get("prescan")
    return prescan.copy() if prescan else None


def save_cached_prescan(
    filename: str,
    file_path: str,
    assignment_test: str,
    extraction: dict,
) -> str:
    cache = _load_cache(assignment_test)
    files = cache.setdefault("files", {})
    files[filename] = {
        "fingerprint": file_fingerprint(file_path),
        "prescan": extraction,
        "by_student": files.get(filename, {}).get("by_student", {}),
    }
    _save_cache(assignment_test, cache)
    return assignment_test


def _student_key(student_name: str, student_id: str) -> str:
    return f"{student_name.strip().lower()}|{student_id.strip().lower()}"


def get_cached_analysis(
    filename: str,
    file_path: str,
    assignment_test: str,
    student_name: str,
    student_id: str,
) -> dict | None:
    cache = _load_cache(assignment_test)
    entry = cache.get("files", {}).get(filename)
    if not entry or not _fingerprint_matches(entry, file_path):
        return None

    student_cache = entry.get("by_student", {}).get(_student_key(student_name, student_id))
    if student_cache:
        return student_cache.copy()

    prescan = entry.get("prescan")
    if not prescan:
        return None

    cached_name = (prescan.get("student_name") or "").strip().lower()
    cached_id = (prescan.get("student_id") or "").strip().lower()
    if (
        cached_name
        and cached_id
        and cached_name == student_name.strip().lower()
        and cached_id == student_id.strip().lower()
    ):
        return prescan.copy()

    return None


def save_cached_analysis(
    filename: str,
    file_path: str,
    assignment_test: str,
    student_name: str,
    student_id: str,
    extraction: dict,
) -> str:
    cache = _load_cache(assignment_test)
    files = cache.setdefault("files", {})
    entry = files.setdefault(
        filename,
        {
            "fingerprint": file_fingerprint(file_path),
            "prescan": None,
            "by_student": {},
        },
    )
    entry["fingerprint"] = file_fingerprint(file_path)
    entry.setdefault("by_student", {})[_student_key(student_name, student_id)] = extraction
    if not entry.get("prescan"):
        entry["prescan"] = extraction
    _save_cache(assignment_test, cache)
    return assignment_test


def format_assignment_display_name(assignment_test: str) -> str:
    label = assignment_test.replace("_", " ")
    label = re.sub(r"\bwtm\b", "WTM", label, flags=re.IGNORECASE)
    label = re.sub(r"\bjee main\b", "JEE Main", label, flags=re.IGNORECASE)
    label = re.sub(r"\bunit test\b", "Unit Test", label, flags=re.IGNORECASE)
    return label


def has_history() -> bool:
    ensure_local_mem_dir()
    return any(LOCAL_MEM_DIR.glob("*.json"))


def list_assignment_history() -> list[dict]:
    ensure_local_mem_dir()
    history = []

    for cache_file in sorted(LOCAL_MEM_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        assignment_test = cache_file.stem
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        files = cache.get("files", {})
        students = []
        exam_names = []
        filenames = []

        for filename, entry in files.items():
            filenames.append(filename)
            prescan = entry.get("prescan") or {}
            if prescan.get("exam_name"):
                exam_names.append(prescan["exam_name"])
            if prescan.get("student_name") or prescan.get("student_id"):
                students.append(
                    {
                        "name": prescan.get("student_name", ""),
                        "id": prescan.get("student_id", ""),
                    }
                )
            for student_key in entry.get("by_student", {}):
                name, _, student_id = student_key.partition("|")
                students.append({"name": name, "id": student_id})

        unique_students = []
        seen = set()
        for student in students:
            token = (student.get("name", ""), student.get("id", ""))
            if token in seen or not any(token):
                continue
            seen.add(token)
            unique_students.append(student)

        history.append(
            {
                "id": assignment_test,
                "display_name": format_assignment_display_name(assignment_test),
                "updated_at": cache.get("updated_at"),
                "file_count": len(files),
                "files": sorted(filenames),
                "exam_names": sorted(set(exam_names)),
                "students": unique_students,
            }
        )

    return history
