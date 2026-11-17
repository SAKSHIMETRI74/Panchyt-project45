import os
import re
import secrets
from datetime import timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from database import utc_now, utc_str

INDIAN_PHONE_REGEX = re.compile(r"^[6-9]\d{9}$")


class MockSMSProvider:
    """Demo provider: prints the OTP in the server log (and the app shows it on screen)."""
    is_mock = True

    def send_otp(self, phone: str, otp: str) -> bool:
        print(f"[SMS SIMULATION] +91 {phone} -> OTP {otp}")
        return True


class Fast2SMSProvider:
    """Real SMS via Fast2SMS. Needs FAST2SMS_API_KEY."""
    is_mock = False

    def __init__(self, api_key: str):
        self.api_key = api_key

    def send_otp(self, phone: str, otp: str) -> bool:
        if not self.api_key:
            print("[SMS] FAST2SMS_API_KEY missing - cannot send real SMS.")
            return False
        try:
            import json
            import urllib.parse
            import urllib.request
            data = urllib.parse.urlencode({
                "authorization": self.api_key,
                "variables_values": otp,
                "route": "otp",
                "numbers": phone,
            }).encode("utf-8")
            req = urllib.request.Request("https://www.fast2sms.com/dev/bulkV2", data=data)
            with urllib.request.urlopen(req, timeout=8) as response:
                res = json.loads(response.read().decode())
                return bool(res.get("return", False))
        except Exception as e:  # network / API failure
            print(f"[SMS Gateway Error] {e}")
            return False


class OTPService:
    def __init__(self, provider_type="mock"):
        if provider_type == "fast2sms":
            self.provider = Fast2SMSProvider(os.environ.get("FAST2SMS_API_KEY", ""))
        else:
            self.provider = MockSMSProvider()

    @property
    def is_mock(self):
        return getattr(self.provider, "is_mock", False)

    @staticmethod
    def clean_phone(phone) -> str:
        phone = re.sub(r"[^\d]", "", str(phone or ""))
        if phone.startswith("91") and len(phone) == 12:
            phone = phone[2:]
        return phone

    def request_otp(self, conn, raw_phone):
        phone = self.clean_phone(raw_phone)
        if not INDIAN_PHONE_REGEX.match(phone):
            return False, "Invalid Indian mobile number. Must be 10 digits starting with 6, 7, 8, or 9."

        cur = conn.cursor()
        cutoff = utc_str(utc_now() - timedelta(minutes=15))
        cur.execute("SELECT COUNT(*) AS cnt FROM phone_otps WHERE phone = ? AND created_at > ?", (phone, cutoff))
        row = cur.fetchone()
        if row and row["cnt"] >= 3:
            return False, "Too many OTP requests for this number. Please wait 15 minutes."

        raw_otp = f"{secrets.randbelow(900000) + 100000}"
        cur.execute(
            "INSERT INTO phone_otps (phone, otp_hash, expires_at) VALUES (?, ?, ?)",
            (phone, generate_password_hash(raw_otp), utc_str(utc_now() + timedelta(minutes=5))),
        )
        conn.commit()

        if not self.provider.send_otp(phone, raw_otp):
            return False, "Could not send the SMS right now. Please try again."

        result = {"message": f"OTP sent to +91 {phone[:2]}******{phone[-2:]}"}
        if self.is_mock:  # NEVER expose the OTP when a real SMS provider is used
            result["dev_otp"] = raw_otp
        return True, result

    def verify_otp(self, conn, raw_phone, submitted_otp):
        phone = self.clean_phone(raw_phone)
        submitted_otp = str(submitted_otp or "").strip()
        if not phone or not submitted_otp:
            return False, "Phone and OTP are required."

        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM phone_otps WHERE phone = ? AND is_verified = 0 ORDER BY id DESC LIMIT 1",
            (phone,),
        )
        record = cur.fetchone()
        if not record:
            return False, "No pending OTP found. Please request a new OTP."
        if record["expires_at"] < utc_str():
            return False, "OTP has expired. Please request a fresh OTP."
        if record["attempts"] >= 3:
            return False, "Maximum attempts exceeded. Please request a new OTP."

        if not check_password_hash(record["otp_hash"], submitted_otp):
            cur.execute("UPDATE phone_otps SET attempts = attempts + 1 WHERE id = ?", (record["id"],))
            conn.commit()
            remaining = max(0, 2 - record["attempts"])
            return False, f"Incorrect OTP. {remaining} attempt(s) remaining."

        token = secrets.token_hex(24)
        cur.execute(
            "UPDATE phone_otps SET is_verified = 1, verification_token = ? WHERE id = ?",
            (token, record["id"]),
        )
        conn.commit()
        return True, {"verification_token": token, "message": "Mobile number verified."}
