print(">>> GREENIDLE_SERVER.PY LOADED – MIN-SEC V5 (AUTO-PLUGINS) + CLIENT-UPDATES V1 <<<")

from flask import (
    Flask, request, jsonify, render_template_string, redirect, url_for,
    abort, send_from_directory
)
from datetime import datetime
import uuid
import os
import time
import hmac
import hashlib
from functools import wraps

app = Flask(__name__)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


# =========================
#   PATHS
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =========================
#   PLUGINS (server_plugins/)
# =========================
PLUGINS_DIR = os.path.join(BASE_DIR, "server_plugins")

@app.route("/plugins/<path:filename>")
def serve_plugin(filename):
    # anti path traversal minimal
    if ".." in filename or filename.startswith(("/", "\\")) or ":" in filename:
        abort(400)
    return send_from_directory(PLUGINS_DIR, filename, as_attachment=False)

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def list_plugins():
    items = []
    try:
        if not os.path.isdir(PLUGINS_DIR):
            return items

        for name in sorted(os.listdir(PLUGINS_DIR)):
            if not name.endswith(".py"):
                continue
            full = os.path.join(PLUGINS_DIR, name)
            if not os.path.isfile(full):
                continue

            items.append({
                "name": name,
                "bytes": os.path.getsize(full),
                "sha256": file_sha256(full),
                "url": f"/plugins/{name}"
            })
    except Exception:
        pass
    return items

@app.route("/plugins.json")
def plugins_json():
    plugins = list_plugins()
    return jsonify({"count": len(plugins), "plugins": plugins})

@app.route("/plugins")
def plugins_page():
    plugins = list_plugins()
    html = """
    <h1>GreenIdle - Plugins disponibles</h1>
    <p>Nombre: <b>{{ plugins|length }}</b></p>

    {% if plugins %}
      <table border="1" cellspacing="0" cellpadding="6">
        <tr><th>Nom</th><th>Taille</th><th>SHA256</th><th>Lien</th></tr>
        {% for p in plugins %}
          <tr>
            <td><b>{{ p.name }}</b></td>
            <td>{{ p.bytes }} bytes</td>
            <td style="font-family: monospace; font-size: 0.9em;">{{ p.sha256 }}</td>
            <td><a href="{{ p.url }}" target="_blank">Ouvrir</a></td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p>Aucun plugin trouvé dans <code>server_plugins/</code>.</p>
    {% endif %}

    <p style="margin-top:16px;">
      JSON: <a href="/plugins.json" target="_blank">/plugins.json</a>
    </p>
    """
    return render_template_string(html, plugins=plugins)

# =========================
#   CLIENT RELEASES (releases/)
# =========================
RELEASES_DIR = os.path.join(BASE_DIR, "releases")

@app.route("/releases/<path:filename>")
def serve_release(filename):
    # anti path traversal minimal
    if ".." in filename or filename.startswith(("/", "\\")) or ":" in filename:
        abort(400)

    # autorise uniquement .exe ou .msi
    lower = filename.lower()
    if not (lower.endswith(".exe") or lower.endswith(".msi")):
        abort(400)

    return send_from_directory(RELEASES_DIR, filename, as_attachment=True)

# =========================
#   CONFIG
# =========================
APP_NAME = "GreenIdle"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # Render env
BLACKLIST_IPS = set(ip.strip() for ip in os.getenv("BLACKLIST_IPS", "").split(",") if ip.strip())
DEBUG = False  # False sur Render

# =========================
#   MINI BDD EN MEMOIRE
# =========================
machines = {}         # machine_id -> dict
machine_configs = {}  # machine_id -> config dict
jobs = {}             # job_id -> dict
tasks = {}            # task_id -> dict
results = []          # list[dict]
tasks_log = []        # list[dict]

# Auth clients minimal
clients = {}            # client_id -> {"machine_key": "...", "created_at": "..."}
machine_to_client = {}  # machine_id -> client_id

# =========================
#   UPDATE STATE (in-memory)
# =========================
update_state = {
    "latest_version": "1.0.0",
    "download_filename": "",   # ex: "GreenIdleClient-1.0.1.exe"
    "sha256": "",              # sha256 of the exe
    "update_allowed": False,   # kill-switch for 2B (manual allow)
    "rollout_percent": 0,      # 0..100
    "notes": ""
}

# =========================
#   UTILS
# =========================
def now_iso():
    return datetime.utcnow().isoformat()

def get_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"

_RATE = {}  # {key: [timestamps]}
def rate_limit(key: str, limit=30, window=60):
    now = time.time()
    lst = _RATE.get(key, [])
    lst = [t for t in lst if now - t < window]
    if len(lst) >= limit:
        abort(429)
    lst.append(now)
    _RATE[key] = lst

def is_blacklisted():
    return get_ip() in BLACKLIST_IPS

def is_admin():
    if not ADMIN_TOKEN:
        return False
    if request.headers.get("X-Admin-Token", "") == ADMIN_TOKEN:
        return True
    return request.args.get("token", "") == ADMIN_TOKEN

