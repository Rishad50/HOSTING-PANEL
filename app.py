import os
import sys
import shutil
import zipfile
import subprocess
import threading
from typing import List, Optional
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Control Panel API")

# CORS এনাবল করা
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ওয়ার্কস্পেস পাথ (যেখানে ফাইল তৈরি/রান হবে)
BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

# গ্লোবাল স্টেট
logs_buffer = []
server_process: Optional[subprocess.Popen] = None
startup_config = {
    "main_file": "main.py",
    "req_file": "requirements.txt"
}

# --- হেল্পার ফাংশন: পাথ সিকিউরিটি চেক ---
def get_safe_path(rel_path: str) -> Path:
    target = (WORKSPACE_DIR / rel_path.strip("/\\")).resolve()
    if not str(target).startswith(str(WORKSPACE_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path access")
    return target

def append_log(text: str):
    logs_buffer.append(text)
    if len(logs_buffer) > 1000:
        logs_buffer.pop(0)

def log_reader_thread(proc):
    for line in iter(proc.stdout.readline, ''):
        append_log(line)
    proc.stdout.close()

# --- Pydantic মডেলসমূহ ---
class CommandRequest(BaseModel):
    cmd: str
    server_id: str

class FileSaveRequest(BaseModel):
    path: str
    content: str

class FolderCreateRequest(BaseModel):
    path: str
    folder_name: str

class RenameRequest(BaseModel):
    old_path: str
    new_path: str

class ExtractRequest(BaseModel):
    file_path: str
    target_path: Optional[str] = ""

class DeleteRequest(BaseModel):
    path: str

class StartupRequest(BaseModel):
    main_file: str
    req_file: str


# ==================== ১. ফ্রন্টএন্ড UI রাউট ====================
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = BASE_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h3>index.html file not found!</h3>", status_code=404)
    return FileResponse(index_file)


# ==================== ২. সার্ভার কন্ট্রোল API ====================
@app.post("/api/start/{server_id}")
async def start_server(server_id: str):
    global server_process
    if server_process and server_process.poll() is None:
        return {"message": "Server is already running"}

    main_script = get_safe_path(startup_config["main_file"])
    if not main_script.exists():
        # ফাইল না থাকলে একটি ডেমো ফাইল বানিয়ে নেবে
        main_script.write_text("import time\nprint('Server started successfully!')\nwhile True:\n    time.sleep(2)\n    print('Running...')\n")

    cmd = [sys.executable, "-u", str(main_script)]
    server_process = subprocess.Popen(
        cmd,
        cwd=WORKSPACE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    threading.Thread(target=log_reader_thread, args=(server_process,), daemon=True).start()
    append_log(f"\n--- [Started {startup_config['main_file']}] ---\n")
    return {"status": "started"}

@app.post("/api/stop/{server_id}")
async def stop_server(server_id: str):
    global server_process
    if server_process and server_process.poll() is None:
        server_process.terminate()
        server_process = None
        append_log("\n--- [Server Stopped] ---\n")
        return {"status": "stopped"}
    return {"message": "Server is not running"}

@app.post("/api/restart/{server_id}")
async def restart_server(server_id: str):
    await stop_server(server_id)
    return await start_server(server_id)

@app.get("/api/logs/{server_id}")
async def get_logs(server_id: str):
    return {"logs": "".join(logs_buffer)}

@app.post("/api/clear_logs/{server_id}")
async def clear_logs(server_id: str):
    global logs_buffer
    logs_buffer = []
    return {"status": "cleared"}


# ==================== ৩. টার্মিনাল কমান্ড API ====================
@app.post("/api/command")
async def execute_command(req: CommandRequest):
    append_log(f"\n$ {req.cmd}\n")
    try:
        result = subprocess.run(
            req.cmd,
            shell=True,
            cwd=WORKSPACE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30
        )
        append_log(result.stdout)
    except subprocess.TimeoutExpired:
        append_log("Command timed out.\n")
    except Exception as e:
        append_log(f"Error: {str(e)}\n")
    return {"status": "executed"}


# ==================== ৪. ফাইল ম্যানেজার API ====================
@app.get("/api/files/{server_id}")
async def list_files(server_id: str, path: str = Query("")):
    dir_path = get_safe_path(path)
    if not dir_path.exists() or not dir_path.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    items = []
    for item in dir_path.iterdir():
        items.append({
            "name": item.name,
            "is_dir": item.is_dir()
        })
    return {"files": items}

@app.get("/api/file/{server_id}")
async def read_file(server_id: str, path: str = Query(...)):
    file_path = get_safe_path(path)
    if not file_path.exists() or file_path.is_dir():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = "[Binary or unsupported file format]"
    return {"content": content}

@app.post("/api/file/{server_id}")
async def save_file(server_id: str, req: FileSaveRequest):
    file_path = get_safe_path(req.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(req.content, encoding="utf-8")
    return {"status": "saved"}

@app.delete("/api/file/{server_id}")
async def delete_item(server_id: str, req: DeleteRequest):
    target = get_safe_path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Item not found")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"status": "deleted"}

@app.post("/api/create_folder/{server_id}")
async def create_folder(server_id: str, req: FolderCreateRequest):
    target = get_safe_path(req.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"status": "created"}

@app.post("/api/rename/{server_id}")
async def rename_item(server_id: str, req: RenameRequest):
    old_target = get_safe_path(req.old_path)
    new_target = get_safe_path(req.new_path)
    if not old_target.exists():
        raise HTTPException(status_code=404, detail="Old path not found")
    old_target.rename(new_target)
    return {"status": "renamed"}

@app.post("/api/extract/{server_id}")
async def extract_zip(server_id: str, req: ExtractRequest):
    zip_path = get_safe_path(req.file_path)
    dest_path = get_safe_path(req.target_path)
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        raise HTTPException(status_code=400, detail="Invalid zip file")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(dest_path)
    return {"status": "extracted"}

@app.post("/api/upload/{server_id}")
async def upload_files(
    server_id: str,
    path: str = Form(""),
    file: List[UploadFile] = File(...)
):
    dest_dir = get_safe_path(path)
    dest_dir.mkdir(parents=True, exist_ok=True)

    for f in file:
        file_dest = dest_dir / f.filename
        with open(file_dest, "wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
    return {"status": "uploaded", "count": len(file)}


# ==================== ৫. স্টার্টআপ কনফিগারেশন API ====================
@app.get("/api/get_startup/{server_id}")
async def get_startup(server_id: str):
    return startup_config

@app.post("/api/set_startup/{server_id}")
async def set_startup(server_id: str, req: StartupRequest):
    startup_config["main_file"] = req.main_file
    startup_config["req_file"] = req.req_file
    return {"status": "updated", "config": startup_config}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
