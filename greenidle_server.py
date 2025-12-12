from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from datetime import datetime
import uuid

app = Flask(__name__)

# =========================
#   CONFIG GREENIDLE
# =========================
APP_NAME = "GreenIdle"

# ⚠ A changer pour toi uniquement
ADMIN_TOKEN = "Iletait1fois@33"

DEBUG = False  # True en local, False sur Render


# =========================
#   MINI BDD EN MEMOIRE
# =========================
# machines[machine_id] = {...}
machines = {}

# jobs[job_id] = {
#   "job_id": ...,
#   "name": ...,
#   "description": ...,
#   "task_type": "demo" ou "montecarlo",
#   "total_chunks": int,
#   "created_at": ...,
#   "status": "pending"/"running"/"done",
#   "total_seconds": int,
# }
jobs = {}

# tasks[task_id] = {
#   "task_id": ...,
#   "job_id": ...,
#   "task_type": ...,
#   "size": int,
#   "status": "pending"/"assigned"/"done",
#   "assigned_to": machine_id ou None,
#   "created_at": ...,
#   "updated_at": ...,
#   "seconds": int
# }
tasks = {}

# results : liste de rapports de tâches (pour affichage)
results = []  # {task_id, job_id, machine_id, seconds, timestamp}

# log brut des reports
tasks_log = []


# =========================
#   OUTILS
# =========================
def now_iso():
    return datetime.utcnow().isoformat()


def ensure_machine(machine_id, display_name=None, mode=None):
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
            "mode": mode or "normal",
            "tags": []
        }
    else:
        if display_name:
            machines[machine_id]["display_name"] = display_name
        if mode:
            machines[machine_id]["mode"] = mode

    return machines[machine_id]


def require_admin():
    token = request.args.get("token")
    if token != ADMIN_TOKEN:
        return False
    return True


# =========================
#   API CLIENTS
# =========================

@app.route("/register", methods=["POST"])
def register():
    data = request.json or {}
    machine_id = data.get("machine_id")
    client_name = data.get("client_name")
    mode = data.get("mode")

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    m = ensure_machine(machine_id, client_name, mode)
    m["last_seen"] = now_iso()

    print(f"[{APP_NAME}] REGISTER {machine_id} (name={m['display_name']}, mode={m['mode']})")
    return jsonify({"status": "ok", "message": "machine enregistree"})


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json or {}
    machine_id = data.get("machine_id")
    client_name = data.get("client_name")
    cpu = float(data.get("cpu_percent", 0.0))
    mode = data.get("mode")

    if not machine_id:
        return jsonify({"error": "machine_id manquant"}), 400

    m = ensure_machine(machine_id, client_name, mode)
    m["last_seen"] = now_iso()
    m["last_cpu"] = cpu

    return jsonify({"status": "ok"})


@app.route("/task", methods=["GET"])
def get_task():
    machine_id = request.args.get("machine_id", "unknown")
    ensure_machine(machine_id)

    # 1) Cherche une tâche utilisateur en attente
    for t in tasks.values():
        if t["status"] == "pending":
            t["status"] = "assigned"
            t["assigned_to"] = machine_id
            t["updated_at"] = now_iso()
            job = jobs.get(t["job_id"])
            if job and job["status"] == "pending":
                job["status"] = "running"

            print(f"[{APP_NAME}] Assign task {t['task_id']} (job={t['job_id']}) to {machine_id}")
            return jsonify({
                "task_id": t["task_id"],
                "payload": t["task_type"],
                "size": t["size"]
            })

    # 2) Sinon, tâche démo par défaut
    return jsonify({
        "task_id": "demo-task",
        "payload": "demo",
        "max_duration_seconds": 10
    })


@app.route("/report", methods=["POST"])
def report():
    data = request.json or {}
    machine_id = data.get("machine_id")
    task_id = data.get("task_id")
    seconds = int(data.get("seconds", 0))

    if not machine_id or task_id is None:
        return jsonify({"error": "machine_id ou task_id manquant"}), 400

    m = ensure_machine(machine_id)
    m["total_seconds"] += seconds
    m["last_seen"] = now_iso()

    tasks_log.append({
        "machine_id": machine_id,
        "task_id": task_id,
        "seconds": seconds,
        "reported_at": now_iso()
    })

    # Si c'est une vraie tâche (pas juste demo-task)
    if task_id in tasks:
        t = tasks[task_id]
        t["status"] = "done"
        t["seconds"] = t.get("seconds", 0) + seconds
        t["updated_at"] = now_iso()

        job = jobs.get(t["job_id"])
        if job:
            job["total_seconds"] += seconds
            # On vérifie si toutes les tâches de ce job sont finies
            all_done = all(
                (tt["status"] == "done")
                for tt in tasks.values()
                if tt["job_id"] == job["job_id"]
            )
            if all_done:
                job["status"] = "done"

        results.append({
            "task_id": task_id,
            "job_id": t["job_id"],
            "machine_id": machine_id,
            "seconds": seconds,
            "timestamp": now_iso()
        })

    print(f"[{APP_NAME}] REPORT {machine_id} +{seconds}s (task={task_id})")
    return jsonify({"status": "ok"})


