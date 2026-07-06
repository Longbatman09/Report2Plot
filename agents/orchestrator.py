import os
import sys
from email.parser import BytesParser
from email.policy import default

# Add project root to sys.path to allow importing from mcp_servers folder
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.append(project_root)

import json
import time
import threading
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

from dotenv import load_dotenv

# Import tools directly for pipeline orchestration
import agents.local_mem as lm
import mcp_servers.file_watcher as fw
import mcp_servers.vision_extractor as ve
import mcp_servers.plot_renderer as pr
import agents.docling_converter as dc
from pathlib import Path

load_dotenv()

# Thread-safe pipeline state management
pipeline_state = {
    "stage": "idle",  # "idle", "extracting", "rendering", "done", "error"
    "progress_current": 0,
    "progress_total": 0,
    "message": "",
    "files": []
}
state_lock = threading.Lock()
pipeline_lock = threading.Lock()
ignore_next_instruction_mtime = None

active_clients = {}  # client_id -> last_seen_time
heartbeat_received = False
last_empty_time = None
startup_time = time.time()

def check_heartbeat_loop(httpd):
    global active_clients, heartbeat_received, last_empty_time
    while True:
        time.sleep(1.0)
        now = time.time()
        with state_lock:
            # 1. Prune clients that haven't pinged in 300 seconds (5 minutes)
            expired_clients = [cid for cid, last_seen in active_clients.items() if now - last_seen > 300.0]
            for cid in expired_clients:
                active_clients.pop(cid, None)
                print(f"[Heartbeat] Client {cid} expired (no ping for 300s).")
            
            # 2. Update last_empty_time if active_clients is empty
            if heartbeat_received and not active_clients:
                if last_empty_time is None:
                    last_empty_time = now
            elif active_clients:
                last_empty_time = None
                
            # 3. Check for shutdown condition
            # Never shut down the server if an active pipeline is in progress (i.e. pipeline_lock is locked)
            if heartbeat_received and not active_clients:
                if not pipeline_lock.locked() and last_empty_time and (now - last_empty_time > 15.0):
                    print("[Heartbeat] No active GUI clients and no pipeline running. Shutting down server...")
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
                    os._exit(0)
            
            # Case B: Server started but no GUI client connected within 60 seconds
            if not heartbeat_received:
                if now - startup_time > 60.0:
                    print("[Heartbeat] No GUI connection established within 60 seconds. Shutting down server...")
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
                    os._exit(0)

def set_state(stage, current=0, total=0, message="", files=None):
    with state_lock:
        pipeline_state["stage"] = stage
        pipeline_state["progress_current"] = current
        pipeline_state["progress_total"] = total
        pipeline_state["message"] = message
        if files is not None:
            pipeline_state["files"] = files



def extract_for_prescan(filename: str):
    file_path = os.path.join("input", filename)
    series_name, test_folder, _ = lm.parse_report_filename(filename)
    cached = lm.get_cached_prescan(filename, file_path, series_name)
    if cached:
        print(f"[local_mem] Reusing cached pre-scan for {filename} ({series_name})")
        return cached, series_name, True

    docling_path = lm.get_docling_document_path(filename)
    result = ve.extract_report_data(docling_path, student_name="the student", student_id="any ID")
    if result.get("exam_name"):
        series_name = lm.detect_assignment_test(filename, result["exam_name"])
    lm.save_cached_prescan(filename, file_path, series_name, result)
    print(f"[local_mem] Saved pre-scan for {filename} -> local_mem/{series_name}")
    return result, series_name, False


