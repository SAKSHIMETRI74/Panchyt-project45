# Gram Panchayat Digital Grievance Portal (ಗ್ರಾಮ ಪಂಚಾಯತ್ ಡಿಜಿಟಲ್ ಪೋರ್ಟಲ್)

Bilingual (English + Kannada) grievance portal: citizens file and track complaints, officials (PDO) manage them.

## Features
- Citizen portal: phone-OTP registration, login, file grievance (photo + GPS), live status, appeals, printable statement
- Official portal: priority queue, status updates, assignments, strike policy, appeals desk, full audit trail
- Public tracking by Tracking ID (shows no personal data)
- Security: hashed passwords, login lockout, rate limits, private photo storage (EXIF stripped), CSP headers

## Run locally
```
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
python app.py                  # http://127.0.0.1:5000
```
Local default accounts: Official `9876543210` / `PanchayatAdmin@2026`, demo citizen `9845012345` / `Citizen@123`.

## Deploy on Render (public website)
1. Push this folder to GitHub (the `.gitignore` keeps the database and uploads out).
2. Render -> your Web Service -> Settings:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 1 --threads 4 --timeout 60 --preload`
   - Health Check Path: `/healthz`
3. Render -> Environment, add:
   | Key | Value |
   | --- | --- |
   | `PYTHON_VERSION` | `3.12.8` |
   | `FLASK_ENV` | `production` |
   | `SECRET_KEY` | any long random string |
   | `DEFAULT_ADMIN_PASSWORD` | your own strong password for the official login |
   | `HELPLINE_NUMBER` | your real helpline (optional) |
4. Deploy. In production the official login uses **your** `DEFAULT_ADMIN_PASSWORD` (there is no public default).

## Before real public use
- **SMS OTP:** by default OTP is shown on screen (demo mode). For real users set `SMS_GATEWAY_PROVIDER=fast2sms` and `FAST2SMS_API_KEY`.
- **Sample data:** set `SEED_DEMO_DATA=0` to start with an empty system (no sample complaints).
- **Data storage:** Render's free disk is wiped on every deploy. Use a Render persistent disk (paid) mounted at `/var/data` and set `DATA_DIR=/var/data`, or move to PostgreSQL.

## Tests
`python -m unittest discover -s tests`
