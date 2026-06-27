from mcp.server.fastmcp import FastMCP
import json
import os
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv
import fitz  # pymupdf
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

mcp = FastMCP("vision-extractor")

# Load local configuration for desktop runs and direct imports.
load_dotenv()

# Lazy client initialization to prevent import-time exceptions
client = None


def get_client():
    global client
    if client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Gemini API key not configured. Set GEMINI_API_KEY or GOOGLE_API_KEY in the environment or .env."
            )
        client = genai.Client(api_key=api_key)
    return client

class VisionExtractionResult(BaseModel):
    class NumericField(BaseModel):
        name: str = Field(description="Machine-friendly field name such as mathematics or total_mark")
        value: float = Field(description="Numeric value for the field")

    class StudentEntry(BaseModel):
        student_name: str = Field(description="The full name of the student")
        student_id: str = Field(description="The ID or roll number of the student")

    exam_name: str = Field(description="The generic type or name of the exam/assignment without any numbers, series, or sequence identifiers (e.g. 'Unit Test' instead of 'Unit Test 1' or 'Unit Test 2'). This acts as the assignment_name.")
    test_name: str = Field(default="", description="The specific name or number of the test indicating which test it is using Sequential Test Numbering (e.g. 'WTM 29', 'WTM 30', 'Test 1', 'Test 2', etc.).")
    data_mode: str = Field(description="Mode of the report: 'single' (only this student) or 'grouped' (class-wide report)")
    found_student: bool = Field(description="Whether the student's record/score was successfully found in this report")
    student_name: str = Field(default="", description="The name of the student found in the report.")
    student_id: str = Field(default="", description="The ID of the student found in the report.")
    student_class: str = Field(default="", description="The class of the student if found (e.g., '12').")
    student_section: str = Field(default="", description="The section or stream of the student if found (e.g., 'Science', 'A').")
    numerical_fields: list[NumericField] = Field(
        default_factory=list,
        description="All numerical fields (e.g. scores, ranks, marks) extracted for the student. Exclude non-numeric fields."
    )
    class_averages: list[NumericField] = Field(
        default_factory=list,
        description="Optional class averages or mean scores for each field if available in the report."
    )
    all_students: list[StudentEntry] = Field(
        default_factory=list,
        description="A list of all students (names and roll numbers/student IDs) listed in the report. If this is a grouped class report, list all students. If it is a single student report, list only that student."
    )


def _normalize_numeric_fields(items: list[VisionExtractionResult.NumericField] | list[dict]) -> Dict[str, float]:
    normalized: Dict[str, float] = {}
    for item in items:
        if isinstance(item, dict):
            name = item.get("name")
            value = item.get("value")
        else:
            name = item.name
            value = item.value
        if not name:
            continue
        try:
            normalized[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return normalized

def build_content_parts(path: str) -> list[types.Part]:
    p = Path(path)
    ext = p.suffix.lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".pdf": "application/pdf"}
    if ext == ".pdf":
        # Convert every PDF page to PNG for vision so multi-page reports are preserved.
        doc = fitz.open(path)
        parts = []
        try:
            for page in doc:
                pix = page.get_pixmap(dpi=150)
                parts.append(types.Part.from_bytes(data=pix.tobytes("png"), mime_type="image/png"))
        finally:
            doc.close()
        return parts
    with open(path, "rb") as f:
        return [types.Part.from_bytes(data=f.read(), mime_type=media_map.get(ext, "image/jpeg"))]

@mcp.tool()
def extract_report_data(file_path: str, student_name: str,
                          student_id: str) -> dict:
    """
    Use Gemini to extract numerical data from a report file (PDF, Image, or Docling Markdown).
    Returns exam name, data type (single/grouped), and all numerical fields.
    """
    if file_path.lower().endswith(".md"):
        with open(file_path, "r", encoding="utf-8") as f:
            text_content = f.read()
        content_parts = [text_content]
    else:
        content_parts = build_content_parts(file_path)
    
    prompt = f"""
    Extract data from this academic report.
    We are searching for student: {student_name} (ID: {student_id}).
    If student_name is 'the student' or empty, please identify the primary student in the report and extract their details.
    
    Please populate the fields in the requested schema.
    For the exam_name field (which represents the generic assignment_name), do NOT include any numbers, series, or sequence numbers (e.g. use "Unit Test" instead of "Unit Test 1" or "Unit Test 2"). There should only be the generic type of exam.
    For the test_name field, use Sequential Test Numbering to tell which test it is (e.g. "WTM 29", "WTM 30", "Unit Test 1", "Unit Test 2", etc. based on the report content or context).
    If the student is not present in the report or class list, set found_student to false.
    Extract the student's name, ID, class, and section if visible in the report.
    If you see class average, mean, or average marks/scores for the subjects, extract them into class_averages.
    Only include fields that are numerical in numerical_fields and class_averages.
    
    Also, please populate the all_students list with all students found in the report (their names and roll numbers/student IDs).
    """
    
    genai_client = get_client()
    response = genai_client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[*content_parts, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VisionExtractionResult,
        )
    )
    
    if response.parsed:
        parsed = response.parsed.model_dump()
    else:
        parsed = json.loads(response.text)

    parsed["numerical_fields"] = _normalize_numeric_fields(parsed.get("numerical_fields", []))
    parsed["class_averages"] = _normalize_numeric_fields(parsed.get("class_averages", []))
    return parsed

@mcp.tool()
def detect_common_data_types(results: list) -> dict:
    """
    Given a list of per-file extraction results,
    find numerical fields common across ALL files.
    """
    if not results:
        return {"common_fields": []}
    field_sets = [set(r.get("numerical_fields", {}).keys()) for r in results]
    common = set.intersection(*field_sets) if field_sets else set()
    return {"common_fields": sorted(list(common))}

if __name__ == "__main__":
    mcp.run()