def extract_for_analysis(filename: str, student_name: str, student_id: str):
    file_path = os.path.join("input", filename)
    series_name, test_folder, exam_code = lm.parse_report_filename(filename)
    cached = lm.get_cached_analysis_phase_3(series_name, test_folder, exam_code, student_id)
    if cached:
        print(f"[local_mem] Reusing cached analysis for {filename} ({series_name} / {test_folder})")
        return cached, series_name, True

    docling_path = lm.get_docling_document_path(filename)
    
    # FAST PATH: Check if student exists in raw markdown before making Gemini API HTTP request
    if os.path.exists(docling_path) and student_name.strip().lower() != "the student":
        try:
            with open(docling_path, "r", encoding="utf-8") as f:
                md_content = f.read().lower()
            
            clean_name = " ".join(student_name.lower().split())
            clean_id = str(student_id).lower().strip()
            
            name_parts = [p for p in clean_name.split() if len(p) >= 3]
            if not name_parts:
                name_parts = clean_name.split()
                
            name_present = any(part in md_content for part in name_parts) if name_parts else False
            id_present = (clean_id in md_content) if clean_id else False
            
            if not name_present and not id_present:
                print(f"[orchestrator] Fast Path: Student '{student_name}' (ID: {student_id}) not found in markdown content of {filename}. Skipping Gemini API HTTP request.")
                result = {
                    "found_student": False,
                    "student_name": student_name,
                    "student_id": student_id,
                    "numerical_fields": {},
                    "class_averages": {}
                }
                lm.save_phase_3_extraction(series_name, test_folder, exam_code, student_name, student_id, result)
                return result, series_name, False
        except Exception as e:
            print(f"Warning: Failed to check markdown content for student presence: {e}")

    result = ve.extract_report_data(docling_path, student_name=student_name, student_id=student_id)
    import os
    result["test_name"] = os.path.splitext(os.path.basename(filename))[0]
    result["source_file"] = filename
    lm.save_phase_3_extraction(series_name, test_folder, exam_code, student_name, student_id, result)
    print(f"[local_mem] Saved analysis for {filename} -> local_mem/{series_name}/{test_folder}/{exam_code}.json")
    return result, series_name, False


def prescan_input_files():
    inputs = fw.list_input_files()
    files = inputs.get("files", [])
    if not files:
        set_state("scanning", 0, 0, "No files found to scan.")
        return {"common_fields": [], "student": {"name": "", "id": "", "class": "", "section": ""}}

    lm.ensure_local_mem_dir()
    set_state("scanning", 0, len(files), "Initializing scan of input files...")
    
    # Run Phase 1 convert for all input files to their series/test folders
    series_names = set()
    for idx, filename in enumerate(files):
        set_state("scanning", idx + 1, len(files), f"Converting and parsing {filename}...")
        try:
            series_name, test_folder, _ = lm.parse_report_filename(filename)
            series_names.add(series_name)
            lm.get_docling_document_path(filename)
        except Exception as ex:
            print(f"Warning: Phase 1 parsing failed for {filename}: {ex}")
            
    # Run Phase 2 roster for each detected series
    roster_data = {}
    roster_errors = []
    for series_name in series_names:
        try:
            roster_data = lm.run_phase_2_roster(series_name)
        except Exception as ex:
            roster_errors.append(f"Phase 2 roster failed for {series_name}: {ex}")
            print(f"Warning: Phase 2 roster failed for {series_name}: {ex}")
            
    # If no roster succeeded, or no students were found/parsed, raise error
    if not roster_data or not roster_data.get("students"):
        set_state("idle", 0, 0, "Pre-scan failed.")
        error_msg = "Pre-scan failed for every report. No roster was produced."
        if roster_errors:
            error_msg += " Details: " + "; ".join(roster_errors)
        raise RuntimeError(error_msg)
        
    set_state("scanning", len(files), len(files), "Finished scanning. Finalizing results...")
    
    # Map roster_data to payload format
    common_students = [{"name": s["student_name"], "id": s["student_id"]} for s in roster_data.get("students", [])]
    payload = {
        "common_fields": roster_data.get("common_fields", []),
        "common_students": sorted(common_students, key=lambda s: s["name"]),
        "student": common_students[0] if common_students else {"name": "", "id": "", "class": "", "section": ""},
        "exam_name": roster_data.get("series", "Jee Mains WTM")
    }
    
    set_state("idle", 0, 0, "Idle")
    return payload


