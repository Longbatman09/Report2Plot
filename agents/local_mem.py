"""Local on-disk cache for vision pre-scan and extraction results under 4-phase design."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from mcp_servers import vision_extractor as ve

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_MEM_DIR = Path("local_mem")
STUDENT_DIRECTORY_PATH = PROJECT_ROOT / "All_stud_details.json"
STUDENT_REPORT_OUTPUT_DIR = PROJECT_ROOT / "Output"


def ensure_local_mem_dir() -> Path:
    LOCAL_MEM_DIR.mkdir(parents=True, exist_ok=True)
    return LOCAL_MEM_DIR

def get_docling_document_path(filename: str) -> str:
    from agents import docling_converter as dc
    input_path = Path("input") / filename
    series_name, test_folder, _ = parse_report_filename(filename)
    output_dir = LOCAL_MEM_DIR / series_name / test_folder / "Docling_Out"
    output_path = output_dir / f"{input_path.stem}.md"
    
    if not input_path.exists():
        return str(output_path)
        
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or input_path.stat().st_mtime > output_path.stat().st_mtime:
        dc.convert_file(input_path, output_dir)
        
    return str(output_path)

def build_scan_payload(scan_results: list) -> dict:
    detected_student = {"name": "", "id": "", "class": "", "section": ""}
    detected_exam_name = "Unit Test"
    detected_data_mode = "single"

    for res in scan_results:
        if res.get("found_student"):
            if res.get("student_name"):
                detected_student["name"] = res.get("student_name")
            if res.get("student_id"):
                detected_student["id"] = res.get("student_id")
            if res.get("student_class"):
                detected_student["class"] = res.get("student_class")
            if res.get("student_section"):
                detected_student["section"] = res.get("student_section")
        if res.get("exam_name"):
            raw_exam = res.get("exam_name")
            detected_exam_name = re.sub(r"\s+\d+$", "", raw_exam).strip()
        if res.get("data_mode"):
            detected_data_mode = res.get("data_mode")

    # Determine students present in ALL files (intersection)
    student_lists_per_file = []
    for res in scan_results:
        students = res.get("all_students", [])
        student_lists_per_file.append(students)

    common_students = []
    if student_lists_per_file:
        first_file_students = student_lists_per_file[0]
        other_files_students = student_lists_per_file[1:]
        
        for student in first_file_students:
            name = student.get("student_name", "").strip()
            student_id = str(student.get("student_id", "")).strip()
            if not name:
                continue
                
            norm_name = name.lower()
            is_common = True
            for other_list in other_files_students:
                found_in_other = False
                for other_student in other_list:
                    other_name = other_student.get("student_name", "").strip().lower()
                    other_id = str(other_student.get("student_id", "")).strip().lower()
                    if other_name == norm_name or (student_id and other_id == student_id.lower()):
                        found_in_other = True
                        break
                if not found_in_other:
                    is_common = False
                    break
                    
            if is_common:
                # Avoid duplicate names in the common students list
                if not any(cs["name"].lower() == norm_name for cs in common_students):
                    common_students.append({
                        "name": name,
                        "id": student_id
                    })

    common = ve.detect_common_data_types(scan_results)
    return {
        "student": detected_student,
        "exam_name": detected_exam_name,
        "data_mode": detected_data_mode,
        "common_fields": common.get("common_fields", []),
        "common_students": sorted(common_students, key=lambda s: s["name"]),
    }



def load_student_directory() -> list[dict]:
    if not STUDENT_DIRECTORY_PATH.exists():
        return []
    try:
        data = json.loads(STUDENT_DIRECTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def search_student_directory(student_name_or_id: str) -> list[dict]:
    query = student_name_or_id.strip().lower()
    if not query:
        return []

    matches = []
    for student in load_student_directory():
        s_name = str(student.get("student_name", "")).strip().lower()
        s_id = str(student.get("student_id", "")).strip().lower()
        if query == s_name or query == s_id or query in s_name or query in s_id:
            matches.append(student)
    return matches


def resolve_student_record(student_name_or_id: str) -> dict | None:
    matches = search_student_directory(student_name_or_id)
    if not matches:
        return None

    query = student_name_or_id.strip().lower()
    for student in matches:
        s_name = str(student.get("student_name", "")).strip().lower()
        s_id = str(student.get("student_id", "")).strip().lower()
        if query == s_name or query == s_id:
            return student

    return matches[0]


def _standardize_total_mark_key(key: str) -> str:
    normalized = re.sub(r"[\s\-]+", "_", str(key).strip().lower())
    if normalized in {"total", "total_mark", "total_marks"}:
        return "total_mark"
    return normalized


def normalize_numeric_fields(fields: dict | list) -> dict:
    if isinstance(fields, list):
        iterable = fields
    elif isinstance(fields, dict):
        iterable = [{"name": k, "value": v} for k, v in fields.items()]
    else:
        iterable = []

    normalized: dict = {}
    for item in iterable:
        if isinstance(item, dict):
            key = item.get("name") or item.get("field") or item.get("key")
            value = item.get("value")
        else:
            try:
                key, value = item
            except Exception:
                continue
        if not key:
            continue
        normalized[_standardize_total_mark_key(str(key))] = value
    return normalized


def maintain_per_student_json(student_name: str, student_id: str, list_of_extractions_json: str) -> str:
    try:
        extractions = json.loads(list_of_extractions_json)
    except Exception as e:
        return f"Error parsing list_of_extractions_json: {e}"

    STUDENT_REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = STUDENT_REPORT_OUTPUT_DIR / f"{student_id}_details.json"

    existing_list = []
    
    # Read from existing details.json across Local_Mem and output folder
    search_paths = list(LOCAL_MEM_DIR.rglob(f"{student_id}_details.json"))
    legacy_path = STUDENT_REPORT_OUTPUT_DIR / f"{student_id}_details.json"
    if legacy_path.exists():
        search_paths.append(legacy_path)
        
    for p in search_paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing_list.extend(data)
        except Exception:
            pass

    # Build a lookup to avoid duplicates
    merged = {}
    for item in existing_list:
        prescan = item.get("prescan", {})
        key = (prescan.get("exam_name", ""), prescan.get("test_name", ""))
        merged[key] = item
        
    last_source_file = None

    for item in extractions if isinstance(extractions, list) else []:
        standardized_num_fields = normalize_numeric_fields(item.get("numerical_fields", {}))
        standardized_class_avgs = normalize_numeric_fields(item.get("class_averages", {}))

        exam_name = item.get("exam_name", "JEE MAIN WTM")
        test_name = item.get("test_name", "Unknown Test")
        source_file = item.get("source_file")
        if source_file:
            last_source_file = source_file

        # Check if it resembles previous data (Optional: could validate fields here, but plotting handles it)
        prescan = {
            "exam_name": exam_name,
            "test_name": test_name,
            "data_mode": item.get("data_mode", "grouped"),
            "found_student": item.get("found_student", True),
            "student_name": student_name,
            "student_id": student_id,
            "student_class": item.get("student_class", ""),
            "student_section": item.get("student_section", ""),
            "numerical_fields": standardized_num_fields,
            "class_averages": standardized_class_avgs,
        }

        if source_file:
            prescan["source_file"] = source_file
        if item.get("parse_warnings"):
            prescan["parse_warnings"] = item.get("parse_warnings")

        merged[(exam_name, test_name)] = {"prescan": prescan}

    final_list = list(merged.values())

    # Determine respective exam folder
    if last_source_file:
        series_name, test_folder, _ = parse_report_filename(last_source_file)
        target_dir = LOCAL_MEM_DIR / series_name / test_folder
    else:
        target_dir = LOCAL_MEM_DIR / "Other"
        
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / f"{student_id}_details.json"

    try:
        json_path.write_text(json.dumps(final_list, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(json_path)
    except Exception as e:
        return f"Error writing per-student JSON: {e}"


def render_final_output(student_id: str, provided_json_path: str = None) -> str:
    STUDENT_REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    if provided_json_path and Path(provided_json_path).exists():
        json_path = Path(provided_json_path)
    else:
        # Fallback to searching Local_Mem for the latest one
        matches = list(LOCAL_MEM_DIR.rglob(f"{student_id}_details.json"))
        if matches:
            # Get the most recently modified one
            json_path = max(matches, key=lambda p: p.stat().st_mtime)
        else:
            json_path = STUDENT_REPORT_OUTPUT_DIR / f"{student_id}_details.json"
            
    if not json_path.exists():
        return f"Error: Per-student JSON file for ID {student_id} not found."

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error reading per-student JSON: {e}"

    if not isinstance(data, list) or not data:
        return "Error: Invalid JSON data format."

    first_record = data[0].get("prescan", {})
    student_name = first_record.get("student_name", "Unknown Student")
    student_class = first_record.get("student_class", "")
    student_section = first_record.get("student_section", "")

    lines = []
    lines.append(f"# Student Performance Report: {student_name}")
    lines.append(f"**Student ID:** {student_id}")
    if student_class:
        lines.append(f"**Class:** {student_class}")
    if student_section:
        lines.append(f"**Section:** {student_section}")
    lines.append("")
    lines.append("## Test Performance History")
    lines.append("")

    for idx, item in enumerate(data):
        prescan = item.get("prescan", {})
        test_name = prescan.get("test_name", f"Test {idx + 1}")
        exam_name = prescan.get("exam_name", "Exam")
        found_student = prescan.get("found_student", False)
        num_fields = prescan.get("numerical_fields", {})
        class_avgs = prescan.get("class_averages", {})

        lines.append(f"### {exam_name} - {test_name}")
        lines.append(f"- **Found Student:** {found_student}")

        if num_fields:
            lines.append("- **Subject Marks:**")
            for sub, val in num_fields.items():
                lines.append(f"  - **{str(sub).replace('_', ' ').title()}:** {val}")

        total_mark = num_fields.get("total_mark")
        if total_mark is not None:
            lines.append(f"- **Total Mark:** {total_mark}")

        if class_avgs:
            lines.append("- **Class Averages Comparison:**")
            for sub, val in class_avgs.items():
                student_val = num_fields.get(sub, "N/A")
                diff_str = ""
                if student_val != "N/A":
                    try:
                        diff = float(student_val) - float(val)
                        diff_str = f" ({'+' if diff >= 0 else ''}{diff:.1f} vs average)"
                    except Exception:
                        diff_str = ""
                lines.append(f"  - **{str(sub).replace('_', ' ').title()}:** {student_val} vs {val}{diff_str}")
        lines.append("")

    report_content = "\n".join(lines)
    report_path = STUDENT_REPORT_OUTPUT_DIR / f"{student_id}_report.md"
    try:
        report_path.write_text(report_content, encoding="utf-8")
        return str(report_path)
    except Exception as e:
        return f"Error writing final markdown report: {e}"


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s/-]", "", text)
    text = re.sub(r"[\s/]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown_assignment"


def get_series_and_test_from_assignment(assignment_test: str) -> tuple[str, str, str]:
    """
    Given a series code or assignment_test string (like 'jee_main_wtm_30' or 'wtm_29'),
    derives the canonical Series Name, Test Folder, and Exam Code.
    """
    cleaned = assignment_test.lower().replace("_", " ")
    
    # Match test code and number
    match = re.search(r'\b(wtm|wta|ut|unit test|ut)[\s_-]*(\d+)', cleaned)
    if match:
        code_raw = match.group(1)
        num = match.group(2)
        if "wtm" in code_raw:
            code = "WTM"
        elif "wta" in code_raw:
            code = "WTA"
        elif "ut" in code_raw or "unit" in code_raw:
            code = "UT"
        else:
            code = code_raw.upper()
        test_folder = f"{code} {num}"
        exam_code = f"{code}{num}"
    else:
        test_folder = "Unknown Test"
        exam_code = "Unknown"
        code = "TEST"
        
    # Series Name
    if "jee" in cleaned and "main" in cleaned:
        series_name = f"Jee Mains {code}"
    elif "jee" in cleaned and ("advance" in cleaned or "adv" in cleaned):
        series_name = f"Jee Advance {code}"
    else:
        series_name = f"Jee Mains {code}"
        
    return series_name, test_folder, exam_code


def parse_report_filename(filename: str) -> tuple[str, str, str]:
    """
    Parses a report filename and returns a tuple of (series_name, test_folder_name, exam_code).
    Example:
    '06-06-2026_SR C 120 (INCOMING)_Jee-Main_WTM-29_INTERNAL ANALYSIS.pdf'
    -> ('Jee Mains WTM', 'WTM 29', 'WTM29')
    """
    slug = detect_assignment_test(filename)
    return get_series_and_test_from_assignment(slug)


def detect_assignment_test(filename: str, exam_name: str | None = None) -> str:
    """Detect assignment/test type from filename or extracted exam name."""
    if exam_name:
        # Standard cleaning for legacy tests
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


def _fingerprint_matches(stored_fp: dict, file_path: str) -> bool:
    current = file_fingerprint(file_path)
    return stored_fp.get("mtime") == current["mtime"] and stored_fp.get("size") == current["size"]


def run_phase_2_roster(series_name: str) -> dict:
    """Phase 2: Roster Extraction -> all_students_list.json"""
    series_dir = LOCAL_MEM_DIR / series_name
    roster_path = series_dir / "all_students_list.json"
    
    # 1. Find all test folders and their .md files
    md_files = []
    if series_dir.exists():
        for test_dir in series_dir.iterdir():
            if test_dir.is_dir():
                docling_out_dir = test_dir / "Docling_Out"
                if docling_out_dir.exists():
                    for md_file in docling_out_dir.glob("*.md"):
                        md_files.append(md_file)
                        
    if not md_files:
        try:
            from mcp_servers import file_watcher as fw
            inputs = fw.list_input_files()
            for name in inputs.get("files", []):
                f_series, test_folder, _ = parse_report_filename(name)
                if f_series == series_name:
                    md_file = LOCAL_MEM_DIR / series_name / test_folder / "Docling_Out" / f"{Path(name).stem}.md"
                    md_files.append(md_file)
        except Exception as e:
            print(f"Warning: Fallback roster listing failed: {e}")
                        
    # 2. Extract roster from all md files
    students_by_id = {}
    all_subject_fields = set()
    
    for md_file in md_files:
        if md_file.exists():
            mtime = int(md_file.stat().st_mtime)
            size = int(md_file.stat().st_size)
        else:
            mtime = 0
            size = 0
        fingerprint = {"mtime": mtime, "size": size}
        
        cache_path = md_file.with_name(f"{md_file.stem}_prescan.json")
        cached_result = None
        if cache_path.exists():
            try:
                cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
                stored_fp = cached_data.get("fingerprint", {})
                if stored_fp.get("mtime") == mtime and stored_fp.get("size") == size:
                    cached_result = cached_data.get("extraction")
            except Exception:
                pass
                
        if cached_result:
            result = cached_result
        else:
            result = ve.extract_report_data(str(md_file), student_name="the student", student_id="any ID")
            try:
                cache_data = {
                    "fingerprint": fingerprint,
                    "extraction": result
                }
                cache_path.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"Warning: Failed to save prescan cache: {e}")
                
        all_studs = result.get("all_students", [])
        if not all_studs and result.get("student_name") and result.get("student_id"):
            all_studs = [{
                "student_name": result["student_name"],
                "student_id": result["student_id"],
                "student_class": result.get("student_class", ""),
                "student_section": result.get("student_section", "")
            }]
            
        for stud in all_studs:
            s_name = stud.get("name") or stud.get("student_name") or ""
            s_id = str(stud.get("id") or stud.get("student_id") or "").strip()
            s_class = stud.get("class") or stud.get("student_class") or stud.get("class_name") or ""
            s_section = stud.get("section") or stud.get("student_section") or ""
            
            if s_id:
                existing = students_by_id.get(s_id)
                if existing:
                    if not existing["student_name"] and s_name:
                        existing["student_name"] = s_name
                    if not existing["student_class"] and s_class:
                        existing["student_class"] = s_class
                    if not existing["student_section"] and s_section:
                        existing["student_section"] = s_section
                else:
                    students_by_id[s_id] = {
                        "student_name": s_name,
                        "student_id": s_id,
                        "student_class": s_class,
                        "student_section": s_section
                    }
                    
        if result.get("numerical_fields"):
            all_subject_fields.update(result["numerical_fields"].keys())
            
    students_list = list(students_by_id.values())
    students_list.sort(key=lambda x: x["student_name"])
    
    roster_data = {
        "series": series_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_students": len(students_list),
        "students": students_list,
        "common_fields": sorted(list(all_subject_fields))
    }
    
    series_dir.mkdir(parents=True, exist_ok=True)
    roster_json_str = json.dumps(roster_data, indent=2, ensure_ascii=False)
    roster_path.write_text(roster_json_str, encoding="utf-8")
    
    # Save in each sub test folder under the series
    for item in series_dir.iterdir():
        if item.is_dir() and item.name != "Docling_Out":
            test_roster_path = item / "all_students_list.json"
            try:
                test_roster_path.write_text(roster_json_str, encoding="utf-8")
                print(f"[local_mem] Saved roster to {test_roster_path}")
            except Exception as e:
                print(f"Warning: Failed to save roster to test folder {item.name}: {e}")
                
    return roster_data


def save_phase_3_extraction(
    series_name: str,
    test_folder: str,
    exam_code: str,
    student_name: str,
    student_id: str,
    extraction: dict,
    error: str = None
) -> None:
    """Phase 3: Write <ExamCode>.json per test"""
    test_dir = LOCAL_MEM_DIR / series_name / test_folder
    test_dir.mkdir(parents=True, exist_ok=True)
    json_path = test_dir / f"{exam_code}.json"
    
    prescan_data = {
        "exam_name": extraction.get("exam_name") or series_name.upper(),
        "test_name": extraction.get("test_name") or test_folder,
        "data_mode": extraction.get("data_mode") or "grouped",
        "found_student": extraction.get("found_student", False),
        "student_name": student_name,
        "student_id": student_id,
        "student_class": extraction.get("student_class", ""),
        "student_section": extraction.get("student_section", ""),
        "numerical_fields": extraction.get("numerical_fields", {}),
        "class_averages": extraction.get("class_averages", {})
    }
    
    if error:
        prescan_data["error"] = error
    if extraction.get("parse_warnings"):
        prescan_data["parse_warnings"] = extraction["parse_warnings"]
        
    output_data = {
        "prescan": prescan_data
    }
    
    json_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_cached_analysis_phase_3(
    series_name: str,
    test_folder: str,
    exam_code: str,
    student_id: str
) -> dict | None:
    """Get cached Phase 3 results if student_id matches"""
    json_path = LOCAL_MEM_DIR / series_name / test_folder / f"{exam_code}.json"
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        prescan = data.get("prescan", {})
        if str(prescan.get("student_id")).strip() == str(student_id).strip():
            return prescan
    except Exception:
        pass
    return None


def run_phase_4_unified_data(series_name: str, student_id: str) -> dict:
    """Phase 4: Unified data structure assembled from <ExamCode>.json files"""
    series_dir = LOCAL_MEM_DIR / series_name
    tests = []
    student_name = ""
    
    if series_dir.exists():
        test_dirs = [d for d in series_dir.iterdir() if d.is_dir()]
        
        # Natural sorting
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s.name)]
        test_dirs.sort(key=natural_sort_key)
        
        for test_dir in test_dirs:
            test_folder = test_dir.name
            exam_code = test_folder.replace(" ", "")
            json_path = test_dir / f"{exam_code}.json"
            
            if json_path.exists():
                try:
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                    prescan = data.get("prescan", {})
                    found = prescan.get("found_student", False)
                    scores = prescan.get("numerical_fields", {}) if found else {}
                    class_avgs = prescan.get("class_averages", {}) if found else {}
                    
                    if found and not student_name:
                        student_name = prescan.get("student_name", "")
                        
                    tests.append({
                        "test_name": prescan.get("test_name", test_folder),
                        "exam_code": exam_code,
                        "found": found,
                        "scores": scores,
                        "class_averages": class_avgs
                    })
                except Exception as e:
                    print(f"Warning: Failed to load {json_path.name}: {e}")
            else:
                tests.append({
                    "test_name": test_folder,
                    "exam_code": exam_code,
                    "found": False,
                    "scores": {},
                    "class_averages": {}
                })
                
    return {
        "student_name": student_name,
        "student_id": student_id,
        "series": series_name,
        "tests": tests
    }


def map_unified_to_aggregated(unified: dict) -> dict:
    results = []
    for test in unified["tests"]:
        if test["found"]:
            results.append({
                "exam_name": unified["series"],
                "test_name": test["test_name"],
                "found_student": True,
                "student_name": unified["student_name"],
                "student_id": unified["student_id"],
                "numerical_fields": test["scores"],
                "class_averages": test["class_averages"]
            })
    return {
        "student": {
            "name": unified["student_name"],
            "id": unified["student_id"],
            "class": "",
            "section": ""
        },
        "results": results
    }


def format_assignment_display_name(series_name: str) -> str:
    label = series_name.replace("_", " ")
    label = re.sub(r"\bwtm\b", "WTM", label, flags=re.IGNORECASE)
    label = re.sub(r"\bjee main\b", "JEE Main", label, flags=re.IGNORECASE)
    label = re.sub(r"\bunit test\b", "Unit Test", label, flags=re.IGNORECASE)
    return label


def has_history() -> bool:
    ensure_local_mem_dir()
    for item in LOCAL_MEM_DIR.iterdir():
        if item.name not in (".", "..", ".gitkeep", ".keep"):
            return True
    return False


def list_assignment_history() -> list[dict]:
    ensure_local_mem_dir()
    history = []
    
    series_dirs = []
    for d in LOCAL_MEM_DIR.iterdir():
        if d.is_dir():
            # Check if it has any test folders with json or Source files
            has_data = False
            for td in d.iterdir():
                if td.is_dir() and (list(td.glob("*.json")) or list((td / "Source").glob("*"))):
                    has_data = True
                    break
            if has_data:
                series_dirs.append(d)
            
    legacy_files = []
    for f in LOCAL_MEM_DIR.glob("*.json"):
        if f.stem not in ("all_students", "all_students_list", "prescan"):
            legacy_files.append(f)

    student_reports = []
    if STUDENT_REPORT_OUTPUT_DIR.exists():
        for details_path in STUDENT_REPORT_OUTPUT_DIR.glob("*_details.json"):
            student_reports.append(details_path)
            
    items = []
    for sd in series_dirs:
        mtime = sd.stat().st_mtime
        if (sd / "all_students_list.json").exists():
            mtime = (sd / "all_students_list.json").stat().st_mtime
        items.append((sd, mtime, "new"))
    for lf in legacy_files:
        items.append((lf, lf.stat().st_mtime, "legacy"))
    for report in student_reports:
        items.append((report, report.stat().st_mtime, "student_report"))
        
    items.sort(key=lambda x: x[1], reverse=True)
    
    for item, mtime, style in items:
        if style == "new":
            series_name = item.name
            roster_path = item / "all_students_list.json"
            roster = {}
            try:
                roster = json.loads(roster_path.read_text(encoding="utf-8"))
            except Exception:
                pass
                
            students = []
            for s in roster.get("students", []):
                students.append({
                    "name": s.get("student_name", ""),
                    "id": s.get("student_id", "")
                })
                
            filenames = []
            for test_dir in item.iterdir():
                if test_dir.is_dir():
                    source_dir = test_dir / "Source"
                    if source_dir.exists():
                        for f in source_dir.glob("*"):
                            filenames.append(f.name)
                            
            active_student = None
            for test_dir in item.iterdir():
                if test_dir.is_dir():
                    for json_file in test_dir.glob("*.json"):
                        if json_file.stem != f"{json_file.parent.name.replace(' ', '')}_prescan":
                            try:
                                json_data = json.loads(json_file.read_text(encoding="utf-8"))
                                # Handle both prescan wrapper (legacy) and direct extraction
                                if "prescan" in json_data:
                                    ext = json_data["prescan"]
                                else:
                                    ext = json_data
                                if ext.get("found_student") and ext.get("student_name"):
                                    active_student = {
                                        "name": ext["student_name"],
                                        "id": ext["student_id"]
                                    }
                                    break
                            except Exception:
                                pass
                    if active_student:
                        break
                        
            if active_student:
                students = [active_student]
                
            history.append({
                "id": series_name,
                "display_name": format_assignment_display_name(series_name),
                "updated_at": roster.get("generated_at", datetime.fromtimestamp(mtime).isoformat()),
                "file_count": len(filenames),
                "files": sorted(filenames),
                "exam_names": [series_name],
                "students": students
            })
        else:
            if style == "student_report":
                details_path = item
                try:
                    data = json.loads(details_path.read_text(encoding="utf-8"))
                except Exception:
                    continue

                if not isinstance(data, list) or not data:
                    continue

                student_id = details_path.stem.replace("_details", "")
                first_record = data[0].get("prescan", {})
                student_name = first_record.get("student_name", "")
                exam_names = []
                source_files = []

                for entry in data:
                    prescan = entry.get("prescan", {})
                    if prescan.get("exam_name"):
                        exam_names.append(prescan["exam_name"])
                    if prescan.get("source_file"):
                        source_files.append(prescan["source_file"])

                
                report_md = details_path.with_name(f"{student_id}_report.md")
                history.append({
                    "id": student_id,
                    "display_name": student_name or student_id,
                    "updated_at": datetime.fromtimestamp(mtime).isoformat(),
                    "file_count": len(source_files) + 2,
                    "files": sorted(list(set(source_files + [details_path.name, f"{student_id}_report.md"]))),
                    "exam_names": sorted(list(set(exam_names))),
                    "students": [{"name": student_name, "id": student_id}],
                    "json_path": str(details_path.resolve()),
                    "report_path": str(report_md.resolve()) if report_md.exists() else None
                })
            else:
                assignment_test = item.stem
                try:
                    cache = json.loads(item.read_text(encoding="utf-8"))
                except Exception:
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
                            students.append({
                                "name": prescan.get("student_name", ""),
                                "id": prescan.get("student_id", "")
                            })
                            
                unique_students = []
                seen = set()
                for student in students:
                    token = (student.get("name", ""), student.get("id", ""))
                    if token in seen or not any(token):
                        continue
                    seen.add(token)
                    unique_students.append(student)
                    
                if active_student:
                    unique_students = [active_student]
                    
                history.append({
                    "id": assignment_test,
                    "display_name": format_assignment_display_name(assignment_test),
                    "updated_at": cache.get("updated_at"),
                    "file_count": len(files),
                    "files": sorted(filenames),
                    "exam_names": sorted(set(exam_names)) if exam_names else [assignment_test],
                    "students": unique_students
                })
    # Deduplicate: if an exam only has 1 student and a student_report exists for that student+exam, hide the exam
    final_history = []
    student_reports_list = [h for h in history if h.get("report_path") or h.get("json_path")]
    exam_list = [h for h in history if not (h.get("report_path") or h.get("json_path"))]
    
    for ex in exam_list:
        if len(ex.get("students", [])) == 1:
            sid = ex["students"][0]["id"]
            # Hide the standalone exam entry if it only has 1 student and a comprehensive student report exists for them
            has_report = any(sid == sr["id"] for sr in student_reports_list)
            if has_report:
                continue # Skip adding the duplicate exam entry
        final_history.append(ex)
        
    final_history.extend(student_reports_list)
    final_history.sort(key=lambda x: x["updated_at"], reverse=True)
    return final_history


# Legacy placeholders for backwards compatibility in existing tests & code
def get_cached_prescan(filename: str, file_path: str, assignment_test: str) -> dict | None:
    series_name, test_folder, _ = parse_report_filename(filename)
    md_file = LOCAL_MEM_DIR / series_name / test_folder / "Docling_Out" / f"{Path(filename).stem}.md"
    cache_path = md_file.with_name(f"{md_file.stem}_prescan.json")
    if cache_path.exists():
        try:
            cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
            if _fingerprint_matches(cached_data.get("fingerprint", {}), file_path):
                return cached_data.get("extraction")
        except Exception:
            pass
    return None


def save_cached_prescan(filename: str, file_path: str, assignment_test: str, extraction: dict) -> str:
    series_name, test_folder, _ = parse_report_filename(filename)
    md_file = LOCAL_MEM_DIR / series_name / test_folder / "Docling_Out" / f"{Path(filename).stem}.md"
    md_file.parent.mkdir(parents=True, exist_ok=True)
    cache_path = md_file.with_name(f"{md_file.stem}_prescan.json")
    try:
        cache_data = {
            "fingerprint": file_fingerprint(file_path),
            "extraction": extraction
        }
        cache_path.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Legacy save_cached_prescan failed: {e}")
    return series_name


def archive_processed_input_file(filename: str) -> str:
    input_path = Path("input") / filename
    series_name, test_folder, _ = parse_report_filename(filename)
    archive_dir = LOCAL_MEM_DIR / series_name / test_folder / "Source"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / input_path.name

    if input_path.exists():
        if archive_path.exists():
            archive_path.unlink()
        shutil.move(str(input_path), str(archive_path))

    return str(archive_path)


def get_cached_analysis(filename: str, file_path: str, assignment_test: str, student_name: str, student_id: str) -> dict | None:
    series_name, test_folder, exam_code = parse_report_filename(filename)
    return get_cached_analysis_phase_3(series_name, test_folder, exam_code, student_id)


def save_cached_analysis(filename: str, file_path: str, assignment_test: str, student_name: str, student_id: str, extraction: dict) -> str:
    series_name, test_folder, exam_code = parse_report_filename(filename)
    save_phase_3_extraction(series_name, test_folder, exam_code, student_name, student_id, extraction)
    return series_name


def load_student_analysis_json(assignment_test: str, student_name: str, student_id: str) -> dict:
    series_name = assignment_test
    unified = run_phase_4_unified_data(series_name, student_id)
    files = {}
    series_dir = LOCAL_MEM_DIR / series_name
    if series_dir.exists():
        for test_dir in series_dir.iterdir():
            if test_dir.is_dir():
                docling_out_dir = test_dir / "Docling_Out"
                if docling_out_dir.exists():
                    for md_file in docling_out_dir.glob("*.md"):
                        exam_code = test_dir.name.replace(" ", "")
                        prescan = get_cached_analysis_phase_3(series_name, test_dir.name, exam_code, student_id)
                        if prescan:
                            files[md_file.name] = {
                                "extraction": prescan
                            }
    return {"files": files}
