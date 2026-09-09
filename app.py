"""Flask task service: password authentication, isolated records and atomic edits."""
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from bson import ObjectId
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash


def create_app(config=None, database=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=16 * 1024,
        MONGO_URI=os.environ.get("MONGO_URI", "mongodb://localhost:27017/taskmanager"),
    )
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
        raise ValueError("SECRET_KEY must contain at least 32 characters; generate one with secrets.token_hex(32)")
    if database is None:
        client = MongoClient(app.config["MONGO_URI"], serverSelectionTimeoutMS=2000)
        database = client.get_default_database()
        app.extensions["mongo_client"] = client
    app.extensions["db"] = db = database
    db.users.create_index("username", unique=True)
    db.tasks.create_index([("owner", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)])
    db.tasks.create_index([("owner", ASCENDING), ("done", ASCENDING), ("created_at", DESCENDING)])
    # Dummy verification avoids an obviously fast response for nonexistent users.
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32), method="scrypt")

    def login_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapped

    @app.before_request
    def protect_request():
        g.started = time.monotonic()
        g.request_id = secrets.token_hex(8)
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            token = request.form.get("csrf", "")
            if not secrets.compare_digest(token.encode(), session["csrf"].encode()):
                abort(400, "Invalid form token. Reload the page and try again.")

    @app.after_request
    def observe(response):
        response.headers.update({
            "X-Request-ID": g.request_id, "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'",
            "Cache-Control": "no-store",
        })
        app.logger.info(json.dumps({"request_id": g.request_id,
            "method": request.method, "route": str(request.url_rule),
            "status": response.status_code, "duration_ms": round((time.monotonic()-g.started)*1000, 2)}))
        return response

    @app.errorhandler(PyMongoError)
    def database_error(error):
        app.logger.error("Database operation failed: %s", type(error).__name__)
        return render_template("error.html", message="Database temporarily unavailable. Please try again."), 503

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def expected_error(error):
        return render_template("error.html", message=error.description), error.code

    @app.get("/health/live")
    def live():
        return jsonify(status="ok")

    @app.get("/health/ready")
    def ready():
        try:
            db.command("ping")
            return jsonify(status="ready")
        except PyMongoError:
            return jsonify(status="unavailable"), 503

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            if not re.fullmatch(r"[a-z0-9_]{3,32}", username) or not 12 <= len(password) <= 128:
                abort(400, "Use a 3–32 character username (letters, numbers, underscores) and a 12–128 character password.")
            try:
                db.users.insert_one({"username": username, "password_hash": generate_password_hash(password, method="scrypt")})
            except DuplicateKeyError:
                abort(409, "That username is already registered.")
            return redirect(url_for("login"))
        return render_template("auth.html", registering=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            if len(password) > 128:
                abort(400, "Invalid credentials.")
            user = db.users.find_one({"username": username})
            valid = check_password_hash(user["password_hash"] if user else dummy_hash, password)
            if not user or not valid:
                return render_template("auth.html", error="Invalid username or password."), 401
            session.clear()
            session.update(user_id=str(user["_id"]), username=user["username"], csrf=secrets.token_urlsafe(32))
            session.permanent = True
            return redirect(url_for("index"))
        return render_template("auth.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        status = request.args.get("status", "all")
        if status not in {"all", "open", "done"}:
            abort(400, "Unknown task filter.")
        try:
            page = int(request.args.get("page", 1))
            if not 1 <= page <= 10000:
                raise ValueError
        except ValueError:
            abort(400, "Invalid page.")
        query = {"owner": session["user_id"]}
        if status != "all":
            query["done"] = status == "done"
        tasks = list(db.tasks.find(query).sort([("created_at", -1), ("_id", -1)]).skip((page-1)*20).limit(21))
        counts = {"all": db.tasks.count_documents({"owner": session["user_id"]}),
                  "done": db.tasks.count_documents({"owner": session["user_id"], "done": True})}
        return render_template("dashboard.html", tasks=tasks[:20], has_next=len(tasks)>20,
                               page=page, status=status, counts=counts)

    @app.post("/tasks")
    @login_required
    def add():
        title = request.form.get("title", "").strip()
        priority = request.form.get("priority", "normal")
        if not 1 <= len(title) <= 200 or priority not in {"low", "normal", "high"}:
            abort(400, "Supply a 1–200 character title and a valid priority.")
        db.tasks.insert_one({"owner": session["user_id"], "title": title, "priority": priority,
                             "done": False, "version": 1, "created_at": datetime.now(timezone.utc)})
        return redirect(url_for("index"))

    @app.post("/tasks/<task_id>/<action>")
    @login_required
    def change(task_id, action):
        if not ObjectId.is_valid(task_id) or action not in {"complete", "reopen", "delete"}:
            abort(404)
        try:
            version = int(request.form.get("version", ""))
            if version < 1:
                raise ValueError
        except ValueError:
            abort(400, "Invalid task version.")
        query = {"_id": ObjectId(task_id), "owner": session["user_id"], "version": version}
        if action == "delete":
            changed = db.tasks.delete_one(query).deleted_count
        else:
            changed = db.tasks.update_one(query, {"$set": {"done": action == "complete"}, "$inc": {"version": 1}}).modified_count
        if not changed:
            # Do not expose whether a task belongs to another account.
            if not db.tasks.find_one({"_id": ObjectId(task_id), "owner": session["user_id"]}):
                abort(404)
            abort(409, "This task changed in another tab. Reload before editing it.")
        return redirect(url_for("index"))

    return app


logging.basicConfig(level=logging.INFO)