def prescan_selected_files(selected_files):
    files = [f for f in selected_files if f]
    if not files:
        set_state("scanning", 0, 0, "No selected files to scan.")
        return {"common_fields": [], "common_students": [], "student": {"name": "", "id": "", "class": "", "section": ""}, "exam_name": "", "selected_files": []}

    lm.ensure_local_mem_dir()
    set_state("scanning", 0, len(files), "Initializing scan of selected files...")

    series_names = set()
    scan_results = []
    for idx, filename in enumerate(files):
        set_state("scanning", idx + 1, len(files), f"Converting and parsing {filename}...")
        try:
            series_name, _, _ = lm.parse_report_filename(filename)
            series_names.add(series_name)
            docling_path = lm.get_docling_document_path(filename)
            res = ve.extract_report_data(docling_path, student_name="the student", student_id="any ID")
            scan_results.append(res)
        except Exception as ex:
            print(f"Warning: Selected-file pre-scan failed for {filename}: {ex}")

    if not scan_results:
        set_state("idle", 0, 0, "Pre-scan failed.")
        raise RuntimeError("Pre-scan failed for every selected report. No usable data was produced.")

    response_payload = lm.build_scan_payload(scan_results)
    response_payload["selected_files"] = files
    response_payload["series_names"] = sorted(series_names)

    set_state("idle", 0, 0, "Idle")
    return response_payload


def validate_selected_files(files):
    if not files:
        raise Exception("Select at least 1 report file before analyzing.")

    missing = [filename for filename in files if not (Path("input") / filename).exists()]
    if missing:
        raise Exception("Missing selected file(s): " + ", ".join(missing))


def archive_selected_files(files):
    archived = []
    for filename in files:
        try:
            archived.append(lm.archive_processed_input_file(filename))
        except Exception as ex:
            print(f"Warning: Failed to archive {filename}: {ex}")
    return archived


def clear_input_folder():
    input_dir = Path("input")
    if input_dir.exists():
        for item in input_dir.iterdir():
            try:
                if item.is_file():
                    item.unlink()
            except Exception as e:
                print(f"Warning: Failed to delete {item.name} from input folder: {e}")


