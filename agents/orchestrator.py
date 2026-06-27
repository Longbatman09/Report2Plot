import os
import sys

# Add project root to sys.path to allow importing from mcp_servers folder
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.append(project_root)

import json
import time
import threading
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
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
            # 1. Prune clients that haven't pinged in 90 seconds
            expired_clients = [cid for cid, last_seen in active_clients.items() if now - last_seen > 90.0]
            for cid in expired_clients:
                active_clients.pop(cid, None)
                print(f"[Heartbeat] Client {cid} expired (no ping for 90s).")
            
            # 2. Update last_empty_time if active_clients is empty
            if heartbeat_received and not active_clients:
                if last_empty_time is None:
                    last_empty_time = now
            elif active_clients:
                last_empty_time = None
                
            # 3. Check for shutdown condition
            if heartbeat_received and not active_clients:
                if last_empty_time and (now - last_empty_time > 4.0):
                    print("[Heartbeat] No active GUI clients. Shutting down server...")
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
                    os._exit(0)
            
            # Case B: Server started but no GUI client connected within 30 seconds
            if not heartbeat_received:
                if now - startup_time > 30.0:
                    print("[Heartbeat] No GUI connection established within 30 seconds. Shutting down server...")
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


def build_scan_payload(scan_results):
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


def get_docling_document_path(filename: str) -> str:
    input_path = Path("input") / filename
    assignment_test = lm.detect_assignment_test(filename)
    output_dir = Path("local_mem") / assignment_test / "Docling_Out"
    output_path = output_dir / f"{input_path.stem}.md"
    
    if not input_path.exists():
        # Fallback to output path if input doesn't exist (e.g. in test suites / mocks)
        return str(output_path)
        
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or input_path.stat().st_mtime > output_path.stat().st_mtime:
        dc.convert_file(input_path, output_dir)
        
    return str(output_path)


def extract_for_prescan(filename: str):
    file_path = os.path.join("input", filename)
    assignment_test = lm.detect_assignment_test(filename)
    cached = lm.get_cached_prescan(filename, file_path, assignment_test)
    if cached:
        print(f"[local_mem] Reusing cached pre-scan for {filename} ({assignment_test})")
        return cached, assignment_test, True

    docling_path = get_docling_document_path(filename)
    result = ve.extract_report_data(docling_path, student_name="the student", student_id="any ID")
    if result.get("exam_name"):
        assignment_test = lm.detect_assignment_test(filename, result["exam_name"])
    lm.save_cached_prescan(filename, file_path, assignment_test, result)
    print(f"[local_mem] Saved pre-scan for {filename} -> local_mem/{assignment_test}.json")
    return result, assignment_test, False


def extract_for_analysis(filename: str, student_name: str, student_id: str):
    file_path = os.path.join("input", filename)
    assignment_test = lm.detect_assignment_test(filename)
    cached = lm.get_cached_analysis(filename, file_path, assignment_test, student_name, student_id)
    if cached:
        print(f"[local_mem] Reusing cached analysis for {filename} ({assignment_test})")
        return cached, assignment_test, True

    docling_path = get_docling_document_path(filename)
    
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
                    "numerical_fields": [],
                    "class_averages": []
                }
                lm.save_cached_analysis(filename, file_path, assignment_test, student_name, student_id, result)
                return result, assignment_test, False
        except Exception as e:
            print(f"Warning: Failed to check markdown content for student presence: {e}")

    result = ve.extract_report_data(docling_path, student_name=student_name, student_id=student_id)
    if result.get("exam_name"):
        assignment_test = lm.detect_assignment_test(filename, result["exam_name"])
    lm.save_cached_analysis(filename, file_path, assignment_test, student_name, student_id, result)
    print(f"[local_mem] Saved analysis for {filename} -> local_mem/{assignment_test}.json")
    return result, assignment_test, False


