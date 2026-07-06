# Project Presentation Information

> This document contains a detailed analysis of the Report2Plot (Report2Statistics) project, suitable for preparing a 4–5 minute presentation speech.

---

# 1. Basic Information

**Project Name:** Report2Plot 

**One-line Description (30 words max):** An automated AI-powered tool that extracts student marks from unstructured report files to generate analytical charts and PowerPoint presentations.

**Team Size:** N/A

**Your Role:** N/A

**Course / Competition (if any):** N/A

---

# 2. Problem Statement

### What problem does your project solve?
Extracting marks and performance data manually from unstructured student reports (like PDFs and images) to perform statistical analysis and build presentations is extremely time-consuming and prone to human error.

### Who experiences this problem?
Teachers, educators, school administrators, and academic analysts.

### Why are current methods inefficient or difficult?
Manual data entry requires hours of repetitive work. Additionally, generating custom charts and presentations for individual students or entire classes requires combining multiple tools (Excel, PowerPoint, etc.), which disrupts the workflow.

### Why did you choose this problem?
To automate and streamline the grading and analysis workflow, demonstrating how AI can be leveraged to eliminate manual data entry and instantly produce presentation-ready insights.

---

# 3. Solution Overview

Describe your solution in simple language.

### What does the user do?
The user launches the application, opens the web-based UI, uploads student report files (e.g., PDFs), and clicks to analyze or generate a student report.

### What happens internally?
The system first converts the uploaded files into structured Markdown using Docling. Then, it uses an AI Vision Extractor (powered by Google Gemini) to extract the student's marks. To optimize performance and reduce API costs, the system caches this data in a local memory module. Finally, it aggregates the data and uses specialized renderers to create statistical charts and presentation slides.

### What is the final output?
Generated statistical charts (images) and a downloadable PowerPoint (.pptx) presentation summarizing the student's performance.

---

# 4. Key Features

- Automated data extraction from PDFs and images using Docling and Gemini AI.
- Intelligent local caching (`local_mem`) to avoid redundant API calls.
- Browser-based Web UI for easy file uploading and pipeline tracking.
- Automated chart generation using Matplotlib.
- Automated PowerPoint (.pptx) deck generation.
- History tracking for past assignments and tests.
- Modular architecture using Model Context Protocol (MCP) servers.

---

# 5. Project Workflow

```text
User Uploads Report Files via Web UI
    ↓
Docling Document Conversion (Phase 1)
    ↓
AI Data Extraction via Gemini (Phase 3)
    ↓
Data Aggregation & Caching in Local_Mem (Phase 4)
    ↓
Chart Rendering (Matplotlib)
    ↓
PowerPoint Generation (python-pptx)
    ↓
Final Output Displayed in Web UI & Downloadable
```

---

# 6. System Architecture

- **Frontend**: HTML/Vanilla JS Web UI (`description_page.html`, `history_page.html`)
- **Backend**: Python `ThreadingHTTPServer` (`orchestrator.py`)
- **AI Agent & MCP Servers**: Modular servers (`file_watcher`, `vision_extractor`, `plot_renderer`) using `fastmcp`.
- **Data Storage**: Local file system cache (`local_mem/`) for state and parsed JSONs.
- **Processing Utilities**: Docling for PDF-to-Markdown conversion.

---

# 7. Technologies Used

### Programming Languages
- Python, HTML, JavaScript

### Frameworks
- Python's built-in `http.server`
- FastMCP (Model Context Protocol)

### Libraries
- `docling` (Document conversion)
- `google-genai` (Gemini API integration)
- `matplotlib` (Data visualization)
- `python-pptx` (PowerPoint generation)
- `pymupdf` & `pillow` (PDF and Image handling)
- `pydantic`, `python-dotenv`, `watchdog`

### Database
- Local File System (JSON-based local memory)

### AI Models
- Google Gemini

### APIs
- Gemini API

### Other Tools
- Batch script (`run.bat`) for easy environment setup and launching.

---

# 8. AI Components

### Which AI model(s) are used?
Google Gemini.

### Why did you choose them?
Gemini provides excellent multimodal and vision capabilities, making it highly effective at parsing complex tabular data from unstructured report formats.