def run_student_report_pipeline(instruction_data):
    with pipeline_lock:
        try:
            print("\n=== STARTING STUDENT REPORT PIPELINE ===")
            files = instruction_data.get("input_files", [])
            student_info = instruction_data.get("student", {})
            student_query = (
                student_info.get("query")
                or student_info.get("name")
                or student_info.get("id")
                or ""
            ).strip()

            if not files:
                raise Exception("Select at least 1 report file before generating the student report.")
            if not student_query:
                raise Exception("Enter a student name or ID before generating the student report.")

            set_state("converting", 0, len(files), "Loading student directory...")
            student_record = lm.resolve_student_record(student_query)
            if not student_record:
                if student_info.get("name"):
                    student_record = {
                        "student_name": student_info.get("name"),
                        "student_id": student_info.get("id") or student_info.get("name"),
                        "student_class": student_info.get("class", ""),
                        "student_section": student_info.get("section", "")
                    }
                else:
                    raise Exception(f"Student '{student_query}' not found in All_stud_details.json, and no name was provided manually.")

            student_name = str(student_record.get("student_name", student_info.get("name", ""))).strip()
            student_id = str(student_record.get("student_id", student_info.get("id", ""))).strip()
            if not student_name or not student_id:
                raise Exception("Resolved student record is missing a name or ID.")

            target_exam_name = instruction_data.get("target_exam_name")

            extraction_results = []
            lm.ensure_local_mem_dir()

            for idx, filename in enumerate(files):
                set_state("extracting", idx + 1, len(files), f"Converting and extracting {filename}...")
                docling_path = lm.get_docling_document_path(filename)
                result = ve.extract_report_data(docling_path, student_name=student_name, student_id=student_id)
                import os
                result["test_name"] = os.path.splitext(os.path.basename(filename))[0]
                result["source_file"] = filename
                
                # Export automatically to the respective exam folder in Local_mem
                series_name, test_folder, exam_code = lm.parse_report_filename(filename, target_exam_name)
                lm.save_phase_3_extraction(series_name, test_folder, exam_code, student_name, student_id, result)
                
                extraction_results.append(result)

            set_state("rendering", len(files), len(files), "Assembling per-student JSON and final report...")
            json_path = lm.maintain_per_student_json(student_name, student_id, json.dumps(extraction_results), target_exam_name)
            report_path = lm.render_final_output(student_id, json_path)
            output_format = instruction_data.get("output", {}).get("format", "both")

            if isinstance(json_path, str) and json_path.startswith("Error"):
                raise Exception(json_path)
            if isinstance(report_path, str) and report_path.startswith("Error"):
                raise Exception(report_path)

            generated_files = []
            if output_format in ("markdown", "both"):
                generated_files.append(report_path)

            if output_format in ("charts", "both"):
                # Load the full merged history from json_path for plotting
                full_history = []
                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        full_history_data = json.load(f)
                    for item in full_history_data:
                        if "prescan" in item:
                            full_history.append(item["prescan"])
                except Exception as e:
                    print(f"Warning: Could not load full history for plotting: {e}")
                    full_history = extraction_results

                # Build aggregated data for plot renderer
                aggregated = {
                    "student": {
                        "name": student_name,
                        "id": student_id,
                        "class": "",
                        "section": ""
                    },
                    "results": full_history
                }
                print("Generating charts for student report...")
                plot_res = pr.render_matplotlib(aggregated, "analyze_instruction.json")
                if plot_res.get("status") == "success":
                    generated_files.extend(plot_res.get("charts", []))

            archive_selected_files(files)
            clear_input_folder()

            set_state(
                "done",
                len(files),
                len(files),
                f"Student report generated for {student_name}.",
                generated_files,
            )
            print("=== STUDENT REPORT COMPLETE ===")
            print(f"Student: {student_name} ({student_id})")
            print("Generated Files:")
            for f in generated_files:
                print(f" - {f}")
        except Exception as e:
            print(f"\n❌ Student report pipeline error: {e}")
            import traceback
            traceback.print_exc()
            set_state("error", 0, 0, str(e))