def prescan_input_files():
    inputs = fw.list_input_files()
    files = inputs.get("files", [])
    if not files:
        return {"common_fields": [], "student": {"name": "", "id": "", "class": "", "section": ""}}

    lm.ensure_local_mem_dir()
    scan_results = []
    extraction_errors = []
    cache_hits = []
    assignment_tests = []

    for filename in files:
        try:
            result, assignment_test, from_cache = extract_for_prescan(filename)
            scan_results.append(result)
            assignment_tests.append(assignment_test)
            if from_cache:
                cache_hits.append(filename)
        except Exception as ex:
            extraction_errors.append(f"{filename}: {ex}")

    if not scan_results:
        raise RuntimeError(
            "Pre-scan failed for every report. "
            + ("; ".join(extraction_errors) if extraction_errors else "No extraction results were produced.")
        )

    payload = build_scan_payload(scan_results)
    payload["assignment_tests"] = sorted(set(assignment_tests))
    if cache_hits:
        payload["cache_hits"] = cache_hits
        payload["warnings"] = payload.get("warnings", []) + [
            f"Loaded cached pre-scan for: {', '.join(cache_hits)}"
        ]
    if extraction_errors:
        payload["warnings"] = payload.get("warnings", []) + extraction_errors
    return payload

def run_pipeline(instruction_data):
    with pipeline_lock:
        try:
            print("\n=== STARTING ANALYSIS PIPELINE ===")
            files = instruction_data.get("input_files", [])
            set_state("converting", 0, len(files), "Checking files...")

            val = fw.validate_inputs()
            if not val["valid"]:
                raise Exception("Input folder validation failed: " + val.get("message", "Need at least 2 reports"))

            student_info = instruction_data["student"]
            student_name = student_info["name"]
            student_id = student_info["id"]

            results = []
            extraction_errors = []

            lm.ensure_local_mem_dir()

            # Phase 1: Convert all input files to markdown using Docling first
            print("Converting input files to Docling documents...")
            for idx, filename in enumerate(files):
                set_state("converting", idx + 1, len(files), f"Converting {filename} via Docling...")
                try:
                    get_docling_document_path(filename)
                except Exception as ex:
                    print(f"Warning: Conversion failed for {filename}: {ex}")

            # Phase 2: Extract data from the converted documents
            for idx, filename in enumerate(files):
                set_state("extracting", idx + 1, len(files), f"Extracting marks from {filename}...")
                print(f"[{idx+1}/{len(files)}] Processing {filename}...")

                try:
                    res, _, _ = extract_for_analysis(filename, student_name, student_id)
                    res["source_file"] = filename

                    if not res.get("found_student", False):
                        msg = f"Student {student_name} not found in {filename}"
                        extraction_errors.append(msg)
                        print(f"Warning: {msg}. Skipping.")
                        continue

                    results.append(res)
                except Exception as ex:
                    msg = f"{filename}: {ex}"
                    extraction_errors.append(msg)
                    print(f"Warning: Error extracting data from {msg}")

            if not results:
                raise Exception(
                    f"Failed to find student '{student_name}' or extract scores from any reports. "
                    + ("Details: " + "; ".join(extraction_errors) if extraction_errors else "")
                )

            aggregated = {"student": student_info, "results": results}

            set_state("rendering", len(files), len(files), "Rendering charts and building presentation slides...")
            print("Generating charts and PowerPoint deck...")

            generated_files = []
            output_format = instruction_data.get("output", {}).get("format", "both")
            instruction_path = "analyze_instruction.json"

            if output_format in ("matplotlib", "pptx", "both"):
                print("Rendering Matplotlib charts...")
                plot_res = pr.render_matplotlib(aggregated, instruction_path)
                if output_format in ("matplotlib", "both") and plot_res.get("status") == "success":
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
            self.serve_static_file("ui/description_page.html", "text/html")
        elif path == "/history_page.html":
            self.serve_static_file("ui/history_page.html", "text/html")
        elif path == "/api/inputs":
            self.handle_api_inputs()
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
        else:
            self.send_error(404, "File Not Found")
            
    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        if path == "/api/analyze":
            self.handle_api_analyze()
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
            
    def handle_api_inputs(self):
        try:
            res = fw.validate_inputs()
            self.send_json_response(200, res)
        except Exception as e:
            self.send_json_response(500, {"error": str(e)})
            
    def handle_api_scan(self):
        try:
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
            abs_path = os.path.abspath("output")
            if os.path.exists(abs_path):
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
        abs_output = os.path.abspath("output")
        abs_file = os.path.abspath(file_path)
        
        if not abs_file.startswith(abs_output):
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
            httpd = HTTPServer(server_address, handler_class)
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
