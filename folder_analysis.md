# Folder Analysis

This project is an AI-assisted report processing pipeline. It takes student exam files, extracts marks and metadata, caches the results locally, and generates charts and presentations.

## Top-Level Structure

### `agents/`
Core Python logic for the main local workflow.

- `orchestrator.py` controls the processing pipeline and server-side state.
- `docling_converter.py` converts source documents into markdown.
- `local_mem.py` manages the on-disk cache and assignment history.
- `setup_agent.py` prepares or refreshes the generated agent project.
- `wait_for_server.py` waits for the local server to become ready.

### `mcp_servers/`
Model Context Protocol servers used by the pipeline.

- `file_watcher.py` monitors input files and validates the scan set.
- `vision_extractor.py` extracts structured student data from documents and images.
- `plot_renderer.py` turns extracted data into charts and presentation assets.

### `my-agent/`
Generated Google ADK agent project.

This looks like a template agent scaffold with its own app code, tests, deployment terraform, and evaluation assets. It is separate from the report-processing pipeline and appears to be the general agent workspace used by `agents-cli`.

### `scan-agent/`
Second generated agent project, likely the agent that handles scanning or report extraction.

It mirrors `my-agent/` with app code, tests, deployment files, and eval data. The root launcher can sync this folder through `agents/setup_agent.py`, so it is part of the generated agent tooling rather than the handwritten pipeline.

### `Input/`
Source files placed here for processing.

The batch launcher creates this folder if needed. It is the incoming report folder for PDFs, images, or related inputs.

### `Output/`
Generated results are written here.

This is the destination for charts, reports, and other exported artifacts.

### `Local_Mem/`
Persistent local cache and history.

This stores processed exam data, cached extraction results, and past assignment history so repeated runs can skip work.

### `UI/`
Static HTML pages for the browser interface.

- `description_page.html` and `description_page_enhanced.html` provide the analysis/configuration interface.
- `history_page.html` and `history_page_enhanced.html` show prior runs and cached results.

### `tests/`
High-level tests for the local workflow.

`test_local_workflow.py` exercises the cache, orchestrator payload building, extraction schema, and renderer behavior.

## Important Root Files

- `run.bat` is the Windows launcher. It checks Python, creates the working folders, installs dependencies if needed, starts the orchestrator, and opens the browser UI.
- `factory_reset.py` clears `Input/`, `Output/`, and `Local_Mem/`, then resets the JSON state files.
- `analyze_instruction.json` stores the current analysis request or processing instructions.
- `All_stud_details.json` appears to hold the student dataset or historical student records.
- `project_info.md` is a human-readable project summary and presentation brief.
- `requirements.txt` defines the Python dependencies for the local pipeline.

## How The Pieces Fit Together

1. A user places files in `Input/` or uploads them through the UI.
2. `run.bat` starts the orchestrator in `agents/orchestrator.py`.
3. `mcp_servers/file_watcher.py` validates the input set.
4. `docling_converter.py` converts documents to markdown when needed.
5. `vision_extractor.py` extracts structured marks and student details.
6. `local_mem.py` caches the result in `Local_Mem/`.
7. `plot_renderer.py` generates charts and output artifacts in `Output/`.
8. The UI pages in `UI/` display progress and history.

## What The Structure Means

The repository is split into three layers:

- The root contains launch scripts, state files, and user-facing configuration.
- `agents/` and `mcp_servers/` contain the actual pipeline logic.
- `my-agent/` and `scan-agent/` are generated ADK agent projects and deployment scaffolding.

One important detail: the project uses both uppercase and lowercase folder names in different places, such as `Input/` versus `input/`, and `Output/` versus `output/`. That works on Windows but can be brittle on case-sensitive systems.
