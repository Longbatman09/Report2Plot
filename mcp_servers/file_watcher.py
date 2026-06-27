from mcp.server.fastmcp import FastMCP
import os, json
from pathlib import Path

mcp = FastMCP("file-watcher")
INPUT_DIR = Path("input")

@mcp.tool()
def list_input_files() -> dict:
    """List all PNG, JPG, PDF files in the input folder."""
    files = [f.name for f in INPUT_DIR.iterdir()
             if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf")]
    return {"files": files, "count": len(files)}

@mcp.tool()
def validate_inputs() -> dict:
    """Check minimum 2 files exist and return their paths."""
    files = list_input_files()["files"]
    return {
        "valid": len(files) >= 2,
        "files": files,
        "message": "OK" if len(files) >= 2 else "Need at least 2 report files"
    }

if __name__ == "__main__":
    mcp.run()