def require_admin_route(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return "Accès refusé", 403
        return f(*args, **kwargs)
    return wrapper

# =========================
#   Client auth minimal (HMAC)
# =========================
def _hmac_hex(key: str, body_bytes: bytes) -> str:
    return hmac.new(key.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

def verify_client_if_present(machine_id: str = None):
    if is_blacklisted():
        abort(403)

    client_id = request.headers.get("X-Client-Id", "").strip()
    sig = request.headers.get("X-Client-Signature", "").strip()

    # legacy (non signé) accepté mais rate limité
    if not client_id and not sig:
        rate_limit(f"legacy:{get_ip()}", limit=120, window=60)
        return {"mode": "legacy", "client_id": None}

    if not client_id or not sig:
        abort(401)

    c = clients.get(client_id)
    if not c:
        abort(401)

    expected = _hmac_hex(c["machine_key"], request.data or b"")
    if not hmac.compare_digest(expected, sig):
        abort(401)

    rate_limit(f"client:{client_id}", limit=240, window=60)

    if machine_id:
        machine_to_client[machine_id] = client_id

    return {"mode": "signed", "client_id": client_id}

def ensure_machine(machine_id, display_name=None):
    if not machine_id:
        return None

    if machine_id not in machines:
        machines[machine_id] = {
            "machine_id": machine_id,
            "display_name": display_name or machine_id,
            "registered_at": now_iso(),
            "last_seen": None,
            "total_seconds": 0,
            "last_cpu": 0.0,
        }
    else:
        if display_name:
            machines[machine_id]["display_name"] = display_name

    return machines[machine_id]

def default_config():
    return {
        "enabled": True,
        "cpu_pause_threshold": 50.0,
        "heartbeat_every": 15,
        "idle_sleep_seconds": 2,
        "task_max_seconds": 30,
        "post_task_sleep_seconds": 2,

        # ✅ plugins requis (auto-download côté client)
        "plugins_required": ["montecarlo"],

        "night_mode": {
            "enabled": False,
            "start_hour": 23,
            "end_hour": 7,
            "cpu_pause_threshold": 70.0
        }
    }

def ensure_config(machine_id: str):
    cfg = machine_configs.get(machine_id)
    if not cfg:
        cfg = default_config()
        machine_configs[machine_id] = cfg

    # sécurité : s'assurer que la clé existe toujours
    if "plugins_required" not in cfg or not isinstance(cfg.get("plugins_required"), list):
        cfg["plugins_required"] = ["montecarlo"]

    return cfg

# =========================
#   API CLIENTS
# =========================
@app.route("/register", methods=["POST"])
def register():
    if is_blacklisted():
        return jsonify({"error": "blacklisted"}), 403

    rate_limit(f"register:{get_ip()}", limit=10, window=60)

    data = request.json or {}
    machine_id = data.get("machine_id")
    client_name = data.get("client_name")

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    provided_client_id = (data.get("client_id") or "").strip()
    provided_machine_key = (data.get("machine_key") or "").strip()

    if provided_client_id and provided_machine_key:
        clients[provided_client_id] = {"machine_key": provided_machine_key, "created_at": now_iso()}
        machine_to_client[machine_id] = provided_client_id
        auth_mode = "signed-ready"
        returned_client_id = provided_client_id
        returned_machine_key = None
    else:
        generated_client_id = str(uuid.uuid4())
        generated_machine_key = str(uuid.uuid4()) + str(uuid.uuid4())
        clients[generated_client_id] = {"machine_key": generated_machine_key, "created_at": now_iso()}
        machine_to_client[machine_id] = generated_client_id
        auth_mode = "generated"
        returned_client_id = generated_client_id
        returned_machine_key = generated_machine_key

    m = ensure_machine(machine_id, client_name)
    m["last_seen"] = now_iso()
    ensure_config(machine_id)

    return jsonify({
        "status": "ok",
        "message": "machine enregistree",
        "auth": {
            "mode": auth_mode,
            "client_id": returned_client_id,
            "machine_key": returned_machine_key,
            "how_to_sign": "HMAC_SHA256(machine_key, raw_request_body) -> X-Client-Signature",
            "headers": ["X-Client-Id", "X-Client-Signature"]
        }
    })

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json or {}
    machine_id = data.get("machine_id")
    cpu = float(data.get("cpu_percent", 0.0))

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    verify_client_if_present(machine_id)

    m = ensure_machine(machine_id)
    m["last_seen"] = now_iso()
    m["last_cpu"] = cpu
    ensure_config(machine_id)
    return jsonify({"status": "ok"})

@app.route("/config", methods=["GET"])
def get_config():
    machine_id = request.args.get("machine_id")
    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    verify_client_if_present(machine_id)
    ensure_machine(machine_id)
    return jsonify(ensure_config(machine_id))

@app.route("/task", methods=["GET"])
def get_task():
    machine_id = request.args.get("machine_id")
    if not machine_id:
        return ("", 400)

    verify_client_if_present(machine_id)

    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)

    if not cfg.get("enabled", True):
        return ("", 204)

    for t in tasks.values():
        if t["status"] == "pending":
            t["status"] = "assigned"
            t["assigned_to"] = machine_id
            t["updated_at"] = now_iso()

            job = jobs.get(t["job_id"])
            if job and job["status"] == "pending":
                job["status"] = "running"

            return jsonify({
                "task_id": t["task_id"],
                "payload": t["task_type"],
                "params": t.get("params", {}),
                "size": t.get("size", 0),
                "task_max_seconds": cfg.get("task_max_seconds", 30),
                "post_task_sleep_seconds": cfg.get("post_task_sleep_seconds", 2),
            })

    return ("", 204)

@app.route("/report", methods=["POST"])
def report():
    data = request.json or {}
    machine_id = data.get("machine_id")
    task_id = data.get("task_id")
    seconds = int(data.get("seconds", 0))
    result = data.get("result")

    if not machine_id or task_id is None:
        return jsonify({"error": "machine_id ou task_id manquant"}), 400

    verify_client_if_present(machine_id)

    m = ensure_machine(machine_id)
    m["total_seconds"] += seconds
    m["last_seen"] = now_iso()
    ensure_config(machine_id)

    tasks_log.append({
        "machine_id": machine_id,
        "task_id": task_id,
        "seconds": seconds,
        "result": result,
        "reported_at": now_iso()
    })

    if task_id in tasks:
        t = tasks[task_id]
        t["status"] = "done"
        t["seconds"] = t.get("seconds", 0) + seconds
        t["result"] = result
        t["updated_at"] = now_iso()

        job = jobs.get(t["job_id"])
        if job:
            job["total_seconds"] += seconds

            all_done = all(
                (tt["status"] == "done")
                for tt in tasks.values()
                if tt["job_id"] == job["job_id"]
            )
            if all_done:
                job["status"] = "done"

            results.append({
                "job_id": job["job_id"],
                "task_id": task_id,
                "machine_id": machine_id,
                "seconds": seconds,
                "timestamp": now_iso(),
                "result": result
            })
    else:
        results.append({
            "job_id": None,
            "task_id": task_id,
            "machine_id": machine_id,
            "seconds": seconds,
            "timestamp": now_iso(),
            "result": result
        })

    return jsonify({"status": "ok"})

@app.route("/status", methods=["GET"])
def status():
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    return jsonify({
        "app": APP_NAME,
        "machines_count": len(machines),
        "total_hours": round(total_seconds / 3600, 4),
        "jobs_count": len(jobs),
        "machines": list(machines.values()),
    })

# =========================
#   VERSION (CLIENT UPDATE 2B) — URL ABSOLUE
# =========================
@app.route("/version", methods=["GET"])
def version():
    machine_id = request.args.get("machine_id")
    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    verify_client_if_present(machine_id)
    ensure_machine(machine_id)
    ensure_config(machine_id)

    download_filename = (update_state.get("download_filename") or "").strip()

    # ✅ URL absolue (ex: https://xxx.onrender.com/releases/GreenIdleClient-1.0.1.exe)
    download_url = ""
    if download_filename:
        base = request.host_url.rstrip("/")  # inclut scheme + host
        download_url = f"{base}/releases/{download_filename}"

    payload = {
        "latest_version": update_state.get("latest_version", "1.0.0"),
        "download_url": download_url,
        "sha256": update_state.get("sha256", ""),
        "mandatory": False,
        "update_allowed": bool(update_state.get("update_allowed", False)),
        "rollout": {"percent": int(update_state.get("rollout_percent", 0))},
        "notes": update_state.get("notes", ""),
        "min_client_version": "1.0.0",
    }
    return jsonify(payload)


# =========================
#   ADMIN: RENAME + CONFIG + STOP/START + UPDATE
# =========================
@app.route("/machines/<machine_id>/rename", methods=["POST"])
@require_admin_route
def rename_machine(machine_id):
    if machine_id not in machines:
        return "Machine inconnue", 404

    new_name = request.form.get("display_name") or (request.json or {}).get("display_name")
    if not new_name:
        return "Nom manquant", 400

    machines[machine_id]["display_name"] = new_name
    return redirect(url_for("dashboard", token=request.args.get("token")))

@app.route("/machines/<machine_id>/config", methods=["POST"])
@require_admin_route
def set_machine_config(machine_id):
    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)

    data = request.form or request.json or {}

    # enabled : checkbox HTML -> présent = True, absent = False
    if request.form is not None and request.form != {}:
        cfg["enabled"] = ("enabled" in data)
    else:
        # si JSON (API) : true/false direct
        if "enabled" in data:
            cfg["enabled"] = bool(data.get("enabled"))

    cfg["cpu_pause_threshold"] = float(data.get("cpu_pause_threshold", cfg.get("cpu_pause_threshold", 50.0)))
    cfg["task_max_seconds"] = int(data.get("task_max_seconds", cfg.get("task_max_seconds", 30)))
    cfg["post_task_sleep_seconds"] = int(data.get("post_task_sleep_seconds", cfg.get("post_task_sleep_seconds", 2)))

    # ✅ plugins requis (string "a,b,c" depuis dashboard)
    raw = (data.get("plugins_required", "") or "").strip()
    if raw:
        cfg["plugins_required"] = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        cfg["plugins_required"] = cfg.get("plugins_required") or ["montecarlo"]

    nm = cfg.get("night_mode") or {}
    cfg["night_mode"] = {
        "enabled": ("night_enabled" in data) if (request.form is not None and request.form != {}) else bool(data.get("night_mode", {}).get("enabled", nm.get("enabled", False))),
        "start_hour": int(data.get("night_start", nm.get("start_hour", 23))),
        "end_hour": int(data.get("night_end", nm.get("end_hour", 7))),
        "cpu_pause_threshold": float(data.get("night_cpu", nm.get("cpu_pause_threshold", 70.0))),
    }

    machine_configs[machine_id] = cfg
    return redirect(url_for("dashboard", token=request.args.get("token")))

