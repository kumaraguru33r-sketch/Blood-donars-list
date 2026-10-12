# Blood Donor Finder

Donors register with their details, blood group, location and a doctor-style health screening. Receivers search by blood group and location (or "use my location" to sort by nearest).

## Run

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 (the `donors.db` SQLite file is created automatically).

## Features

- All 8 ABO/Rh blood groups plus the rare Bombay (hh) type
- Registration with name, age, gender, weight, hemoglobin, last donation date, smoking, alcohol, availability
- Browser location access (optional) so receivers can sort donors by distance
- Pre-donation screening: age 18-65, weight 45+ kg, hemoglobin 12.5+ g/dL, donation gap (90 days men / 120 days women), recent illness, vaccination, tattoo/piercing, surgery, transfusion, malaria, pregnancy, medicines, alcohol in last 24 h, sleep, and permanent deferrals
- Not-eligible people get the reasons and the date they can try again; they are not listed
- **Medical answers are never stored** (only the non-sensitive profile and the date of screening)
- Availability: Available / Emergency only / Not available; after "I donated today" the donor is hidden until eligible again
- Donors get a one-time manage key to change availability or delete their data
- Phone numbers masked in results until the receiver taps "Show contact" (rate limited)

## Structure

- `app.py` - Flask API + SQLite; the screening questions and rules live in `SCREENING` at the top
- `static/index.html` - frontend (Find a Donor / Become a Donor / My Listing)

## API

- `GET    /api/blood-groups`
- `GET    /api/screening` - the questions shown on the form
- `POST   /api/donors` - register (201 listed, 422 not eligible with reasons, 400 validation errors)
- `GET    /api/donors?blood_group=O-&country=India&state=Tamil Nadu&city=Chennai&lat=..&lng=..`
- `GET    /api/donors/<id>/contact`
- `PATCH  /api/donors/<id>` - header `X-Manage-Key`; body `{"availability": "..."}` or `{"donated_today": true}`
- `DELETE /api/donors/<id>` - header `X-Manage-Key`

## Before going live

- The eligibility rules follow common blood-bank guidelines but vary by country and centre. Have a doctor or your blood bank review `SCREENING` and the thresholds. This is a self-screening aid; the doctor at the donation centre always decides.
- Deploy behind HTTPS (gunicorn + nginx, or Render/Railway), keep `debug` off.
- Replace the in-memory rate limiter with Flask-Limiter + Redis, and add a CAPTCHA.
- Add phone OTP verification so fake numbers cannot be registered.
- Donor data is personal data: publish a privacy policy and follow local law (India's DPDP Act, GDPR, etc.).
- For city names, consider a dropdown from a places API so spellings match.