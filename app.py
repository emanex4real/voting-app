"""
Voting App - Flask Web/API service
Phase 2: runs against Postgres (via DATABASE_URL) and pushes cast votes
onto a Redis queue ("vote_queue") for the Node Worker to process and
persist. Flask itself never writes Vote rows anymore.
"""

import os
import json
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, flash, request, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import redis

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///voting.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

redis_client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
VOTE_QUEUE_KEY = "vote_queue"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    has_voted = db.Column(db.Boolean, default=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Option(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    votes = db.relationship("Vote", backref="option", lazy=True)


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey("option.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or not user.is_admin:
            flash("Admin access required.")
            return redirect(url_for("vote"))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None


# ---------------------------------------------------------------------------
# Health check (for container / load balancer health checks later)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    try:
        redis_client.ping()
        db.session.execute(db.text("SELECT 1"))
        return {"status": "ok"}, 200
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}, 503


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            flash("Username and password are required.")
            return redirect(url_for("register"))

        if User.query.filter_by(username=username).first():
            flash("Username already taken.")
            return redirect(url_for("register"))

        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Account created. Please log in.")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session["user_id"] = user.id
            flash(f"Welcome back, {user.username}.")
            return redirect(url_for("vote"))

        flash("Invalid username or password.")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Voting routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("vote"))


@app.route("/vote", methods=["GET", "POST"])
@login_required
def vote():
    user = current_user()
    options = Option.query.all()

    if request.method == "POST":
        if user.has_voted:
            flash("You have already voted.")
            return redirect(url_for("vote"))

        option_id = request.form.get("option_id")
        option = Option.query.get(option_id)
        if not option:
            flash("Invalid option selected.")
            return redirect(url_for("vote"))

        # Mark the user as voted right away so they can't submit twice
        # while the job is still queued. The Worker does the actual
        # Vote-row write once it pulls this job off the queue.
        user.has_voted = True
        db.session.commit()

        job = {"user_id": user.id, "option_id": option.id}
        redis_client.rpush(VOTE_QUEUE_KEY, json.dumps(job))

        flash("Your vote has been recorded.")
        return redirect(url_for("vote"))

    return render_template("vote.html", options=options, user=user)


# ---------------------------------------------------------------------------
# Admin / results routes
# ---------------------------------------------------------------------------

@app.route("/results")
@admin_required
def results():
    options = Option.query.all()
    total_votes = Vote.query.count()
    data = [
        {"name": o.name, "count": len(o.votes)}
        for o in options
    ]
    return render_template("results.html", data=data, total_votes=total_votes)


# ---------------------------------------------------------------------------
# CLI helper: seed some starting data
# ---------------------------------------------------------------------------

@app.cli.command("seed")
def seed():
    """Seed the database with options and an admin user: flask seed"""
    db.create_all()

    if not Option.query.first():
        db.session.add_all([Option(name="Option A"), Option(name="Option B")])

    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", is_admin=True)
        admin.set_password("admin123")
        db.session.add(admin)

    db.session.commit()
    print("Seeded: options + admin user (admin / admin123)")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, host="0.0.0.0", port=5000)