@app.route("/machines/<machine_id>/stop", methods=["POST"])
@require_admin_route
def stop_machine(machine_id):
    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)
    cfg["enabled"] = False
    machine_configs[machine_id] = cfg
    return redirect(url_for("dashboard", token=request.args.get("token")))

@app.route("/machines/<machine_id>/start", methods=["POST"])
@require_admin_route
def start_machine(machine_id):
    ensure_machine(machine_id)
    cfg = ensure_config(machine_id)
    cfg["enabled"] = True
    machine_configs[machine_id] = cfg
    return redirect(url_for("dashboard", token=request.args.get("token")))

# --- ADMIN UPDATE SETTINGS ---
@app.route("/admin/update", methods=["POST"])
@require_admin_route
def admin_update_settings():
    data = request.form or request.json or {}

    latest_version = (data.get("latest_version") or "").strip()
    download_filename = (data.get("download_filename") or "").strip()
    sha256_hex = (data.get("sha256") or "").strip().lower()
    notes = (data.get("notes") or "").strip()

    # checkbox HTML
    if request.form is not None and request.form != {}:
        update_allowed = ("update_allowed" in data)
    else:
        update_allowed = bool(data.get("update_allowed", False))

    try:
        rollout_percent = int(data.get("rollout_percent", update_state.get("rollout_percent", 0)))
    except Exception:
        rollout_percent = int(update_state.get("rollout_percent", 0))

    rollout_percent = max(0, min(100, rollout_percent))

    # validations simples
    if latest_version:
        update_state["latest_version"] = latest_version
    update_state["download_filename"] = download_filename
    update_state["sha256"] = sha256_hex
    update_state["update_allowed"] = bool(update_allowed)
    update_state["rollout_percent"] = int(rollout_percent)
    update_state["notes"] = notes

    return redirect(url_for("dashboard", token=request.args.get("token")))