def run_pipeline(instruction_data):
    with pipeline_lock:
        try:
            print("\n=== STARTING ANALYSIS PIPELINE ===")
            files = instruction_data.get("input_files", [])
            set_state("converting", 0, len(files), "Checking files...")

            validate_selected_files(files)

            student_info = instruction_data["student"]
            student_name = student_info["name"]
            student_id = student_info["id"]

            extraction_errors = []

            lm.ensure_local_mem_dir()

            # Phase 1: Convert all input files to markdown using Docling first
            print("Converting input files to Docling documents...")
            for idx, filename in enumerate(files):
                set_state("converting", idx + 1, len(files), f"Converting {filename} via Docling...")
                try:
                    lm.get_docling_document_path(filename)
                except Exception as ex:
                    print(f"Warning: Conversion failed for {filename}: {ex}")

            # Phase 2: Run Phase 2 Roster to ensure we have all_students_list.json
            series_name = instruction_data.get("exam_name") or instruction_data.get("test_folder")
            if not series_name or series_name == "Unknown":
                series_name, _, _ = lm.parse_report_filename(files[0])
            lm.run_phase_2_roster(series_name)

            # Phase 3: Extract data from the converted documents per test folder
            for idx, filename in enumerate(files):
                set_state("extracting", idx + 1, len(files), f"Extracting marks from {filename}...")
                print(f"[{idx+1}/{len(files)}] Processing {filename}...")

                try:
                    res, file_series, _ = extract_for_analysis(filename, student_name, student_id)
                    series_name = file_series
                except Exception as ex:
                    msg = f"{filename}: {ex}"
                    extraction_errors.append(msg)
                    print(f"Warning: Error extracting data from {msg}")

            # Phase 4: Unified data assembly from <ExamCode>.json files only
            print("Assembling Phase 4 unified data for analysis/plotting...")
            unified = lm.run_phase_4_unified_data(series_name, student_id)
            aggregated = lm.map_unified_to_aggregated(unified)
            
            # Count actual found tests
            found_count = sum(1 for t in unified["tests"] if t["found"])
            if found_count == 0:
                raise Exception(
                    f"Failed to find student '{student_name}' or extract scores from any reports. "
                    + ("Details: " + "; ".join(extraction_errors) if extraction_errors else "")
                )

            set_state("rendering", len(files), len(files), "Rendering charts and building presentation slides...")
            print("Generating charts and PowerPoint deck...")

            generated_files = []
            output_format = instruction_data.get("output", {}).get("format", "both")
            instruction_path = "analyze_instruction.json"

            if output_format in ("plotly", "matplotlib", "pptx", "both"):
                print("Rendering Matplotlib charts...")
                plot_res = pr.render_matplotlib(aggregated, instruction_path)
                if output_format in ("plotly", "matplotlib", "both") and plot_res.get("status") == "success":
                    generated_files.extend(plot_res.get("charts", []))

            if output_format in ("pptx", "both"):
                print("Building PowerPoint Presentation...")
                pptx_res = pr.render_pptx(aggregated, instruction_path)
                if pptx_res.get("status") == "success":
                    generated_files.append(pptx_res.get("file"))

            completion_message = "Analysis Complete!"
            if extraction_errors:
                completion_message += " Some files were skipped."
            set_state("done", len(files), len(files), completion_message, generated_files)
            archived_files = archive_selected_files(files)
            if archived_files:
                print("Archived input files:")
                for archived_file in archived_files:
                    print(f" - {archived_file}")
            
            clear_input_folder()
            
            print("=== ANALYSIS COMPLETE ===")
            print("Generated Files:")
            for f in generated_files:
                print(f" - {f}")

            if extraction_errors:
                print("Skipped files / warnings:")
                for msg in extraction_errors:
                    print(f" - {msg}")

        except Exception as e:
            print(f"\n❌ Pipeline Error: {e}")
            import traceback
            traceback.print_exc()
            set_state("error", 0, 0, str(e))

class OrchestratorHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default request logging to keep console logs cleaner
        pass
        
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        
        if path in ("/", "/description_page.html"):
            # Redirect to history if history exists and we're not explicitly requesting a new report
            if lm.has_history() and not query.get("new"):
                self.send_response(302)
                self.send_header("Location", "/history_page.html")
                self.end_headers()
                return
            self.serve_static_file("ui/description_page.html", "text/html")
        elif path == "/history_page.html":
            self.serve_static_file("ui/history_page.html", "text/html")
        elif path == "/exam_detail_page.html":
            self.serve_static_file("ui/exam_detail_page.html", "text/html")
        elif path == "/api/inputs":
            self.handle_api_inputs()
        elif path == "/api/student-directory":
            self.handle_api_student_directory()
        elif path == "/api/scan":
            self.handle_api_scan()
        elif path == "/api/history":
            self.handle_api_history()
        elif path == "/api/status":
            self.handle_api_status()
        elif path == "/api/heartbeat":
            self.handle_api_heartbeat(query)
        elif path == "/api/open-output":
            self.handle_api_open_output()
        elif path == "/api/file":
            self.handle_api_serve_file(query)
        elif path == "/api/clear_memory":
            self.handle_api_clear_memory()
        else:
            self.send_error(404, "File Not Found")
            
    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        if path == "/api/upload":
            self.handle_api_upload()
        elif path == "/api/scan":
            self.handle_api_scan()
        elif path == "/api/analyze":
            self.handle_api_analyze()
        elif path == "/api/clear_memory":
            self.handle_api_clear_memory()
        elif path == "/api/heartbeat":
            query = urllib.parse.parse_qs(parsed_url.query)
            self.handle_api_heartbeat(query)
        else:
            self.send_error(404, "Endpoint Not Found")
            
    def serve_static_file(self, file_path, content_type):
        if not os.path.exists(file_path):
            self.send_error(404, "File Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(file_path, "rb") as f:
            self.wfile.write(f.read())
            
    def handle_api_clear_memory(self):
        try:
            import shutil
            folders = ["input", "local_mem", "output", "Output"]
            for f in folders:
                folder_path = Path(f)
                if folder_path.exists():
                    for item in folder_path.iterdir():
                        if item.name not in (".", "..", ".gitkeep", ".keep"):
                            try:
                                if item.is_dir():
                                    shutil.rmtree(item)
                                else:
                                    item.unlink()
                            except Exception as ex:
                                print(f"Warning: could not delete {item}: {ex}")
            self.send_json_response(200, {"status": "success", "message": "Memory cleared successfully."})
        except Exception as e:
            self.send_json_response(500, {"status": "error", "error": str(e)})

    def handle_api_inputs(self):
        try:
            res = fw.validate_inputs()
            self.send_json_response(200, res)
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})

    def handle_api_student_directory(self):
        try:
            students = lm.load_student_directory()
            self.send_json_response(200, {"count": len(students), "students": students})
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})

    def handle_api_upload(self):
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise Exception("Expected multipart/form-data upload.")

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
            )

            uploaded_files = []
            input_dir = Path("input")
            input_dir.mkdir(parents=True, exist_ok=True)

            for field in message.iter_parts():
                if field.get_content_disposition() != "form-data":
                    continue

                original_name = os.path.basename(field.get_filename() or "")
                if not original_name:
                    continue

                destination = input_dir / original_name
                counter = 1
                while destination.exists():
                    destination = input_dir / f"{Path(original_name).stem}_{counter}{Path(original_name).suffix}"
                    counter += 1

                payload = field.get_payload(decode=True) or b""
                with open(destination, "wb") as output_file:
                    output_file.write(payload)
                uploaded_files.append(destination.name)

            if not uploaded_files:
                raise Exception("No files were uploaded.")

            self.send_json_response(200, {"status": "success", "files": uploaded_files})
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})
            
    def handle_api_scan(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            payload = {}
            if content_length:
                raw_data = self.rfile.read(content_length)
                if raw_data:
                    payload = json.loads(raw_data.decode("utf-8"))

            selected_files = payload.get("input_files") or payload.get("selected_files") or []
            if selected_files:
                response_payload = prescan_selected_files(selected_files)
            else:
                response_payload = prescan_input_files()
            self.send_json_response(200, response_payload)
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})

    def handle_api_history(self):
        try:
            assignments = lm.list_assignment_history()
            self.send_json_response(
                200,
                {
                    "count": len(assignments),
                    "has_history": len(assignments) > 0,
                    "assignments": assignments,
                },
            )
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})
            
    def handle_api_status(self):
        with state_lock:
            self.send_json_response(200, pipeline_state)
            
    def handle_api_heartbeat(self, query):
        global heartbeat_received, active_clients, last_empty_time
        client_id_list = query.get("client_id")
        closing_list = query.get("closing")
        
        if client_id_list:
            client_id = client_id_list[0]
            is_closing = closing_list and closing_list[0] == "true"
            
            with state_lock:
                heartbeat_received = True
                if is_closing:
                    active_clients.pop(client_id, None)
                    print(f"[Heartbeat] Client {client_id} closed/closing.")
                else:
                    active_clients[client_id] = time.time()
                
                if not active_clients:
                    if last_empty_time is None:
                        last_empty_time = time.time()
                else:
                    last_empty_time = None
                    
        self.send_json_response(200, {"status": "alive"})
            
    def handle_api_open_output(self):
        try:
            candidate_paths = [os.path.abspath("Output"), os.path.abspath("output")]
            abs_path = next((path for path in candidate_paths if os.path.exists(path)), candidate_paths[0])
            if sys.platform == "win32":
                os.startfile(abs_path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", abs_path])
            else:
                import subprocess
                subprocess.run(["xdg-open", abs_path])
            self.send_json_response(200, {"status": "success"})
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})
            
    def handle_api_serve_file(self, query):
        file_path_list = query.get("path")
        if not file_path_list:
            self.send_error(400, "Missing path parameter")
            return
            
        file_path = file_path_list[0]
        abs_outputs = [os.path.abspath("output"), os.path.abspath("Output")]
        abs_file = os.path.abspath(file_path)
        
        if not any(abs_file.startswith(abs_output) for abs_output in abs_outputs):
            self.send_error(403, "Access Denied")
            return
            
        if not os.path.exists(file_path):
            self.send_error(404, "File Not Found")
            return
            
        self.send_response(200)
        if file_path.endswith(".png"):
            self.send_header("Content-Type", "image/png")
        elif file_path.endswith(".pptx"):
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
            self.send_header("Content-Disposition", f"attachment; filename={os.path.basename(file_path)}")
        else:
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(file_path, "rb") as f:
            self.wfile.write(f.read())
            
    def handle_api_analyze(self):
        global ignore_next_instruction_mtime
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)
        
        try:
            payload = json.loads(post_data.decode("utf-8"))
            if not payload.get("test_folder") and payload.get("exam_name"):
                payload["test_folder"] = payload["exam_name"]

            if payload.get("workflow") == "student_report":
                threading.Thread(target=run_student_report_pipeline, args=(payload,), daemon=True).start()
                self.send_json_response(200, {"status": "processing"})
                return
            
            # Write configuration state to analyze_instruction.json
            with open("analyze_instruction.json", "w") as f:
                json.dump(payload, f, indent=2)

            ignore_next_instruction_mtime = os.path.getmtime("analyze_instruction.json")

            # Spawn pipeline runner thread
            threading.Thread(target=run_pipeline, args=(payload,), daemon=True).start()

            self.send_json_response(200, {"status": "processing"})
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})
            
    def send_json_response(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

def watch_instruction_file():
    global ignore_next_instruction_mtime
    last_mtime = 0
    instruction_path = "analyze_instruction.json"
    if os.path.exists(instruction_path):
        last_mtime = os.path.getmtime(instruction_path)
        
    while True:
        try:
            if os.path.exists(instruction_path):
                mtime = os.path.getmtime(instruction_path)
                if mtime > last_mtime:
                    last_mtime = mtime
                    if ignore_next_instruction_mtime == mtime:
                        ignore_next_instruction_mtime = None
                        continue
                    print(f"\n[Watcher] Detected changes in {instruction_path}. Re-running pipeline...")
                    with open(instruction_path, "r") as f:
                        data = json.load(f)
                    threading.Thread(target=run_pipeline, args=(data,), daemon=True).start()
            time.sleep(1.0)
        except Exception as e:
            print(f"[Watcher] Error: {e}")
            time.sleep(1.0)

def main(port=5000):
    handler_class = OrchestratorHTTPHandler
    server_address = ('', port)
    
    while port <= 5010:
        try:
            httpd = ThreadingHTTPServer(server_address, handler_class)
            print(f"\n==================================================")
            print(f"Report2Statistics Orchestrator & UI Server Ready!")
            print(f"URL: http://localhost:{port}/description_page.html")
            print(f"==================================================\n")
            
            if len(sys.argv) > 1 and sys.argv[1] == "--watch":
                print("[Watcher] Starting watcher thread for direct modifications to analyze_instruction.json...")
                threading.Thread(target=watch_instruction_file, daemon=True).start()
                
            # Start the heartbeat check thread
            print("[Heartbeat] Starting heartbeat check thread...")
            threading.Thread(target=check_heartbeat_loop, args=(httpd,), daemon=True).start()
            
            httpd.serve_forever()
            break
        except OSError:
            print(f"Port {port} in use, trying next...")
            port += 1
            server_address = ('', port)
            
    if port > 5010:
        print("Error: Could not start the HTTP server. Ports 5000-5010 are already occupied.")
        sys.exit(1)

if __name__ == "__main__":
    main()
