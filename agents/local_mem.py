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
    exam_slug = slugify(assignment_test)
    student_slug = slugify(student_name)
    student_json_path = LOCAL_MEM_DIR / assignment_test / f"{exam_slug}_{student_slug}_analyze.json"
    
    if not student_json_path.exists():
        return None
        
    try:
        student_data = json.loads(student_json_path.read_text(encoding="utf-8"))
    except Exception:
        return None
        
    entry = student_data.get("files", {}).get(filename)
    if not entry or not _fingerprint_matches(entry, file_path):
        return None
        
    return entry.get("extraction")


def save_cached_analysis(
    filename: str,
    file_path: str,
    assignment_test: str,
    student_name: str,
    student_id: str,
    extraction: dict,
) -> str:
    cache = _load_cache(assignment_test)
    
    # Enforce rule: only one student per test type
    active = cache.get("active_student")
    if active and (active.get("name") != student_name or active.get("id") != student_id):
        exam_dir = LOCAL_MEM_DIR / assignment_test
        if exam_dir.exists():
            for f in exam_dir.glob("*_analyze.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
                
    cache["active_student"] = {"name": student_name, "id": student_id}
    
    files = cache.setdefault("files", {})
    entry = files.setdefault(
        filename,
        {
            "fingerprint": file_fingerprint(file_path),
            "prescan": None,
        },
    )
    entry["fingerprint"] = file_fingerprint(file_path)
    if "by_student" in entry:
        del entry["by_student"]
        
    _save_cache(assignment_test, cache)
    
    # Save student marks exclusively to the new student json file
    exam_slug = slugify(assignment_test)
    student_slug = slugify(student_name)
    student_json_path = LOCAL_MEM_DIR / assignment_test / f"{exam_slug}_{student_slug}_analyze.json"
    
    student_data = {}
    if student_json_path.exists():
        try:
            student_data = json.loads(student_json_path.read_text(encoding="utf-8"))
        except Exception:
            student_data = {}
            
    files_dict = student_data.setdefault("files", {})
    files_dict[filename] = {
        "fingerprint": file_fingerprint(file_path),
        "extraction": extraction
    }
    
    student_json_path.parent.mkdir(parents=True, exist_ok=True)
    student_json_path.write_text(json.dumps(student_data, indent=4, ensure_ascii=False), encoding="utf-8")
    
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

        active_student = cache.get("active_student")
        if active_student:
            students.append(active_student)

        for filename, entry in files.items():
            filenames.append(filename)
            prescan = entry.get("prescan") or {}
            if prescan.get("exam_name"):
                exam_names.append(prescan["exam_name"])
            if not active_student:
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


def load_student_analysis_json(
    assignment_test: str,
    student_name: str,
    student_id: str,
) -> dict:
    exam_slug = slugify(assignment_test)
    student_slug = slugify(student_name)
    student_json_path = LOCAL_MEM_DIR / assignment_test / f"{exam_slug}_{student_slug}_analyze.json"
    
    if not student_json_path.exists():
        return {}
        
    try:
        return json.loads(student_json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