# =========================
#   JOBS (ADMIN) — MONTECARLO UNIQUEMENT
# =========================
@app.route("/submit", methods=["GET", "POST"])
@require_admin_route
def submit_job():
    token = request.args.get("token")

    if request.method == "POST":
        name = request.form.get("name", "Job sans nom")
        description = request.form.get("description", "")
        task_type = "montecarlo"  # verrouillé
        total_chunks = int(request.form.get("chunks", 5))
        size = int(request.form.get("size", 200000))

        job_id = str(uuid.uuid4())[:8]
        jobs[job_id] = {
            "job_id": job_id,
            "name": name,
            "description": description,
            "task_type": task_type,
            "total_chunks": total_chunks,
            "created_at": now_iso(),
            "status": "pending",
            "total_seconds": 0
        }

        for i in range(total_chunks):
            task_id = f"{job_id}_part_{i+1}"
            params = {"n": size, "seed": i + 1}

            tasks[task_id] = {
                "task_id": task_id,
                "job_id": job_id,
                "task_type": task_type,
                "size": size,
                "params": params,
                "status": "pending",
                "assigned_to": None,
                "created_at": now_iso(),
                "updated_at": None,
                "seconds": 0,
                "result": None
            }

        return redirect(url_for("jobs_view", token=token))

    html = """
    <h1>Soumettre un job GreenIdle</h1>
    <p><a href="/dashboard?token={{ token }}">⬅ Retour dashboard</a></p>

    <form method="post">
        Nom du job :<br>
        <input name="name" type="text" value="Estimation de PI"><br><br>

        Description :<br>
        <textarea name="description" rows="3" cols="60">Test calcul distribué</textarea><br><br>

        Type de tâche : <b>montecarlo</b> (unique)<br><br>

        Chunks :<br>
        <input name="chunks" type="number" value="5" min="1" max="200"><br><br>

        Taille (n) :<br>
        <input name="size" type="number" value="200000" min="1000"><br><br>

        <button type="submit">Créer le job</button>
    </form>
    """
    return render_template_string(html, token=token)

@app.route("/jobs")
@require_admin_route
def jobs_view():
    token = request.args.get("token")
    html = """
    <h1>Jobs GreenIdle</h1>
    <p>
      <a href="/dashboard?token={{ token }}">⬅ Dashboard</a> |
      <a href="/submit?token={{ token }}">➕ Nouveau job</a>
    </p>

    {% if jobs %}
    <table border="1" cellspacing="0" cellpadding="6">
      <tr>
        <th>ID</th><th>Nom</th><th>Type</th><th>Status</th><th>Chunks</th><th>Secondes</th><th>Créé le</th><th>Détail</th>
      </tr>
      {% for j in jobs %}
      <tr>
        <td>{{ j.job_id }}</td>
        <td>{{ j.name }}</td>
        <td>{{ j.task_type }}</td>
        <td>{{ j.status }}</td>
        <td>{{ j.total_chunks }}</td>
        <td>{{ j.total_seconds }}</td>
        <td>{{ j.created_at }}</td>
        <td><a href="/jobs/{{ j.job_id }}?token={{ token }}">Voir</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
      <p>Aucun job (après redeploy Render, la mémoire repart à zéro). Clique sur “Nouveau job”.</p>
    {% endif %}
    """
    return render_template_string(html, jobs=list(jobs.values()), token=token)

