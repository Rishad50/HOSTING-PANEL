import os
import sys
import json
import shutil
import zipfile
import threading
import subprocess
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# মূল ডেটা সংরক্ষণ ডিরেক্টরি
BASE_SERVERS_DIR = os.path.abspath("./servers")
os.makedirs(BASE_SERVERS_DIR, exist_ok=True)

# মেমোরিতে সার্ভার স্টেট এবং প্রসেস ম্যানেজমেন্ট
server_processes = {}  # { server_id: subprocess.Popen }
server_logs = {}       # { server_id: [log_strings] }
log_locks = {}         # { server_id: threading.Lock }


def get_server_dir(server_id: str) -> str:
    """সার্ভারের জন্য নিরাপদ ডিরেক্টরি পাথ রিটার্ন করে"""
    safe_id = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
    path = os.path.abspath(os.path.join(BASE_SERVERS_DIR, safe_id))
    os.makedirs(path, exist_ok=True)
    return path


def safe_resolve_path(server_dir: str, rel_path: str) -> str:
    """Directory Traversal (যেমন: ../..) আক্রমণ প্রতিরোধ করার ফাংশন"""
    normalized_rel = rel_path.lstrip("/\\")
    resolved = os.path.abspath(os.path.join(server_dir, normalized_rel))
    if not resolved.startswith(server_dir):
        raise PermissionError("Access denied: Path outside working directory")
    return resolved


def append_log(server_id: str, text: str):
    """সার্ভার লগে আউটপুট যোগ করে"""
    if server_id not in server_logs:
        server_logs[server_id] = []
        log_locks[server_id] = threading.Lock()

    with log_locks[server_id]:
        server_logs[server_id].append(text)
        # মেমোরি নিয়ন্ত্রণে রাখতে সর্বোচ্চ ১০০০ লাইন সংরক্ষণ
        if len(server_logs[server_id]) > 1000:
            server_logs[server_id] = server_logs[server_id][-1000:]


def stream_process_output(server_id: str, process: subprocess.Popen):
    """ব্যাকগ্রাউন্ডে সাব-প্রসেসের STDOUT এবং STDERR রিড করে"""
    try:
        for line in iter(process.stdout.readline, ""):
            if not line:
                break
            append_log(server_id, line)
    except Exception as e:
        append_log(server_id, f"\n[Logger Error]: {str(e)}\n")
    finally:
        process.stdout.close()
        process.wait()
        append_log(server_id, f"\n[System]: Process terminated with exit code {process.returncode}\n")