@app.route("/status", methods=["GET"])
def status():
    total_seconds = sum(m["total_seconds"] for m in machines.values())
    return jsonify({
        "app": APP_NAME,
        "machines_count": len(machines),
        "total_hours": round(total_seconds / 3600, 4),
        "machines": list(machines.values()),
        "jobs_count": len(jobs)
    })


# =========================
#   API / VUES ADMIN
# =========================

@app.route("/machines", methods=["GET"])
def list_machines():
    if not require_admin():
        return "Accès refusé (admin token)", 403
    return jsonify({
        "machines": list(machines.values()),
        "count": len(machines)
    })


@app.route("/machines/<machine_id>/rename", methods=["POST"])
def rename_machine(machine_id):
    if not require_admin():
        return "Accès refusé (admin token)", 403

    if machine_id not in machines:
        return jsonify({"error": "machine inconnue"}), 404

    data = request.json or {}
    new_name = data.get("display_name")
    if not new_name:
        return jsonify({"error": "display_name manquant"}), 400

    machines[machine_id]["display_name"] = new_name
    return jsonify({"status": "ok", "machine": machines[machine_id]})


@app.route("/submit", methods=["GET", "POST"])
def submit_job():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    if request.method == "POST":
        name = request.form.get("name", "Job sans nom")
        description = request.form.get("description", "")
        task_type = request.form.get("task_type", "demo")  # "demo" ou "montecarlo"
        total_chunks = int(request.form.get("chunks", 10))
        size = int(request.form.get("size", 10000))

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

        # Création des tâches
        for i in range(total_chunks):
            task_id = f"{job_id}_part_{i+1}"
            tasks[task_id] = {
                "task_id": task_id,
                "job_id": job_id,
                "task_type": task_type,
                "size": size,
                "status": "pending",
                "assigned_to": None,
                "created_at": now_iso(),
                "updated_at": None,
                "seconds": 0
            }

        return redirect(url_for("jobs_view", token=request.args.get("token")))

    html = """
    <h1>Soumettre un job GreenIdle</h1>
    <form method="post">
        Nom du job :<br>
        <input name="name" type="text" value="Simulation Monte Carlo"><br><br>

        Description :<br>
        <textarea name="description" rows="3" cols="40"></textarea><br><br>

        Type de tâche :
        <select name="task_type">
            <option value="demo">Démo (sec de calcul)</option>
            <option value="montecarlo">Monte Carlo</option>
        </select><br><br>

        Nombre de morceaux (chunks) :<br>
        <input name="chunks" type="number" value="10"><br><br>

        Taille de chaque morceau (paramètre 'size') :<br>
        <input name="size" type="number" value="10000"><br><br>

        <button type="submit">Créer le job</button>
    </form>
    <br>
    <a href="/dashboard?token={{ token }}">Retour dashboard</a>
    """
    return render_template_string(html, token=request.args.get("token"))


@app.route("/jobs")
def jobs_view():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    html = """
    <h1>Jobs GreenIdle</h1>
    <p><a href="/dashboard?token={{ token }}">Retour dashboard</a> |
       <a href="/submit?token={{ token }}">Nouveau job</a></p>

    {% if jobs %}
    <table border="1" cellspacing="0" cellpadding="5">
        <tr>
            <th>ID</th>
            <th>Nom</th>
            <th>Type</th>
            <th>Status</th>
            <th>Chunks</th>
            <th>Secondes totales</th>
            <th>Créé le</th>
            <th>Détail</th>
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
    <p>Aucun job encore.</p>
    {% endif %}
    """
    return render_template_string(
        html,
        jobs=list(jobs.values()),
        token=request.args.get("token")
    )


