import os, json, sqlite3, uuid
from datetime import date, datetime
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, send_from_directory, abort)
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "app.db")
UP = os.path.join(BASE, "uploads")
os.makedirs(UP, exist_ok=True)
app = Flask(__name__)
app.secret_key = "mandala-dev-key"

STATUS_CLR = {"Draft": "secondary", "Menunggu Review": "warning", "Perlu Revisi": "danger",
              "Siap Dikerjakan": "info", "Sedang Dikerjakan": "primary", "Selesai by Programmer": "success"}
FIELDS = ["name", "unit", "pic_name", "pic_contact", "purpose", "audience", "deadline", "description"]
LABEL = {"name": "Nama Aplikasi", "unit": "Unit Pengusul", "pic_name": "Nama PIC", "pic_contact": "Kontak PIC",
         "purpose": "Tujuan Aplikasi", "audience": "Calon Pengguna", "deadline": "Deadline",
         "description": "Deskripsi Aplikasi"}
KIND = {"letter": "Surat Permohonan", "process": "Lampiran Proses Bisnis",
        "support": "Lampiran Pendukung", "progress": "Bukti Progres"}
ASPECTS = {"a_complete": "Kelengkapan pengajuan", "a_need": "Kejelasan kebutuhan aplikasi",
           "a_process": "Kejelasan proses bisnis"}
URG_W = {"High": 3, "Medium": 2, "Low": 1}
HOME = {"client": "client_list", "analyst": "analyst_list", "leader": "leader_queue", "programmer": "prog_list"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, name TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, client_id INT, name TEXT, unit TEXT, pic_name TEXT,
  pic_contact TEXT, purpose TEXT, audience TEXT, deadline TEXT, description TEXT, status TEXT DEFAULT 'Draft',
  version INT DEFAULT 0, urgency_rec TEXT, urgency TEXT, queue_pos INT, queued_at TEXT, finished_at TEXT,
  progress INT DEFAULT 0, client_notified INT DEFAULT 1, leader_notified INT DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, request_id INT, kind TEXT, filename TEXT, stored TEXT, progress_id INT);
CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY, request_id INT, version INT, snapshot TEXT,
  decision TEXT, flags TEXT, notes TEXT, submitted_at TEXT, reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY, request_id INT, user_id INT, is_coord INT DEFAULT 0,
  active INT DEFAULT 1, assigned_at TEXT, removed_at TEXT);
CREATE TABLE IF NOT EXISTS progress(id INTEGER PRIMARY KEY, request_id INT, user_id INT, pdate TEXT, detail TEXT,
  percent INT, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, request_id INT, name TEXT, description TEXT, assignee_id INT);
