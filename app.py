import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone

from flask import Flask, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "donors.db")

# All ABO + Rh groups, plus the rare Bombay (hh) phenotype.
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Bombay (hh)"]

AVAILABILITY = {"available", "emergency", "unavailable"}
SMOKING = {"never", "occasionally", "daily"}
ALCOHOL = {"never", "occasionally", "regularly"}

MIN_AGE, MAX_AGE = 18, 65
MIN_WEIGHT_KG = 45
MIN_HEMOGLOBIN = 12.5  # g/dL
GAP_DAYS = {"Male": 90, "Female": 120, "Other": 120}  # days between whole-blood donations

# Doctor-style screening questions.
#   bad    -> the answer that defers the donor ("yes" / "no")
#   days   -> temporary deferral length; None = permanent
#   hidden -> reason is NOT shown to the user (sensitive); a generic message is used
#   female -> question only applies to female donors
SCREENING = [
    {"id": "feeling_well", "text": "Do you feel healthy and well today?",
     "bad": "no", "days": 7, "reason": "You should feel well on the day you donate."},
    {"id": "sleep", "text": "Did you sleep less than 5 hours last night?",
     "bad": "yes", "days": 1, "reason": "Please rest well before donating."},
    {"id": "alcohol24", "text": "Have you had any alcohol in the last 24 hours?",
     "bad": "yes", "days": 1, "reason": "No alcohol for 24 hours before donating."},
    {"id": "fever", "text": "Fever, cold, flu, cough or sore throat in the last 14 days?",
     "bad": "yes", "days": 14, "reason": "Recent illness needs 2 weeks of recovery."},
    {"id": "antibiotics", "text": "Are you currently taking antibiotics or being treated for an infection?",
     "bad": "yes", "days": 14, "reason": "Wait until the course is finished and you have recovered."},
    {"id": "special_meds", "text": "Do you take blood thinners, acne medicine (isotretinoin) or hair-loss medicine (finasteride/dutasteride)?",
     "bad": "yes", "days": 30, "reason": "These medicines need a waiting period; ask the doctor at the blood centre."},
    {"id": "vaccine", "text": "Any vaccination in the last 4 weeks?",
     "bad": "yes", "days": 28, "reason": "Wait 4 weeks after a vaccination."},
    {"id": "dental", "text": "Any tooth extraction or dental surgery in the last 72 hours?",
     "bad": "yes", "days": 3, "reason": "Wait 3 days after dental work."},
    {"id": "tattoo", "text": "Any tattoo, body piercing or acupuncture in the last 6 months?",
     "bad": "yes", "days": 180, "reason": "Wait 6 months after a tattoo, piercing or acupuncture."},
    {"id": "surgery", "text": "Any major surgery in the last 6 months?",
     "bad": "yes", "days": 180, "reason": "Wait 6 months after major surgery."},
    {"id": "transfusion", "text": "Have you received a blood transfusion in the last 12 months?",
     "bad": "yes", "days": 365, "reason": "Wait 12 months after a transfusion."},
    {"id": "malaria", "text": "Malaria in the last 3 months, or travel to a high-risk malaria area?",
     "bad": "yes", "days": 90, "reason": "Wait 3 months after malaria or high-risk travel."},
    {"id": "tb", "text": "Treated for tuberculosis in the last 2 years?",
     "bad": "yes", "days": 730, "reason": "Wait 2 years after tuberculosis treatment."},
    {"id": "pregnancy", "text": "Are you pregnant or breastfeeding, or have you delivered or had a miscarriage in the last 6 months?",
     "bad": "yes", "days": 180, "female": True,
     "reason": "Wait 6 months after delivery/miscarriage, and while pregnant or breastfeeding."},
    # Permanent / doctor-decides questions: reasons are intentionally generic and private.
    {"id": "chronic", "text": "Have you ever been diagnosed with heart disease, epilepsy/fits, cancer, kidney or liver disease, a bleeding disorder, or insulin-dependent diabetes?",
     "bad": "yes", "days": None, "hidden": True,
     "reason": "Based on your answers you are not able to donate blood."},
    {"id": "infection", "text": "Have you ever tested positive for HIV, hepatitis B, hepatitis C or syphilis?",
     "bad": "yes", "days": None, "hidden": True,
     "reason": "Based on your answers you are not able to donate blood."},
    {"id": "drugs", "text": "Have you ever injected drugs that were not prescribed by a doctor?",
     "bad": "yes", "days": None, "hidden": True,
     "reason": "Based on your answers you are not able to donate blood."},
]

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------- database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


