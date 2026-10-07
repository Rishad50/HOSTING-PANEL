import os
import sys
import json
import shutil
import zipfile
import threading
import subprocess
from flask import Flask, request, jsonify, send_file, render_template_string

app = Flask(__name__)

# যে ডিরেক্টরিতে প্রজেক্টের ফাইলগুলো থাকবে
WORKSPACE_DIR = os.path.abspath("./server_workspace")
os.makedirs(WORKSPACE_DIR, exist_ok=True)

# ডিফল্ট একটি main.py ফাইল তৈরি করে রাখা যাতে শুরুতেই রান করা যায়
default_main = os.path.join(WORKSPACE_DIR, "main.py")
if not os.path.exists(default_main):
    with open(default_main, "w", encoding="utf-8") as f:
        f.write('import time\n\nprint("Server process started!")\nwhile True:\n    print("Server heartbeat running...")\n    time.sleep(3)\n')

CONFIG_FILE = os.path.join(WORKSPACE_DIR, ".panel_config.json")

# সার্ভার প্রসেস ও লগ ম্যানেজমেন্ট
server_process = None
server_logs = []
logs_lock = threading.Lock()
MAX_LOG_LINES = 1000

def append_log(text):
    global server_logs
    with logs_lock:
        server_logs.append(text)
        if len(server_logs) > MAX_LOG_LINES:
            server_logs = server_logs[-MAX_LOG_LINES:]

def get_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"main_file": "main.py", "req_file": "requirements.txt"}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def stream_process_output(proc):
    for line in iter(proc.stdout.readline, ''):
        append_log(line)
    proc.stdout.close()
    proc.wait()
    append_log(f"\n[Process terminated with exit code {proc.returncode}]\n")

def safe_path(relative_path):
    """পাথ ট্রাভার্সাল (Directory Traversal) প্রতিরোধ করার জন্য সেফ পাথ হ্যান্ডলার"""
    if not relative_path:
        return WORKSPACE_DIR
    target = os.path.abspath(os.path.join(WORKSPACE_DIR, relative_path))
    if not target.startswith(WORKSPACE_DIR):
        raise ValueError("Invalid directory path access!")
    return target


# ==========================================
# 1. FRONTEND ROUTE
# ==========================================
@app.route("/")
def index():
    # একই ডিরেক্টরিতে index.html থাকলে সেটি লোড হবে
    if os.path.exists("index.html"):
        return send_file("index.html")
    return "<h3>Error: 'index.html' not found in the current directory!</h3>"


