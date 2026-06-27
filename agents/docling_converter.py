import os
import sys
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

_shared_converter = None

def get_converter():
    global _shared_converter
    if _shared_converter is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        
        _shared_converter = DocumentConverter(
            format_options={
                "pdf": PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
    return _shared_converter

def convert_file(file_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{file_path.stem}.md"
    
    print(f"Converting {file_path.name} to {output_path.name}...")
    
    converter = get_converter()
    result = converter.convert(str(file_path))
    markdown_content = result.document.export_to_markdown()
    
    output_path.write_text(markdown_content, encoding="utf-8")
    print(f"Successfully converted and saved: {output_path.name}")
    return output_path

def convert_all_inputs():
    from agents.local_mem import detect_assignment_test
    
    input_dir = Path("input")
    local_mem_dir = Path("local_mem")
    
    if not input_dir.exists():
        print("Input directory does not exist.")
        return
        
    supported_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".txt"}
    files_to_convert = [
        f for f in input_dir.iterdir()
        if f.suffix.lower() in supported_extensions and f.is_file()
    ]
    
    if not files_to_convert:
        print("No supported input files found to convert.")
        return
        
    print(f"Found {len(files_to_convert)} files to convert.")
    for file_path in files_to_convert:
        try:
            assignment_test = detect_assignment_test(file_path.name)
            output_dir = local_mem_dir / assignment_test / "Docling_Out"
            output_path = output_dir / f"{file_path.stem}.md"
            if output_path.exists() and output_path.stat().st_mtime >= file_path.stat().st_mtime:
                print(f"Skipping conversion for {file_path.name} (already up-to-date)")
                continue
            convert_file(file_path, output_dir)
        except Exception as e:
            print(f"Error converting {file_path.name}: {e}")

if __name__ == "__main__":
    convert_all_inputs()