NEW_COLUMNS = {
    "weight_kg": "REAL",
    "hemoglobin": "REAL",
    "last_donation_date": "TEXT",
    "next_eligible_date": "TEXT",
    "smoking": "TEXT",
    "alcohol": "TEXT",
    "availability": "TEXT NOT NULL DEFAULT 'available'",
    "token_hash": "TEXT",
    "screened_at": "TEXT",
}


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS donors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            gender TEXT,
            blood_group TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            country TEXT NOT NULL,
            state TEXT NOT NULL,
            city TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    existing = {r[1] for r in db.execute("PRAGMA table_info(donors)")}
    for col, decl in NEW_COLUMNS.items():
        if col not in existing:
            db.execute(f"ALTER TABLE donors ADD COLUMN {col} {decl}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_bg ON donors(blood_group)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_city ON donors(city)")
    db.commit()
    db.close()


# ---------- helpers ----------
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def mask_phone(phone):
    return phone[:3] + "*" * max(len(phone) - 5, 3) + phone[-2:]


def clean(value):
    return str(value or "").strip()


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# Tiny in-memory rate limiter (per IP). Use Flask-Limiter + Redis in production.
_hits = defaultdict(deque)


def rate_limited(key, limit, window_s):
    now = time.time()
    q = _hits[(key, request.remote_addr)]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def evaluate_eligibility(*, age, gender, weight, hemoglobin, last_donation, answers):
    """Returns (reasons, eligible_after_date_or_None, permanent)."""
    reasons, until, permanent = [], None, False
    today = date.today()

    def defer(reason, days):
        nonlocal until, permanent
        if reason not in reasons:
            reasons.append(reason)
        if days is None:
            permanent = True
        else:
            d = today + timedelta(days=days)
            until = d if until is None or d > until else until

    if weight < MIN_WEIGHT_KG:
        defer(f"Minimum weight to donate is {MIN_WEIGHT_KG} kg.", None)
    if hemoglobin is not None and hemoglobin < MIN_HEMOGLOBIN:
        defer(f"Hemoglobin should be at least {MIN_HEMOGLOBIN} g/dL. Please check with a doctor.", 90)

    if last_donation is not None:
        gap = GAP_DAYS.get(gender, 120)
        ready = last_donation + timedelta(days=gap)
        if ready > today:
            defer(f"You donated recently. {gender or 'Donors'} must wait {gap} days between donations "
                  f"(next possible: {ready.isoformat()}).", (ready - today).days)

    for q in SCREENING:
        if q.get("female") and gender != "Female":
            continue
        if answers.get(q["id"]) == q["bad"]:
            defer(q["reason"], q["days"])

    return reasons, until, permanent


# ---------- routes ----------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/blood-groups")
def blood_groups():
    return jsonify(BLOOD_GROUPS)


@app.get("/api/screening")
def screening():
    return jsonify([
        {"id": q["id"], "text": q["text"], "female_only": bool(q.get("female"))}
        for q in SCREENING
    ])