# ==========================================
# 2. SERVER CONTROL ENDPOINTS
# ==========================================
@app.route("/api/start/<server_id>", methods=["POST"])
def start_server(server_id):
    global server_process
    if server_process and server_process.poll() is None:
        return jsonify({"message": "Server is already running"}), 200

    cfg = get_config()
    main_script = cfg.get("main_file", "main.py")
    script_full_path = safe_path(main_script)

    if not os.path.exists(script_full_path):
        return jsonify({"error": f"Entrypoint '{main_script}' not found!"}), 400

    append_log(f"\n>>> Starting python {main_script}...\n")
    try:
        server_process = subprocess.Popen(
            [sys.executable, script_full_path],
            cwd=WORKSPACE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        t = threading.Thread(target=stream_process_output, args=(server_process,), daemon=True)
        t.start()
        return jsonify({"message": "Server started successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stop/<server_id>", methods=["POST"])
def stop_server(server_id):
    global server_process
    if server_process and server_process.poll() is None:
        try:
            server_process.terminate()
            server_process.wait(timeout=3)
        except Exception:
            server_process.kill()
        append_log("\n>>> Server process stopped by user.\n")
        server_process = None
        return jsonify({"message": "Server stopped"}), 200
    return jsonify({"message": "Server is not running"}), 200

@app.route("/api/restart/<server_id>", methods=["POST"])
def restart_server(server_id):
    stop_server(server_id)
    return start_server(server_id)


# ==========================================
# 3. TERMINAL LOGS & COMMANDS
# ==========================================
@app.route("/api/logs/<server_id>", methods=["GET"])
def get_logs(server_id):
    with logs_lock:
        output = "".join(server_logs)
    return jsonify({"logs": output})

@app.route("/api/clear_logs/<server_id>", methods=["POST"])
def clear_logs(server_id):
    global server_logs
    with logs_lock:
        server_logs.clear()
    return jsonify({"message": "Logs cleared"}), 200

@app.route("/api/command", methods=["POST"])
def run_command():
    data = request.get_json() or {}
    cmd = data.get("cmd", "").strip()
    if not cmd:
        return jsonify({"error": "Command is empty"}), 400

    append_log(f"\n$ {cmd}\n")

    def execute_cmd(command):
        try:
            proc = subprocess.Popen(
                command,
                cwd=WORKSPACE_DIR,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            for line in iter(proc.stdout.readline, ''):
                append_log(line)
            proc.stdout.close()
            proc.wait()
        except Exception as err:
            append_log(f"Command Error: {str(err)}\n")

    threading.Thread(target=execute_cmd, args=(cmd,), daemon=True).start()
    return jsonify({"message": "Command started"}), 200


# ==========================================
# 4. FILE MANAGER ENDPOINTS
# ==========================================
@app.route("/api/files/<server_id>", methods=["GET"])
def list_files(server_id):
    path_param = request.args.get("path", "").strip()
    try:
        dir_path = safe_path(path_param)
        if not os.path.exists(dir_path) or not os.path.isdir(dir_path):
            return jsonify({"files": []}), 200

        items = []
        for name in os.listdir(dir_path):
            if name.startswith("."): # লুকানো ফাইল বাদ দেওয়া
                continue
            item_path = os.path.join(dir_path, name)
            items.append({
                "name": name,
                "is_dir": os.path.isdir(item_path)
            })
        return jsonify({"files": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/file/<server_id>", methods=["GET", "POST", "DELETE"])
def handle_file(server_id):
    if request.method == "GET":
        path_param = request.args.get("path", "").strip()
        try:
            file_path = safe_path(path_param)
            if not os.path.isfile(file_path):
                return jsonify({"error": "File not found"}), 404
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return jsonify({"content": content})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    data = request.get_json() or {}
    rel_path = data.get("path", "").strip()

    if request.method == "POST":
        content = data.get("content", "")
        try:
            file_path = safe_path(rel_path)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            return jsonify({"message": "File saved"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if request.method == "DELETE":
        try:
            target_path = safe_path(rel_path)
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            elif os.path.isfile(target_path):
                os.remove(target_path)
            else:
                return jsonify({"error": "Target does not exist"}), 404
            return jsonify({"message": "Deleted successfully"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route("/api/rename/<server_id>", methods=["POST"])
def rename_item(server_id):
    data = request.get_json() or {}
    old_p = data.get("old_path", "")
    new_p = data.get("new_path", "")
    try:
        src = safe_path(old_p)
        dst = safe_path(new_p)
        os.rename(src, dst)
        return jsonify({"message": "Renamed successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/create_folder/<server_id>", methods=["POST"])
def create_folder(server_id):
    data = request.get_json() or {}
    folder_rel = data.get("path", "").strip()
    try:
        target = safe_path(folder_rel)
        os.makedirs(target, exist_ok=True)
        return jsonify({"message": "Folder created"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload/<server_id>", methods=["POST"])
def upload_files(server_id):
    upload_dir_rel = request.form.get("path", "").strip()
    try:
        dest_dir = safe_path(upload_dir_rel)
        os.makedirs(dest_dir, exist_ok=True)

        files = request.files.getlist("file")
        for file in files:
            if file and file.filename:
                save_dest = os.path.join(dest_dir, file.filename)
                file.save(save_dest)
        return jsonify({"message": f"{len(files)} files uploaded"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/extract/<server_id>", methods=["POST"])
def extract_zip(server_id):
    data = request.get_json() or {}
    zip_rel = data.get("file_path", "")
    dest_rel = data.get("target_path", "")
    try:
        zip_full = safe_path(zip_rel)
        dest_full = safe_path(dest_rel)
        if not zipfile.is_zipfile(zip_full):
            return jsonify({"error": "Invalid zip file"}), 400
        with zipfile.ZipFile(zip_full, 'r') as zip_ref:
            zip_ref.extractall(dest_full)
        return jsonify({"message": "Extracted successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==========================================
# 5. STARTUP CONFIG ENDPOINTS
# ==========================================
@app.route("/api/get_startup/<server_id>", methods=["GET"])
def get_startup_cfg(server_id):
    return jsonify(get_config())

@app.route("/api/set_startup/<server_id>", methods=["POST"])
def set_startup_cfg(server_id):
    data = request.get_json() or {}
    cfg = {
        "main_file": data.get("main_file", "main.py"),
        "req_file": data.get("req_file", "requirements.txt")
    }
    save_config(cfg)
    return jsonify({"message": "Startup config updated"}), 200


# ==========================================
# RUN THE APPLICATION
# ==========================================
if __name__ == "__main__":
    print(f"[*] Panel running on http://localhost:5000")
    print(f"[*] Managed Workspace directory: {WORKSPACE_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False)