# -------------------------------------------------------------
# ফ্রন্টএন্ড রুট
# -------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# -------------------------------------------------------------
# ১. সার্ভার কন্ট্রোল API (Start, Stop, Restart, Clear Logs)
# -------------------------------------------------------------
@app.route("/api/start/<server_id>", methods=["POST"])
def start_server(server_id):
    s_dir = get_server_dir(server_id)
    config_file = os.path.join(s_dir, ".config.json")
    
    main_file = "main.py"
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                main_file = cfg.get("main_file", "main.py")
        except Exception:
            pass

    main_script_path = os.path.join(s_dir, main_file)
    if not os.path.exists(main_script_path):
        return jsonify({"error": f"Entrypoint '{main_file}' not found."}), 400

    # প্রসেস ইতিমধ্যে চালু আছে কিনা চেক করা
    current_proc = server_processes.get(server_id)
    if current_proc and current_proc.poll() is None:
        return jsonify({"message": "Server is already running"}), 200

    try:
        # পাইথন স্ক্রিপ্ট ব্যাকগ্রাউন্ডে রান করা
        proc = subprocess.Popen(
            [sys.executable, "-u", main_file],
            cwd=s_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        server_processes[server_id] = proc
        append_log(server_id, f"[System]: Server started (PID: {proc.pid}) with script {main_file}\n")

        # লগ রিড করার জন্য ব্যাকগ্রাউন্ড থ্রেড চালু
        t = threading.Thread(target=stream_process_output, args=(server_id, proc), daemon=True)
        t.start()

        return jsonify({"message": "Server started successfully", "pid": proc.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop/<server_id>", methods=["POST"])
def stop_server(server_id):
    proc = server_processes.get(server_id)
    if not proc or proc.poll() is not None:
        return jsonify({"message": "Server is not running"}), 200

    try:
        proc.terminate()
        append_log(server_id, "[System]: Termination signal sent...\n")
        return jsonify({"message": "Server stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/restart/<server_id>", methods=["POST"])
def restart_server(server_id):
    stop_server(server_id)
    return start_server(server_id)


@app.route("/api/clear_logs/<server_id>", methods=["POST"])
def clear_logs(server_id):
    if server_id in server_logs:
        with log_locks[server_id]:
            server_logs[server_id] = []
    return jsonify({"message": "Logs cleared"})


@app.route("/api/logs/<server_id>", methods=["GET"])
def get_logs(server_id):
    logs = "".join(server_logs.get(server_id, []))
    return jsonify({"logs": logs})


# -------------------------------------------------------------
# ২. কনসোল কমান্ড API
# -------------------------------------------------------------
@app.route("/api/command", methods=["POST"])
def execute_command():
    data = request.get_json(silent=True) or {}
    cmd = data.get("cmd", "").strip()
    server_id = data.get("server_id", "default")
    s_dir = get_server_dir(server_id)

    if not cmd:
        return jsonify({"error": "Empty command"}), 400

    append_log(server_id, f"\n$ {cmd}\n")

    # যদি মূল স্ক্রিপ্ট চলমান থাকে এবং ইনপুট দেওয়া যায়
    proc = server_processes.get(server_id)
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            return jsonify({"message": "Command piped to running process"})
        except Exception:
            pass

    # স্বতন্ত্র শেল কমান্ড এক্সিকিউট করা
    try:
        res = subprocess.run(
            cmd,
            cwd=s_dir,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15
        )
        if res.stdout:
            append_log(server_id, res.stdout)
        return jsonify({"output": res.stdout})
    except subprocess.TimeoutExpired:
        append_log(server_id, "[System Error]: Command timed out (15s limit)\n")
        return jsonify({"error": "Command timed out"}), 408
    except Exception as e:
        append_log(server_id, f"[System Error]: {str(e)}\n")
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------
# ৩. ফাইল ম্যানেজার API
# -------------------------------------------------------------
@app.route("/api/files/<server_id>", methods=["GET"])
def list_files(server_id):
    s_dir = get_server_dir(server_id)
    sub_path = request.args.get("path", "")
    
    try:
        target_dir = safe_resolve_path(s_dir, sub_path)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    if not os.path.exists(target_dir):
        return jsonify({"error": "Directory not found"}), 404

    items = []
    for item in os.listdir(target_dir):
        full_p = os.path.join(target_dir, item)
        items.append({
            "name": item,
            "is_dir": os.path.isdir(full_p)
        })

    return jsonify({"files": items})


@app.route("/api/file/<server_id>", methods=["GET", "POST", "DELETE"])
def handle_file(server_id):
    s_dir = get_server_dir(server_id)

    # GET: ফাইল কনটেন্ট পড়া
    if request.method == "GET":
        rel_path = request.args.get("path", "")
        try:
            target = safe_resolve_path(s_dir, rel_path)
            if not os.path.isfile(target):
                return jsonify({"error": "File not found"}), 404
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                return jsonify({"content": f.read()})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # POST: নতুন ফাইল তৈরি অথবা ফাইল এডিট সেভ
    elif request.method == "POST":
        data = request.get_json(silent=True) or {}
        rel_path = data.get("path", "")
        content = data.get("content", "")
        try:
            target = safe_resolve_path(s_dir, rel_path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return jsonify({"message": "File saved"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # DELETE: ফাইল অথবা ডিরেক্টরি মুছে ফেলা
    elif request.method == "DELETE":
        data = request.get_json(silent=True) or {}
        rel_path = data.get("path", "")
        try:
            target = safe_resolve_path(s_dir, rel_path)
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.isfile(target):
                os.remove(target)
            return jsonify({"message": "Deleted successfully"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/create_folder/<server_id>", methods=["POST"])
def create_folder(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(silent=True) or {}
    rel_path = data.get("path", "")
    try:
        target = safe_resolve_path(s_dir, rel_path)
        os.makedirs(target, exist_ok=True)
        return jsonify({"message": "Folder created"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rename/<server_id>", methods=["POST"])
def rename_item(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(silent=True) or {}
    old_rel = data.get("old_path", "")
    new_rel = data.get("new_path", "")
    try:
        old_target = safe_resolve_path(s_dir, old_rel)
        new_target = safe_resolve_path(s_dir, new_rel)
        os.rename(old_target, new_target)
        return jsonify({"message": "Renamed successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload/<server_id>", methods=["POST"])
def upload_files(server_id):
    s_dir = get_server_dir(server_id)
    upload_path = request.form.get("path", "")
    
    try:
        target_dir = safe_resolve_path(s_dir, upload_path)
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 403

    files = request.files.getlist("file")
    for file in files:
        if file.filename:
            # ফাইলের নাম নিরাপদ করা
            filename = os.path.basename(file.filename)
            file.save(os.path.join(target_dir, filename))

    return jsonify({"message": f"{len(files)} file(s) uploaded successfully"})


@app.route("/api/extract/<server_id>", methods=["POST"])
def extract_zip(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(silent=True) or {}
    file_rel = data.get("file_path", "")
    target_rel = data.get("target_path", "")

    try:
        zip_path = safe_resolve_path(s_dir, file_rel)
        extract_to = safe_resolve_path(s_dir, target_rel)

        if not zipfile.is_zipfile(zip_path):
            return jsonify({"error": "Invalid ZIP file"}), 400

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # ZipSlip ভালনারেবিলিটি প্রতিরোধ
            for member in zip_ref.namelist():
                member_path = os.path.abspath(os.path.join(extract_to, member))
                if not member_path.startswith(extract_to):
                    return jsonify({"error": "Malicious ZIP detected"}), 400
            zip_ref.extractall(extract_to)

        return jsonify({"message": "Extracted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------
# ৪. স্টার্টআপ কনফিগারেশন API
# -------------------------------------------------------------
@app.route("/api/get_startup/<server_id>", methods=["GET"])
def get_startup(server_id):
    s_dir = get_server_dir(server_id)
    cfg_file = os.path.join(s_dir, ".config.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception:
            pass
    return jsonify({"main_file": "main.py", "req_file": "requirements.txt"})


@app.route("/api/set_startup/<server_id>", methods=["POST"])
def set_startup(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json(silent=True) or {}
    cfg_file = os.path.join(s_dir, ".config.json")
    try:
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({
                "main_file": data.get("main_file", "main.py"),
                "req_file": data.get("req_file", "requirements.txt")
            }, f, indent=2)
        return jsonify({"message": "Startup config saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------
# সার্ভার রান
# -------------------------------------------------------------
if __name__ == "__main__":
    print("[*] Dashboard running at http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