@app.route("/jobs/<job_id>")
def job_detail(job_id):
    if not require_admin():
        return "Accès refusé (admin token)", 403

    job = jobs.get(job_id)
    if not job:
        return f"Job {job_id} introuvable", 404

    job_tasks = [t for t in tasks.values() if t["job_id"] == job_id]

    html = """
    <h1>Détail job {{ job.job_id }}</h1>
    <p><strong>Nom :</strong> {{ job.name }}</p>
    <p><strong>Description :</strong> {{ job.description }}</p>
    <p><strong>Type :</strong> {{ job.task_type }}</p>
    <p><strong>Status :</strong> {{ job.status }}</p>
    <p><strong>Secondes totales :</strong> {{ job.total_seconds }}</p>
    <p><a href="/jobs?token={{ token }}">Retour jobs</a></p>

    <h2>Tâches</h2>
    {% if job_tasks %}
    <table border="1" cellspacing="0" cellpadding="5">
        <tr>
            <th>Task ID</th>
            <th>Status</th>
            <th>Assignée à</th>
            <th>Secondes</th>
            <th>Créée</th>
            <th>Maj</th>
        </tr>
        {% for t in job_tasks %}
        <tr>
            <td>{{ t.task_id }}</td>
            <td>{{ t.status }}</td>
            <td>{{ t.assigned_to }}</td>
            <td>{{ t.seconds }}</td>
            <td>{{ t.created_at }}</td>
            <td>{{ t.updated_at }}</td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <p>Aucune tâche pour ce job.</p>
    {% endif %}
    """
    return render_template_string(
        html,
        job=job,
        job_tasks=job_tasks,
        token=request.args.get("token")
    )


@app.route("/results")
def results_view():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    html = """
    <h1>Résultats des tâches</h1>
    <p><a href="/dashboard?token={{ token }}">Retour dashboard</a></p>
    {% if rows %}
    <table border="1" cellspacing="0" cellpadding="5">
        <tr>
            <th>Job</th>
            <th>Task</th>
            <th>Machine</th>
            <th>Secondes</th>
            <th>Date</th>
        </tr>
        {% for r in rows %}
        <tr>
            <td>{{ r.job_id }}</td>
            <td>{{ r.task_id }}</td>
            <td>{{ r.machine_id }}</td>
            <td>{{ r.seconds }}</td>
            <td>{{ r.timestamp }}</td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <p>Aucun résultat pour le moment.</p>
    {% endif %}
    """
    return render_template_string(
        html,
        rows=results,
        token=request.args.get("token")
    )


# =========================
#   DASHBOARD
# =========================

@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    if not require_admin():
        return "Accès refusé (admin token)", 403

    total_seconds = sum(m["total_seconds"] for m in machines.values())
    total_hours = round(total_seconds / 3600, 4)
    machines_count = len(machines)

    html = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="utf-8">
        <title>{{ app_name }} - Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            h1 { margin-bottom: 0.2em; }
            .stats { margin-bottom: 1em; }
            table { border-collapse: collapse; width: 100%; max-width: 1000px; }
            th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
            th { background: #f0f0f0; }
            tr:nth-child(even) { background: #fafafa; }
            .small { font-size: 0.9em; color: #666; }
            h2 { margin-top: 1.5em; }
            .mode-normal { color: #2c7; font-weight: bold; }
            .mode-night { color: #07c; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>{{ app_name }} - Tableau de bord</h1>
        <div class="stats">
            <p><strong>Machines actives :</strong> {{ machines_count }}</p>
            <p><strong>Total d'heures de calcul :</strong> {{ total_hours }}</p>
            <p>
              <a href="/submit?token={{ token }}">Nouveau job</a> |
              <a href="/jobs?token={{ token }}">Voir les jobs</a> |
              <a href="/results?token={{ token }}">Résultats</a> |
              <a href="/machines?token={{ token }}">API machines (JSON)</a>
            </p>
        </div>

        <h2>Machines</h2>
        {% if machines %}
        <table>
            <thead>
                <tr>
                    <th>ID interne</th>
                    <th>Nom affiché</th>
                    <th>Mode</th>
                    <th>CPU (dernier)</th>
                    <th>Enregistrée le</th>
                    <th>Dernière activité</th>
                    <th>Secondes totales</th>
                    <th>Heures</th>
                </tr>
            </thead>
            <tbody>
                {% for m in machines %}
                <tr>
                    <td>{{ m.machine_id }}</td>
                    <td>{{ m.display_name }}</td>
                    <td class="small">
                        {% if m.mode == 'night' %}
                            <span class="mode-night">nuit</span>
                        {% else %}
                            <span class="mode-normal">normal</span>
                        {% endif %}
                    </td>
                    <td>{{ m.last_cpu }} %</td>
                    <td class="small">{{ m.registered_at }}</td>
                    <td class="small">{{ m.last_seen }}</td>
                    <td>{{ m.total_seconds }}</td>
                    <td>{{ (m.total_seconds / 3600) | round(4) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p>Aucune machine n'est encore connectée.</p>
        {% endif %}
    </body>
    </html>
    """
    return render_template_string(
        html,
        app_name=APP_NAME,
        machines=list(machines.values()),
        machines_count=machines_count,
        total_hours=total_hours,
        token=request.args.get("token")
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