@app.post("/api/donors")
def add_donor():
    if rate_limited("register", 10, 3600):
        return jsonify({"errors": ["Too many registrations from this device. Try again later."]}), 429

    data = request.get_json(silent=True) or {}

    name = clean(data.get("name"))
    blood_group = clean(data.get("blood_group"))
    phone = re.sub(r"[\s\-()]", "", clean(data.get("phone")))
    email = clean(data.get("email"))
    country = clean(data.get("country"))
    state = clean(data.get("state"))
    city = clean(data.get("city"))
    gender = clean(data.get("gender"))
    smoking = clean(data.get("smoking")) or "never"
    alcohol = clean(data.get("alcohol")) or "never"
    availability = clean(data.get("availability")) or "available"
    answers = data.get("screening") if isinstance(data.get("screening"), dict) else {}

    errors = []
    if len(name) < 2:
        errors.append("Name is required.")
    if blood_group not in BLOOD_GROUPS:
        errors.append("Select a valid blood group.")
    if gender not in GAP_DAYS:
        errors.append("Select your gender.")
    if not re.fullmatch(r"\+?\d{7,15}", phone):
        errors.append("Enter a valid phone number (7-15 digits, optional +).")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append("Email looks invalid.")
    if not (country and state and city):
        errors.append("Country, state and city are required.")
    if smoking not in SMOKING or alcohol not in ALCOHOL or availability not in AVAILABILITY:
        errors.append("Invalid smoking, alcohol or availability value.")

    try:
        age = int(data.get("age"))
    except (TypeError, ValueError):
        age = 0
        errors.append("Age is required.")
    try:
        weight = float(data.get("weight_kg"))
        if not 20 <= weight <= 300:
            raise ValueError
    except (TypeError, ValueError):
        weight = 0
        errors.append("Enter a valid weight in kg.")

    hemoglobin = None
    if clean(data.get("hemoglobin")):
        try:
            hemoglobin = float(data["hemoglobin"])
            if not 3 <= hemoglobin <= 25:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Hemoglobin must be a number in g/dL (e.g. 13.5).")

    last_donation = None
    if clean(data.get("last_donation_date")):
        last_donation = parse_date(clean(data["last_donation_date"]))
        if last_donation is None or last_donation > date.today():
            errors.append("Last donation date is invalid.")
            last_donation = None

    for q in SCREENING:
        if q.get("female") and gender != "Female":
            continue
        if answers.get(q["id"]) not in ("yes", "no"):
            errors.append("Please answer all health screening questions.")
            break

    if not data.get("consent"):
        errors.append("You must consent to sharing your details with people seeking blood.")

    lat = lng = None
    if data.get("latitude") not in (None, "") and data.get("longitude") not in (None, ""):
        try:
            lat, lng = float(data["latitude"]), float(data["longitude"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Invalid coordinates.")
            lat = lng = None

    if errors:
        return jsonify({"errors": errors}), 400

    # Age check is a hard rule, handled together with the medical screening.
    reasons, until, permanent = [], None, False
    if not MIN_AGE <= age <= MAX_AGE:
        reasons.append(f"Donors must be between {MIN_AGE} and {MAX_AGE} years old.")
        permanent = True
    more, until, perm2 = evaluate_eligibility(
        age=age, gender=gender, weight=weight, hemoglobin=hemoglobin,
        last_donation=last_donation, answers=answers,
    )
    reasons += [r for r in more if r not in reasons]
    permanent = permanent or perm2

    if reasons:
        return jsonify({
            "eligible": False,
            "permanent": permanent,
            "eligible_after": None if permanent else (until.isoformat() if until else None),
            "reasons": reasons,
        }), 422

    # Only non-sensitive facts are stored. Answers to the medical questions are NOT saved.
    token = secrets.token_urlsafe(12)
    gap = GAP_DAYS[gender]
    next_eligible = (last_donation + timedelta(days=gap)).isoformat() if last_donation else None
    db = get_db()
    cur = db.execute(
        """INSERT INTO donors
           (name, age, gender, blood_group, phone, email, country, state, city,
            latitude, longitude, available, created_at,
            weight_kg, hemoglobin, last_donation_date, next_eligible_date,
            smoking, alcohol, availability, token_hash, screened_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)""",
        (name, age, gender, blood_group, phone, email, country, state, city,
         lat, lng, datetime.now(timezone.utc).isoformat(),
         weight, hemoglobin, last_donation.isoformat() if last_donation else None, next_eligible,
         smoking, alcohol, availability, hash_token(token), date.today().isoformat()),
    )
    db.commit()
    return jsonify({
        "eligible": True,
        "id": cur.lastrowid,
        "manage_key": token,
        "message": "Thank you! You passed the self-screening and are now listed as a donor.",
    }), 201


@app.get("/api/donors")
def search_donors():
    blood_group = clean(request.args.get("blood_group"))
    country = clean(request.args.get("country")).lower()
    state = clean(request.args.get("state")).lower()
    city = clean(request.args.get("city")).lower()

    sql = ("SELECT * FROM donors WHERE availability != 'unavailable' "
           "AND (next_eligible_date IS NULL OR next_eligible_date <= ?)")
    params = [date.today().isoformat()]
    if blood_group:
        sql += " AND blood_group = ?"
        params.append(blood_group)
    if country:
        sql += " AND LOWER(country) = ?"
        params.append(country)
    if state:
        sql += " AND LOWER(state) = ?"
        params.append(state)
    if city:
        sql += " AND LOWER(city) = ?"
        params.append(city)

    rows = get_db().execute(sql, params).fetchall()

    try:
        my_lat = float(request.args.get("lat"))
        my_lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        my_lat = my_lng = None

    results = []
    for r in rows:
        item = {
            "id": r["id"],
            "name": r["name"],
            "blood_group": r["blood_group"],
            "age": r["age"],
            "gender": r["gender"],
            "city": r["city"],
            "state": r["state"],
            "country": r["country"],
            "availability": r["availability"],
            "last_donation_date": r["last_donation_date"],
            "phone_masked": mask_phone(r["phone"]),
            "distance_km": None,
        }
        if my_lat is not None and r["latitude"] is not None:
            item["distance_km"] = round(
                haversine_km(my_lat, my_lng, r["latitude"], r["longitude"]), 1
            )
        results.append(item)

    # Fully available donors first, then by distance (if known) or name.
    def sort_key(x):
        no_dist = x["distance_km"] is None
        return (x["availability"] != "available",
                no_dist if my_lat is not None else False,
                x["distance_km"] or 0,
                x["name"].lower())

    results.sort(key=sort_key)
    return jsonify(results[:100])


@app.get("/api/donors/<int:donor_id>/contact")
def donor_contact(donor_id):
    """Full contact details are only returned when the receiver explicitly asks."""
    if rate_limited("contact", 30, 3600):
        return jsonify({"error": "Too many requests. Please try again later."}), 429
    r = get_db().execute(
        "SELECT phone, email FROM donors WHERE id = ? AND availability != 'unavailable'",
        (donor_id,),
    ).fetchone()
    if r is None:
        return jsonify({"error": "Donor not found."}), 404
    return jsonify({"phone": r["phone"], "email": r["email"]})


def authorised_donor(donor_id):
    token = request.headers.get("X-Manage-Key", "") or clean((request.get_json(silent=True) or {}).get("manage_key"))
    row = get_db().execute("SELECT * FROM donors WHERE id = ?", (donor_id,)).fetchone()
    if row is None or not row["token_hash"] or not token:
        return None
    return row if hmac.compare_digest(row["token_hash"], hash_token(token)) else None


@app.patch("/api/donors/<int:donor_id>")
def update_donor(donor_id):
    if rate_limited("manage", 30, 3600):
        return jsonify({"error": "Too many attempts. Try again later."}), 429
    row = authorised_donor(donor_id)
    if row is None:
        return jsonify({"error": "Donor ID or manage key is incorrect."}), 403
    data = request.get_json(silent=True) or {}
    db = get_db()
    msg = []

    if data.get("donated_today"):
        today = date.today()
        nxt = today + timedelta(days=GAP_DAYS.get(row["gender"], 120))
        db.execute("UPDATE donors SET last_donation_date=?, next_eligible_date=? WHERE id=?",
                   (today.isoformat(), nxt.isoformat(), donor_id))
        msg.append(f"Thank you for donating! You will be hidden from search until {nxt.isoformat()}.")

    if "availability" in data:
        if data["availability"] not in AVAILABILITY:
            return jsonify({"error": "Invalid availability."}), 400
        db.execute("UPDATE donors SET availability=? WHERE id=?", (data["availability"], donor_id))
        msg.append(f"Availability set to “{data['availability']}”.")

    if not msg:
        return jsonify({"error": "Nothing to update."}), 400
    db.commit()
    return jsonify({"message": " ".join(msg)})


@app.delete("/api/donors/<int:donor_id>")
def delete_donor(donor_id):
    if rate_limited("manage", 30, 3600):
        return jsonify({"error": "Too many attempts. Try again later."}), 429
    if authorised_donor(donor_id) is None:
        return jsonify({"error": "Donor ID or manage key is incorrect."}), 403
    db = get_db()
    db.execute("DELETE FROM donors WHERE id = ?", (donor_id,))
    db.commit()
    return jsonify({"message": "Your details have been permanently deleted."})


init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000)