def aggregate_job_result(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return None
    if job.get("task_type") != "montecarlo":
        return None

    inside_sum = 0
    total_sum = 0
    for t in tasks.values():
        if t["job_id"] == job_id and t.get("result"):
            r = t["result"] or {}
            inside_sum += int(r.get("inside", 0))
            total_sum += int(r.get("total", 0))

    if total_sum <= 0:
        return {"pi": None, "inside": inside_sum, "total": total_sum}

    pi_est = 4.0 * inside_sum / float(total_sum)
    return {"pi": pi_est, "inside": inside_sum, "total": total_sum}

@app.route("/jobs/<job_id>")
@require_admin_route
def job_detail(job_id):
    token = request.args.get("token")
    job = jobs.get(job_id)
    if not job:
        return "Job introuvable", 404

    job_tasks = [t for t in tasks.values() if t["job_id"] == job_id]
    agg = aggregate_job_result(job_id)

    html = """
    <h1>Job {{ job.job_id }}</h1>
    <p><a href="/jobs?token={{ token }}">⬅ Retour</a></p>

    <p><b>Nom:</b> {{ job.name }}</p>
    <p><b>Type:</b> {{ job.task_type }}</p>
    <p><b>Status:</b> {{ job.status }}</p>
    <p><b>Secondes:</b> {{ job.total_seconds }}</p>

    {% if agg %}
      <h2>Résultat agrégé</h2>
      <p><b>PI estimé:</b> {{ agg.pi }}</p>
      <p>inside={{ agg.inside }} / total={{ agg.total }}</p>
    {% endif %}

    <h2>Tâches</h2>
    <table border="1" cellspacing="0" cellpadding="6">
      <tr><th>Task</th><th>Status</th><th>Assignée à</th><th>Secondes</th><th>Result</th></tr>
      {% for t in job_tasks %}
        <tr>
          <td>{{ t.task_id }}</td>
          <td>{{ t.status }}</td>
          <td>{{ t.assigned_to }}</td>
          <td>{{ t.seconds }}</td>
          <td><pre style="margin:0; white-space:pre-wrap;">{{ t.result }}</pre></td>
        </tr>
      {% endfor %}
    </table>
    """
    return render_template_string(html, job=job, job_tasks=job_tasks, token=token, agg=agg)

@app.route("/results")
@require_admin_route
def results_view():
    token = request.args.get("token")
    html = """
    <h1>Résultats</h1>
    <p><a href="/dashboard?token={{ token }}">⬅ Dashboard</a></p>

    {% if rows %}
    <table border="1" cellspacing="0" cellpadding="6">
      <tr><th>Job</th><th>Task</th><th>Machine</th><th>Secondes</th><th>Date</th><th>Result</th></tr>
      {% for r in rows %}
        <tr>
          <td>{{ r.get('job_id') }}</td>
          <td>{{ r.get('task_id') }}</td>
          <td>{{ r.get('machine_id') }}</td>
          <td>{{ r.get('seconds') }}</td>
          <td>{{ r.get('timestamp') }}</td>
          <td><pre style="margin:0; white-space:pre-wrap;">{{ r.get('result') }}</pre></td>
        </tr>
      {% endfor %}
    </table>
    {% else %}
      <p>Aucun résultat.</p>
    {% endif %}
    """
    return render_template_string(html, rows=results, token=token)

# =========================
#   DASHBOARD (ADMIN)
# =========================
@app.route("/dashboard")
@require_admin_route
def dashboard():
    token = request.args.get("token")
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    total_hours = round(total_seconds / 3600, 4)

    html = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{{ app_name }} - Dashboard</title>
        <style>
            :root[data-theme="light"]{
              --bg:#f7f7f8; --card:#ffffff; --text:#111827; --muted:#6b7280; --line:#e5e7eb;
              --pillOn:#e7f7ee; --pillIdle:#fff7e6; --pillOff:#f3f4f6;
              --btn:#ffffff;
            }
            :root[data-theme="dark"]{
              --bg:#0b1220; --card:#0f1a2c; --text:#e5e7eb; --muted:#93a4bf; --line:#1f2a3d;
              --pillOn:#0f2b1b; --pillIdle:#2a2416; --pillOff:#1b2233;
              --btn:#0f1a2c;
            }
            body{background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,Segoe UI,Roboto; margin:0;}
            .page{max-width:1200px; margin:0 auto; padding:22px;}
            .topbar{display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:18px;}
            h1{margin:0; font-size:20px;}
            .sub{color:var(--muted); font-size:13px; margin-top:4px;}
            .top-actions{display:flex; align-items:center; gap:10px;}
            .hint{color:var(--muted); font-size:13px;}

            .kpis{display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:14px;}
            .kpi{background:var(--card); border:1px solid var(--line); border-radius:14px; padding:12px;}
            .kpi-title{color:var(--muted); font-size:12px;}
            .kpi-value{font-size:18px; font-weight:650; margin-top:6px;}

            .nav{display:flex; flex-wrap:wrap; gap:10px; margin:10px 0 12px;}
            .chip{display:inline-flex; align-items:center; gap:8px; background:var(--card); border:1px solid var(--line);
                 border-radius:999px; padding:8px 12px; color:var(--text); text-decoration:none; font-size:13px;}
            .chip:hover{filter:brightness(1.05);}

            .controls{display:flex; gap:10px; margin:12px 0 12px;}
            .search, select{background:var(--card); color:var(--text); border:1px solid var(--line);
                           border-radius:12px; padding:10px 12px;}
            .search{flex:1;}

            .grid{display:grid; grid-template-columns:repeat(12,1fr); gap:12px;}
            .card{grid-column: span 12; background:var(--card); border:1px solid var(--line); border-radius:16px; overflow:hidden;}
            .card-h{display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border-bottom:1px solid var(--line);}
            .card-h .title{font-weight:650;}
            .card-b{padding:14px;}

            table{width:100%; border-collapse:collapse;}
            thead th{color:var(--muted); text-align:left; font-size:12px; font-weight:600; padding:12px; border-bottom:1px solid var(--line);}
            tbody td{padding:12px; border-bottom:1px solid var(--line); vertical-align:top;}
            tbody tr:hover{filter:brightness(1.05);}

            .machineName{font-weight:650;}
            .machineId{color:var(--muted); font-size:12px; margin-top:2px;}

            .pill{display:inline-block; padding:6px 10px; border-radius:999px; font-size:12px; border:1px solid var(--line);}
            .pill.on{background:var(--pillOn);}
            .pill.idle{background:var(--pillIdle);}
            .pill.off{background:var(--pillOff);}

            .btn{background:var(--btn); color:var(--text); border:1px solid var(--line); border-radius:10px; padding:8px 10px; cursor:pointer;}
            .btn:hover{filter:brightness(1.08);}
            .btn.subtle{opacity:.85;}
            .btnrow{display:flex; gap:8px; justify-content:flex-end; flex-wrap:wrap;}

            .formgrid{display:grid; grid-template-columns:repeat(12,1fr); gap:10px;}
            .f{grid-column: span 6;}
            .f label{display:block; color:var(--muted); font-size:12px; margin-bottom:6px;}
            .f input[type="text"], .f input[type="number"]{
              width:100%; background:var(--card); color:var(--text); border:1px solid var(--line);
              border-radius:10px; padding:9px 10px;
            }
            .fsmall{grid-column: span 4;}
            .line{height:1px; background:var(--line); margin:12px 0;}
            .check{display:flex; gap:10px; align-items:center; color:var(--text); margin:2px 0 10px;}
            .check span{color:var(--muted); font-size:13px;}

            .switch{position:relative; display:inline-block; width:44px; height:24px;}
            .switch input{display:none;}
            .slider{position:absolute; inset:0; background:var(--card); border:1px solid var(--line); border-radius:999px;}
            .slider:before{content:""; position:absolute; height:18px; width:18px; left:3px; top:2.5px; background:var(--text);
                          border-radius:50%; transition:.2s; opacity:.8;}
            .switch input:checked + .slider:before{transform:translateX(20px);}

            @media (max-width: 980px){
              .kpis{grid-template-columns:repeat(2,1fr);}
              thead{display:none;}
              table, tbody, tr, td{display:block; width:100%;}
              tbody td{border-bottom:none;}
              tbody tr{border-bottom:1px solid var(--line); padding:10px;}
              .f, .fsmall{grid-column: span 12;}
            }
        </style>
    </head>
    <body>
      <div class="page">
        <header class="topbar">
          <div>
            <h1>{{ app_name }} — Dashboard</h1>
            <div class="sub">Machines, capacité et configuration (admin)</div>
          </div>

          <div class="top-actions">
            <label class="switch" title="Mode nuit interface">
              <input id="darkToggle" type="checkbox">
              <span class="slider"></span>
            </label>
            <span class="hint">Nuit</span>
          </div>
        </header>

        <section class="kpis">
          <div class="kpi">
            <div class="kpi-title">Machines</div>
            <div class="kpi-value">{{ machines|length }}</div>
          </div>
          <div class="kpi">
            <div class="kpi-title">Heures cumulées</div>
            <div class="kpi-value">{{ total_hours }}</div>
          </div>
          <div class="kpi">
            <div class="kpi-title">Jobs</div>
            <div class="kpi-value">{{ jobs_count }}</div>
          </div>
          <div class="kpi">
            <div class="kpi-title">En ligne (estim.)</div>
            <div class="kpi-value" id="kpiOnline">—</div>
          </div>
          <div class="kpi">
            <div class="kpi-title">CPU moyen (estim.)</div>
            <div class="kpi-value" id="kpiCpu">—</div>
          </div>
        </section>

        <nav class="nav">
          <a class="chip" href="/submit?token={{ token }}">➕ Nouveau job</a>
          <a class="chip" href="/jobs?token={{ token }}">📦 Jobs</a>
          <a class="chip" href="/results?token={{ token }}">📊 Résultats</a>
          <a class="chip" href="/plugins" target="_blank">🧩 Plugins</a>
          <a class="chip" href="/status" target="_blank">🔎 API /status</a>
        </nav>

        <!-- ===== UPDATE SECTION ===== -->
        <section class="card" style="margin-bottom:12px;">
          <div class="card-h">
            <div class="title">Mises à jour client (2B)</div>
            <div class="hint">⚠️ Mémoire volatile (reset au redeploy)</div>
          </div>
          <div class="card-b">
            <form method="post" action="/admin/update?token={{ token }}">
              <div class="formgrid">
                <div class="fsmall">
                  <label>Latest version</label>
                  <input type="text" name="latest_version" value="{{ update.latest_version }}">
                </div>
                <div class="f">
                  <label>Fichier (.exe) dans releases/</label>
                  <input type="text" name="download_filename" placeholder="GreenIdleClient-1.0.1.exe" value="{{ update.download_filename }}">
                </div>
                <div class="f">
                  <label>SHA256 de l'exe</label>
                  <input type="text" name="sha256" placeholder="hex sha256…" value="{{ update.sha256 }}">
                </div>
                <div class="fsmall">
                  <label>Rollout % (0-100)</label>
                  <input type="number" name="rollout_percent" min="0" max="100" value="{{ update.rollout_percent }}">
                </div>
                <div class="f">
                  <label>Notes</label>
                  <input type="text" name="notes" value="{{ update.notes }}">
                </div>
              </div>

              <div class="line"></div>

              <div class="check">
                <input type="checkbox" name="update_allowed" {% if update.update_allowed %}checked{% endif %}>
                <span>Autoriser update (kill-switch global). Si décoché, seul le rollout % peut déclencher.</span>
              </div>

              <button class="btn" type="submit">Enregistrer update</button>
              <span class="hint" style="margin-left:10px;">
                Endpoint client: <code>/version</code> — download: <code>/releases/&lt;file&gt;</code>
              </span>
            </form>
          </div>
        </section>

        <section class="controls">
          <input id="search" class="search" placeholder="Rechercher une machine (nom, id)…" />
          <select id="sort">
            <option value="last_seen">Trier: Dernière activité</option>
            <option value="last_cpu">Trier: CPU (faible → fort)</option>
            <option value="total_seconds">Trier: Secondes cumulées</option>
            <option value="name">Trier: Nom</option>
          </select>
        </section>

        <section class="card">
          <div class="card-h">
            <div class="title">Machines</div>
            <div class="hint">Statut: Offline si &gt; 3 min sans heartbeat</div>
          </div>

          <div class="card-b" style="padding:0;">
            <table>
              <thead>
                <tr>
                  <th>Machine</th>
                  <th>Statut</th>
                  <th>CPU</th>
                  <th>Dernière vue</th>
                  <th>Secondes</th>
                  <th style="text-align:right;">Actions</th>
                </tr>
              </thead>
              <tbody id="rows">
              {% for m in machines %}
                {% set cfg = configs.get(m.machine_id, {}) %}
                {% set nm = cfg.get("night_mode", {}) %}
                <tr class="row"
                    data-name="{{ (m.display_name or '')|lower }}"
                    data-id="{{ (m.machine_id or '')|lower }}"
                    data-lastseen="{{ m.last_seen or '' }}"
                    data-cpu="{{ m.last_cpu or 0 }}"
                    data-seconds="{{ m.total_seconds or 0 }}">
                  <td>
                    <div class="machineName">{{ m.display_name }}</div>
                    <div class="machineId">{{ m.machine_id }} — plugins_required: {{ (cfg.get('plugins_required') or ['montecarlo'])|join(',') }}</div>
                  </td>

                  <td>
                    <span class="pill off" data-pill>—</span>
                  </td>

                  <td>{{ m.last_cpu }}%</td>

                  <td>
                    <span data-ago>—</span><br>
                    <span class="hint" style="font-size:12px;">{{ m.last_seen }}</span>
                  </td>

                  <td>{{ m.total_seconds }}</td>

                  <td>
                    <div class="btnrow">
                      {% if cfg.get("enabled", True) %}
                        <form method="post" action="/machines/{{ m.machine_id }}/stop?token={{ token }}">
                          <button class="btn subtle" type="submit">Pause</button>
                        </form>
                      {% else %}
                        <form method="post" action="/machines/{{ m.machine_id }}/start?token={{ token }}">
                          <button class="btn" type="submit">Reprendre</button>
                        </form>
                      {% endif %}

                      <button class="btn" type="button" onclick="toggleCfg('{{ m.machine_id }}')">Configurer</button>
                    </div>
                  </td>
                </tr>

                <tr id="cfg-{{ m.machine_id }}" style="display:none;">
                  <td colspan="6">
                    <div class="card" style="margin:10px 0;">
                      <div class="card-h">
                        <div class="title">Configuration — {{ m.display_name }}</div>
                        <div class="hint">{{ m.machine_id }}</div>
                      </div>
                      <div class="card-b">
                        <form method="post" action="/machines/{{ m.machine_id }}/config?token={{ token }}">
                          <div class="check">
                            <input type="checkbox" name="enabled" {% if cfg.get("enabled", True) %}checked{% endif %}>
                            <span>Machine active</span>
                          </div>

                          <div class="formgrid">
                            <div class="fsmall">
                              <label>CPU max (%)</label>
                              <input type="number" name="cpu_pause_threshold"
                                     value="{{ cfg.get('cpu_pause_threshold',50) }}"
                                     min="10" max="95" step="5">
                            </div>

                            <div class="fsmall">
                              <label>Durée max tâche (s)</label>
                              <input type="number" name="task_max_seconds"
                                     value="{{ cfg.get('task_max_seconds',30) }}"
                                     min="5" max="300">
                            </div>

                            <div class="fsmall">
                              <label>Pause après tâche (s)</label>
                              <input type="number" name="post_task_sleep_seconds"
                                     value="{{ cfg.get('post_task_sleep_seconds',2) }}"
                                     min="0" max="30">
                            </div>

                            <div class="f">
                              <label>Plugins requis (séparés par virgules)</label>
                              <input type="text" name="plugins_required"
                                     value="{{ (cfg.get('plugins_required') or ['montecarlo'])|join(',') }}">
                            </div>
                          </div>

                          <div class="line"></div>

                          <div class="check">
                            <input type="checkbox" name="night_enabled" {% if nm.get("enabled") %}checked{% endif %}>
                            <span>Mode nuit machine</span>
                          </div>

                          <div class="formgrid">
                            <div class="fsmall">
                              <label>Nuit début (0-23)</label>
                              <input type="number" name="night_start"
                                     value="{{ nm.get('start_hour',23) }}"
                                     min="0" max="23">
                            </div>
                            <div class="fsmall">
                              <label>Nuit fin (0-23)</label>
                              <input type="number" name="night_end"
                                     value="{{ nm.get('end_hour',7) }}"
                                     min="0" max="23">
                            </div>
                            <div class="fsmall">
                              <label>CPU nuit (%)</label>
                              <input type="number" name="night_cpu"
                                     value="{{ nm.get('cpu_pause_threshold',70) }}"
                                     min="20" max="100" step="5">
                            </div>
                          </div>

                          <div class="line"></div>

                          <div class="formgrid">
                            <div class="f">
                              <label>Renommer</label>
                              <div style="display:flex; gap:8px;">
                                <input type="text" name="display_name" placeholder="Nouveau nom" form="rn-{{ m.machine_id }}" style="flex:1;">
                                <button class="btn" type="submit">Appliquer config</button>
                              </div>
                            </div>
                          </div>
                        </form>

                        <form id="rn-{{ m.machine_id }}" method="post" action="/machines/{{ m.machine_id }}/rename?token={{ token }}" style="display:none;"></form>
                      </div>
                    </div>
                  </td>
                </tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <script>
        // Theme
        const saved = localStorage.getItem("theme") || "dark";
        document.documentElement.dataset.theme = saved;
        const darkToggle = document.getElementById("darkToggle");
        darkToggle.checked = (saved === "dark");
        darkToggle.addEventListener("change", () => {
          const t = darkToggle.checked ? "dark" : "light";
          document.documentElement.dataset.theme = t;
          localStorage.setItem("theme", t);
        });

        function timeAgo(iso) {
          if (!iso) return "—";
          const t = new Date(iso).getTime();
          const s = Math.floor((Date.now() - t) / 1000);
          if (s < 60) return `${s}s`;
          const m = Math.floor(s/60);
          if (m < 60) return `${m}min`;
          const h = Math.floor(m/60);
          if (h < 24) return `${h}h`;
          const d = Math.floor(h/24);
          return `${d}j`;
        }

        function computeStatus(lastSeenIso, cpu) {
          if (!lastSeenIso) return {label:"Offline", cls:"off", age: 999999};
          const ageSec = (Date.now() - new Date(lastSeenIso).getTime()) / 1000;
          if (ageSec > 180) return {label:"Offline", cls:"off", age: ageSec};
          if (Number(cpu) <= 10) return {label:"Idle", cls:"idle", age: ageSec};
          return {label:"Online", cls:"on", age: ageSec};
        }

        function refreshDerived() {
          const rows = Array.from(document.querySelectorAll("tr.row"));
          let online = 0, cpuSum = 0, cpuCount = 0;

          rows.forEach(r => {
            const lastSeen = r.dataset.lastseen;
            const cpu = Number(r.dataset.cpu || 0);
            const st = computeStatus(lastSeen, cpu);

            const pill = r.querySelector("[data-pill]");
            pill.textContent = st.label;
            pill.classList.remove("on","idle","off");
            pill.classList.add(st.cls);

            const ago = r.querySelector("[data-ago]");
            ago.textContent = timeAgo(lastSeen);

            if (st.cls !== "off") online += 1;
            cpuSum += cpu; cpuCount += 1;
          });

          document.getElementById("kpiOnline").textContent = online;
          document.getElementById("kpiCpu").textContent = cpuCount ? (cpuSum/cpuCount).toFixed(1) + "%" : "—";
        }

        function toggleCfg(machineId) {
          const el = document.getElementById("cfg-" + machineId);
          if (!el) return;
          el.style.display = (el.style.display === "none") ? "" : "none";
        }

        // search + sort (client-side)
        const search = document.getElementById("search");
        const sort = document.getElementById("sort");
        function applyFilterSort(){
          const q = (search.value || "").toLowerCase().trim();
          const rows = Array.from(document.querySelectorAll("tr.row"));

          rows.forEach(r => {
            const name = r.dataset.name || "";
            const id = r.dataset.id || "";
            const show = !q || name.includes(q) || id.includes(q);
            r.style.display = show ? "" : "none";
            // cache aussi la ligne config associée
            const cfg = document.getElementById("cfg-" + (r.children[0].querySelector(".machineId").textContent.split("—")[0].trim()));
            if (cfg && !show) cfg.style.display = "none";
          });

          const key = sort.value;
          const parent = document.getElementById("rows");

          const sortable = rows
            .filter(r => r.style.display !== "none")
            .map(r => {
              const mid = r.children[0].querySelector(".machineId").textContent.split("—")[0].trim();
              const cfg = document.getElementById("cfg-" + mid);
              return {r, cfg};
            });

          sortable.sort((a,b) => {
            if (key === "name") return (a.r.dataset.name||"").localeCompare(b.r.dataset.name||"");
            if (key === "last_cpu") return Number(a.r.dataset.cpu||0) - Number(b.r.dataset.cpu||0);
            if (key === "total_seconds") return Number(b.r.dataset.seconds||0) - Number(a.r.dataset.seconds||0);
            // last_seen desc
            return new Date(b.r.dataset.lastseen||0).getTime() - new Date(a.r.dataset.lastseen||0).getTime();
          });

          sortable.forEach(({r,cfg}) => {
            parent.appendChild(r);
            if (cfg) parent.appendChild(cfg);
          });
        }

        search.addEventListener("input", applyFilterSort);
        sort.addEventListener("change", applyFilterSort);

        refreshDerived();
        applyFilterSort();
        setInterval(refreshDerived, 4000);
      </script>
    </body>
    </html>
    """

    return render_template_string(
        html,
        app_name=APP_NAME,
        machines=list(machines.values()),
        total_hours=total_hours,
        configs=machine_configs,
        token=token,
        jobs_count=len(jobs),
        update=update_state
    )

@app.route("/")
def home():
    return "GreenIdle server OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