### Does the project use:

- [x] AI Agents
- [x] Prompt Engineering
- [ ] Retrieval-Augmented Generation (RAG)
- [x] Memory (Implemented via local JSON caching)
- [x] Function/Tool Calling (via MCP Servers)
- [x] External APIs (Gemini)
- [x] OCR (Implicitly via Docling and Gemini Vision)
- [x] Computer Vision

Explain how AI is used in your project:
AI is the core extraction engine. The `vision_extractor` MCP server sends the converted documents to Gemini to intelligently extract test names, student details, and tabular marks, transforming unstructured text into structured JSON data.

---

# 9. Technical Challenges

- Accurately parsing tables and marks from diverse and complex PDF layouts.
- Minimizing expensive and slow AI API calls for repeated analyses.
- Managing long-running data processing pipelines in a simple HTTP server without timing out.
- Orchestrating multiple independent tasks (extraction, rendering) cleanly.

---

# 10. Solutions to Challenges

**Challenge:** API costs and latency for repeated extraction.
**Solution:** Implemented a robust `local_mem` caching system. Before sending a document to Gemini, the system checks if the file has already been parsed and reuses the cached JSON data, bypassing the API entirely.

**Challenge:** Extracting tables reliably from PDFs.
**Solution:** Integrated `Docling` as a pre-processing step to convert PDFs into structured Markdown before passing them to the AI, significantly improving extraction accuracy.

**Challenge:** Clean architecture for diverse tasks.
**Solution:** Adopted the Model Context Protocol (MCP) to decouple file watching, vision extraction, and plot rendering into independent, manageable modules.

---

# 11. Innovation / Unique Selling Points

- **End-to-End Automation:** A fully automated workflow that takes an unstructured PDF and outputs a presentation-ready PowerPoint deck.
- **Intelligent Caching:** The custom local memory system drastically reduces API costs and processing time.
- **MCP Architecture:** Uses the cutting-edge Model Context Protocol to modularize AI tools, making the system highly extensible.

---

# 12. Impact

### Who benefits from your project?
Teachers, professors, and educational institutions.

### How does it help them?
It saves countless hours of manual data entry and report generation, allowing educators to focus on teaching rather than administrative tasks.

### Measurable improvements:
- Significant time saved from automated data entry.
- Instant generation of complex visualizations that would otherwise take hours to create in Excel.

---

# 13. Future Scope

- **Cloud Integration:** Migrate the local file system database to a cloud database (like Firebase or PostgreSQL) for better scalability.
- **Broader Export Options:** Support for Excel exports and comprehensive PDF analytical reports.
- **Advanced Analytics:** A dashboard for class-wide trends, predictive analytics for student performance, and multi-language support.

---

# 14. Demo Flow (Reference Only)

1. **Launch:** Run `run.bat` to start the orchestrator and open the Web UI.
2. **Upload:** Upload sample student report PDFs via the UI.
3. **Analyze:** Enter the student's name/ID and start the analysis pipeline.
4. **Track Progress:** Show the real-time progress bar (Conversion -> Extraction -> Rendering).
5. **View Results:** Display the generated Matplotlib charts and download the final PowerPoint presentation.
6. **History:** Navigate to the History page to demonstrate how past runs are saved and cached.

---

# 15. Performance Metrics

- **Supported file formats:** PDF, Images (via Docling/PyMuPDF)
- **Charts generated:** Dynamic Matplotlib charts based on extracted test data
- **Speed:** Highly optimized response times on subsequent runs due to local caching

---

# 16. Development Journey (Optional)

### What inspired the idea?
The sheer amount of time wasted on grading and manually typing marks into spreadsheets to track student performance.

### Biggest lesson learned?
Caching and pre-processing (like using Docling to generate Markdown) are crucial for building reliable and cost-effective AI applications.

### What would you do differently next time?
Use a more robust asynchronous web framework (like FastAPI) instead of the built-in `http.server` to better handle long-running background pipelines and WebSockets for real-time progress updates.

---

# 17. Additional Notes

The project serves as a great showcase for building compound AI systems where multiple specialized agents (MCP servers) work together under a central orchestrator to achieve a complex goal.