"""
SEED = [("client1", "Client Fakultas Teknik", "client"), ("client2", "Client Biro Akademik", "client"),
        ("analyst1", "Analis Satu", "analyst"), ("leader1", "Leader IT", "leader"),
        ("prog1", "Programmer Andi", "programmer"), ("prog2", "Programmer Budi", "programmer"),
        ("prog3", "Programmer Citra", "programmer")]


def init_db():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM users").fetchone():
        c.executemany("INSERT INTO users(username,password,name,role) VALUES(?,?,?,?)",
                      [(u, "123", n, r) for u, n, r in SEED])
    c.commit(); c.close()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e):
    d = g.pop("db", None)
    if d: d.close()


def q(sql, a=(), one=False):
    rows = db().execute(sql, a).fetchall()
    db().commit()
    return (rows[0] if rows else None) if one else rows


def ins(sql, a=()):
    cur = db().execute(sql, a)
    db().commit()
    return cur.lastrowid


def now(): return datetime.now().strftime("%Y-%m-%d %H:%M")


def days_left(d):
    try: return (date.fromisoformat(d) - date.today()).days
    except Exception: return 999


@app.context_processor
def ctx():
    return dict(STATUS_CLR=STATUS_CLR, LABEL=LABEL, KIND=KIND, ASPECTS=ASPECTS, FIELDS=FIELDS, days_left=days_left)


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session: return redirect(url_for("login"))
            if roles and session["role"] not in roles: abort(403)
            return f(*a, **k)
        return w
    return deco


def get_req(rid):
    return q("SELECT * FROM requests WHERE id=?", (rid,), True) or abort(404)


def assigned(rid, uid, active_only=False):
    sql = "SELECT * FROM assignments WHERE request_id=? AND user_id=?" + (" AND active=1" if active_only else "")
    return q(sql, (rid, uid), True)


def can_view(r):
    role, uid = session["role"], session["uid"]
    if role == "client": return r["client_id"] == uid
    if role == "analyst": return r["status"] != "Draft"
    if role == "leader": return r["status"] != "Draft"
    return bool(assigned(r["id"], uid))


def rm_file(f):
    try: os.remove(os.path.join(UP, f["stored"]))
    except OSError: pass
    q("DELETE FROM files WHERE id=?", (f["id"],))


def save_files(rid, field, kind, pid=None):
    for f in request.files.getlist(field):
        if f and f.filename:
            stored = uuid.uuid4().hex + "_" + secure_filename(f.filename)
            f.save(os.path.join(UP, stored))
            ins("INSERT INTO files(request_id,kind,filename,stored,progress_id) VALUES(?,?,?,?,?)",
                (rid, kind, f.filename, stored, pid))


def detail_ctx(r):
    rid = r["id"]
    subs = []
    for s in q("SELECT * FROM submissions WHERE request_id=? ORDER BY version DESC", (rid,)):
        subs.append(dict(s, flags=json.loads(s["flags"] or "{}"), snap=json.loads(s["snapshot"])))
    return dict(
        r=r,
        files=q("SELECT * FROM files WHERE request_id=? AND progress_id IS NULL ORDER BY id", (rid,)),
        pfiles=q("SELECT * FROM files WHERE request_id=? AND progress_id IS NOT NULL", (rid,)),
        prog=q("SELECT p.*, u.name uname FROM progress p JOIN users u ON u.id=p.user_id "
               "WHERE request_id=? ORDER BY pdate, p.id", (rid,)),
        team=q("SELECT a.*, u.name uname FROM assignments a JOIN users u ON u.id=a.user_id "
               "WHERE request_id=? ORDER BY a.active DESC, a.id", (rid,)),
        subs=subs)


# ---------- Beban kerja (LD03) ----------
def load_of(uid):
    apps = q("SELECT r.* FROM assignments a JOIN requests r ON r.id=a.request_id "
             "WHERE a.user_id=? AND a.active=1 AND r.status='Sedang Dikerjakan'", (uid,))
    total = 0
    for r in apps:
        w = URG_W.get(r["urgency"], 2) * (1 + (100 - r["progress"]) / 100)
        dl = days_left(r["deadline"])
        w += 3 if dl < 0 else 2 if dl <= 7 else 1 if dl <= 14 else 0
        total += w
    level = "Ringan" if total < 4 else "Sedang" if total < 8 else "Berat"
    return dict(score=round(total, 1), level=level, apps=apps)


def programmers_with_load():
    return [dict(u=u, **load_of(u["id"])) for u in q("SELECT * FROM users WHERE role='programmer' ORDER BY name")]


def apply_team(rid, ids, coord):
    ids = [int(i) for i in ids]
    if not ids: return "Pilih minimal satu programmer."
    coord = int(coord) if coord else None
    if len(ids) > 1 and coord not in ids: return "Pilih satu Project Coordinator dari programmer terpilih."
    if len(ids) == 1: coord = None
    cur = {a["user_id"]: a for a in q("SELECT * FROM assignments WHERE request_id=? AND active=1", (rid,))}
    for uid, a in cur.items():
        if uid not in ids:
            q("UPDATE assignments SET active=0, is_coord=0, removed_at=? WHERE id=?", (now(), a["id"]))
    for uid in ids:
        if uid in cur: q("UPDATE assignments SET is_coord=? WHERE id=?", (int(uid == coord), cur[uid]["id"]))
        else: ins("INSERT INTO assignments(request_id,user_id,is_coord,assigned_at) VALUES(?,?,?,?)",
                  (rid, uid, int(uid == coord), now()))
    return None


# ---------- Auth ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE username=? AND password=?",
              (request.form["username"], request.form["password"]), True)
        if u:
            session.update(uid=u["id"], role=u["role"], name=u["name"])
            return redirect(url_for("home"))
        flash("Username atau password salah.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    if "uid" not in session: return redirect(url_for("login"))
    return redirect(url_for(HOME[session["role"]]))


@app.route("/file/<int:fid>")
@role_required()
def uploads(fid):
    f = q("SELECT * FROM files WHERE id=?", (fid,), True) or abort(404)
    if not can_view(get_req(f["request_id"])): abort(403)
    return send_from_directory(UP, f["stored"], download_name=f["filename"])


@app.route("/request/<int:rid>")
@role_required()
def request_detail(rid):
    r = get_req(rid)
    if not can_view(r): abort(403)
    return render_template("detail.html", **detail_ctx(r))


# ---------- Client (CL01, CL02) ----------
@app.route("/client")
@role_required("client")
def client_list():
    rows = q("SELECT * FROM requests WHERE client_id=? ORDER BY id DESC", (session["uid"],))
    for r in rows:
        if not r["client_notified"]:
            flash(f'Permintaan "{r["name"]}" telah dinyatakan siap dan masuk ke antrean pengerjaan.', "success")
    q("UPDATE requests SET client_notified=1 WHERE client_id=?", (session["uid"],))
    return render_template("client_list.html", rows=rows)


@app.route("/client/form", methods=["GET", "POST"])
@app.route("/client/form/<int:rid>", methods=["GET", "POST"])
@role_required("client")
def client_form(rid=None):
    r = None
    if rid:
        r = get_req(rid)
        if r["client_id"] != session["uid"] or r["status"] not in ("Draft", "Perlu Revisi"):
            flash("Permintaan tidak dapat diubah pada status ini.", "danger")
            return redirect(url_for("client_list"))
    if request.method == "POST":
        data = {k: request.form.get(k, "").strip() for k in FIELDS}
        if not data["name"]:
            flash("Nama aplikasi wajib diisi.", "danger")
            return redirect(request.url)
        if rid is None:
            rid = ins("INSERT INTO requests(client_id,name,created_at) VALUES(?,?,?)",
                      (session["uid"], data["name"], now()))
        q("UPDATE requests SET " + ",".join(f"{k}=?" for k in FIELDS) + " WHERE id=?", (*data.values(), rid))
        for fid in request.form.getlist("del_file"):
            f = q("SELECT * FROM files WHERE id=? AND request_id=? AND kind='support'", (fid, rid), True)
            if f: rm_file(f)
        for field in ("letter", "process"):
            fl = request.files.get(field)
            if fl and fl.filename:
                for old in q("SELECT * FROM files WHERE request_id=? AND kind=?", (rid, field)): rm_file(old)
                save_files(rid, field, field)
        save_files(rid, "support", "support")
        if request.form.get("action") == "submit":
            miss = [LABEL[k] for k, v in data.items() if not v]
            for kind in ("letter", "process"):
                if not q("SELECT 1 FROM files WHERE request_id=? AND kind=?", (rid, kind)): miss.append(KIND[kind])
            if miss:
                flash("Belum bisa dikirim, lengkapi: " + ", ".join(miss), "danger")
                return redirect(url_for("client_form", rid=rid))
            ver = get_req(rid)["version"] + 1
            snap = json.dumps({"fields": data, "files": [
                {"kind": f["kind"], "filename": f["filename"]} for f in
                q("SELECT kind,filename FROM files WHERE request_id=? AND progress_id IS NULL", (rid,))]})
            ins("INSERT INTO submissions(request_id,version,snapshot,submitted_at) VALUES(?,?,?,?)",
                (rid, ver, snap, now()))
            q("UPDATE requests SET status='Menunggu Review', version=? WHERE id=?", (ver, rid))
            flash("Permintaan dikirim, status: Menunggu Review.", "success")
        else:
            flash("Draf tersimpan.", "success")
        return redirect(url_for("client_list"))
    files = q("SELECT * FROM files WHERE request_id=? AND progress_id IS NULL", (rid,)) if rid else []
    last = q("SELECT * FROM submissions WHERE request_id=? AND decision='revise' ORDER BY version DESC",
             (rid,), True) if rid else None
    return render_template("client_form.html", form=r or {}, files=files, rid=rid, last=last,
                           flags=json.loads(last["flags"]) if last else {})


# ---------- Tim Analis (AN01, AN02) ----------
@app.route("/analyst")
@role_required("analyst")
def analyst_list():
    rows = q("SELECT r.*, u.name cname FROM requests r JOIN users u ON u.id=r.client_id "
             "WHERE status='Menunggu Review' ORDER BY r.id")
    return render_template("analyst_list.html", rows=rows)


@app.route("/analyst/<int:rid>", methods=["GET", "POST"])
@role_required("analyst")
def analyst_review(rid):
    r = get_req(rid)
    if r["status"] != "Menunggu Review":
        flash("Permintaan ini tidak sedang menunggu review.", "warning")
        return redirect(url_for("analyst_list"))
    if request.method == "POST":
        decision = request.form["decision"]
        notes = request.form.get("notes", "").strip()
        flags = {k: bool(request.form.get(k)) for k in ASPECTS}
        if decision == "ready":
            if not all(flags.values()):
                flash("Permintaan hanya bisa dinyatakan siap jika semua aspek terpenuhi.", "danger")
                return redirect(request.url)
            pos = (q("SELECT MAX(queue_pos) m FROM requests", one=True)["m"] or 0) + 1
            q("UPDATE requests SET status='Siap Dikerjakan', urgency_rec=?, queue_pos=?, queued_at=?, "
              "client_notified=0 WHERE id=?", (request.form.get("urgency_rec") or None, pos, now(), rid))
            flash("Permintaan dinyatakan Siap Dikerjakan dan masuk ke bagian bawah antrean.", "success")
        else:
            if all(flags.values()) or not notes:
                flash("Untuk mengembalikan permintaan: tandai minimal satu aspek belum jelas dan isi catatan perbaikan.", "danger")
                return redirect(request.url)
            q("UPDATE requests SET status='Perlu Revisi' WHERE id=?", (rid,))
            flash("Permintaan dikembalikan ke Client (Perlu Revisi).", "success")
        q("UPDATE submissions SET decision=?, flags=?, notes=?, reviewed_at=? WHERE request_id=? AND version=?",
          (decision, json.dumps(flags), notes, now(), rid, r["version"]))
        return redirect(url_for("analyst_list"))
    c = detail_ctx(r)
    cmp = None
    if len(c["subs"]) > 1:
        new, old = c["subs"][0]["snap"], c["subs"][1]["snap"]
        nf = [f'{KIND[f["kind"]]}: {f["filename"]}' for f in new["files"]]
        of = [f'{KIND[f["kind"]]}: {f["filename"]}' for f in old["files"]]
        cmp = dict(rows=[(LABEL[k], old["fields"][k], new["fields"][k]) for k in FIELDS],
                   new_files=nf, old_files=of, old_ver=c["subs"][1]["version"], new_ver=c["subs"][0]["version"])
    return render_template("analyst_review.html", cmp=cmp, **c)


# ---------- Leader (LD01-LD05) ----------
@app.route("/leader/queue")
@role_required("leader")
def leader_queue():
    rows = q("SELECT r.*, u.name cname FROM requests r JOIN users u ON u.id=r.client_id "
             "WHERE status='Siap Dikerjakan' ORDER BY queue_pos")
    return render_template("leader_queue.html", rows=rows)


@app.route("/leader/urgency/<int:rid>", methods=["POST"])
@role_required("leader")
def leader_urgency(rid):
    u = request.form.get("urgency")
    if u in URG_W: q("UPDATE requests SET urgency=? WHERE id=? AND status='Siap Dikerjakan'", (u, rid))
    return redirect(url_for("leader_queue"))


@app.route("/leader/move/<int:rid>/<d>")
@role_required("leader")
def leader_move(rid, d):
    rows = list(q("SELECT id, queue_pos FROM requests WHERE status='Siap Dikerjakan' ORDER BY queue_pos"))
    idx = next((i for i, r in enumerate(rows) if r["id"] == rid), None)
    j = None if idx is None else (idx - 1 if d == "up" else idx + 1)
    if j is not None and 0 <= j < len(rows):
        q("UPDATE requests SET queue_pos=? WHERE id=?", (rows[j]["queue_pos"], rows[idx]["id"]))
        q("UPDATE requests SET queue_pos=? WHERE id=?", (rows[idx]["queue_pos"], rows[j]["id"]))
    return redirect(url_for("leader_queue"))


@app.route("/leader/assign/<int:rid>", methods=["GET", "POST"])
@role_required("leader")
def leader_assign(rid):
    r = get_req(rid)
    if r["status"] != "Siap Dikerjakan":
        flash("Aplikasi tidak berada di antrean Siap Dikerjakan.", "warning")
        return redirect(url_for("leader_queue"))
    if request.method == "POST":
        err = apply_team(rid, request.form.getlist("prog"), request.form.get("coord"))
        if err:
            flash(err, "danger")
            return redirect(request.url)
        q("UPDATE requests SET status='Sedang Dikerjakan' WHERE id=?", (rid,))
        flash("Programmer ditugaskan. Status: Sedang Dikerjakan.", "success")
        return redirect(url_for("leader_monitor"))
    return render_template("leader_team.html", r=r, progs=programmers_with_load(), active_ids=[], coord_id=None,
                           history=[], mode="assign")


@app.route("/leader/manage/<int:rid>", methods=["GET", "POST"])
@role_required("leader")
def leader_manage(rid):
    r = get_req(rid)
    if r["status"] != "Sedang Dikerjakan":
        flash("Penugasan hanya bisa diubah pada aplikasi berstatus Sedang Dikerjakan.", "warning")
        return redirect(url_for("leader_monitor"))
    if request.method == "POST":
        err = apply_team(rid, request.form.getlist("prog"), request.form.get("coord"))
        flash(err or "Penugasan diperbarui. Laporan progres lama tetap tersimpan.", "danger" if err else "success")
        return redirect(request.url if err else url_for("leader_monitor"))
    c = detail_ctx(r)
    act = [a for a in c["team"] if a["active"]]
    return render_template("leader_team.html", r=r, progs=programmers_with_load(),
                           active_ids=[a["user_id"] for a in act],
                           coord_id=next((a["user_id"] for a in act if a["is_coord"]), None),
                           history=c["team"], mode="manage")


@app.route("/leader/workload")
@role_required("leader")
def leader_workload():
    return render_template("leader_workload.html", progs=programmers_with_load())


@app.route("/leader/monitor")
@role_required("leader")
def leader_monitor():
    rows = []
    for r in q("SELECT r.* FROM requests r WHERE status IN ('Sedang Dikerjakan','Selesai by Programmer') "
               "ORDER BY status DESC, deadline"):
        team = q("SELECT u.name, a.is_coord FROM assignments a JOIN users u ON u.id=a.user_id "
                 "WHERE a.request_id=? AND a.active=1", (r["id"],))
        dl = days_left(r["deadline"])
        flag = None
        if r["status"] == "Sedang Dikerjakan":
            flag = "Lewat deadline" if dl < 0 else "Mendekati deadline" if dl <= 7 else None
        if r["status"] == "Selesai by Programmer" and not r["leader_notified"]:
            flash(f'Aplikasi "{r["name"]}" telah ditandai Selesai by Programmer.', "success")
        rows.append(dict(r=r, team=team, dl=dl, flag=flag))
    q("UPDATE requests SET leader_notified=1")
    return render_template("leader_monitor.html", rows=rows)


# ---------- Programmer (PM01-PM04) ----------
@app.route("/prog")
@role_required("programmer")
def prog_list():
    rows = q("SELECT r.*, a.active, a.is_coord FROM assignments a JOIN requests r ON r.id=a.request_id "
             "WHERE a.user_id=? ORDER BY a.active DESC, r.deadline", (session["uid"],))
    return render_template("prog_list.html", rows=rows)


def prog_ctx(rid):
    r = get_req(rid)
    if not assigned(rid, session["uid"]): abort(403)
    me = assigned(rid, session["uid"], True)
    team = [a for a in detail_ctx(r)["team"] if a["active"]]
    return r, me, team


@app.route("/prog/<int:rid>")
@role_required("programmer")
def prog_detail(rid):
    r, me, team = prog_ctx(rid)
    c = detail_ctx(r)
    working = bool(me) and r["status"] == "Sedang Dikerjakan"
    tasks = q("SELECT t.*, u.name uname FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id "
              "WHERE request_id=? ORDER BY t.id", (rid,))
    return render_template("prog_detail.html", me=me, active_team=team, tasks=tasks, working=working,
                           now_date=date.today().isoformat(),
                           active_ids=[a["user_id"] for a in team],
                           can_task=working and len(team) > 1 and bool(me["is_coord"]),
                           can_finish=working and (len(team) == 1 or bool(me["is_coord"])), **c)


@app.route("/prog/<int:rid>/progress", methods=["POST"])
@role_required("programmer")
def prog_progress(rid):
    r, me, team = prog_ctx(rid)
    if not me or r["status"] != "Sedang Dikerjakan":
        flash("Anda tidak dapat menambah laporan progres pada aplikasi ini.", "danger")
        return redirect(url_for("prog_detail", rid=rid))
    detail = request.form.get("detail", "").strip()
    try: pct = int(request.form.get("percent", ""))
    except ValueError: pct = -1
    if not detail or not 0 <= pct <= 99:
        flash("Uraian wajib diisi dan persentase 0-99 (100% otomatis saat ditandai selesai).", "danger")
        return redirect(url_for("prog_detail", rid=rid))
    pid = ins("INSERT INTO progress(request_id,user_id,pdate,detail,percent,created_at) VALUES(?,?,?,?,?,?)",
              (rid, session["uid"], request.form.get("pdate") or date.today().isoformat(), detail, pct, now()))
    save_files(rid, "files", "progress", pid)
    q("UPDATE requests SET progress=? WHERE id=?", (pct, rid))
    flash("Laporan progres tersimpan.", "success")
    return redirect(url_for("prog_detail", rid=rid))


@app.route("/prog/<int:rid>/finish", methods=["POST"])
@role_required("programmer")
def prog_finish(rid):
    r, me, team = prog_ctx(rid)
    if not me or r["status"] != "Sedang Dikerjakan" or (len(team) > 1 and not me["is_coord"]):
        flash("Anda tidak berwenang menandai aplikasi ini selesai.", "danger")
    else:
        q("UPDATE requests SET status='Selesai by Programmer', progress=100, finished_at=?, leader_notified=0 "
          "WHERE id=?", (date.today().isoformat(), rid))
        flash("Aplikasi ditandai Selesai by Programmer.", "success")
    return redirect(url_for("prog_detail", rid=rid))


@app.route("/prog/<int:rid>/task", methods=["POST"])
@role_required("programmer")
def prog_task(rid):
    r, me, team = prog_ctx(rid)
    if not (me and me["is_coord"] and len(team) > 1 and r["status"] == "Sedang Dikerjakan"):
        abort(403)
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    who = int(request.form.get("assignee") or 0)
    if not name or who not in [a["user_id"] for a in team]:
        flash("Nama pekerjaan dan penanggung jawab (anggota aktif) wajib diisi.", "danger")
    elif request.form.get("task_id"):
        q("UPDATE tasks SET name=?, description=?, assignee_id=? WHERE id=? AND request_id=?",
          (name, desc, who, request.form["task_id"], rid))
        flash("Pekerjaan diperbarui.", "success")
    else:
        ins("INSERT INTO tasks(request_id,name,description,assignee_id) VALUES(?,?,?,?)", (rid, name, desc, who))
        flash("Pekerjaan ditambahkan.", "success")
    return redirect(url_for("prog_detail", rid=rid))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
