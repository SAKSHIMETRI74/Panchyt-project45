import io
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["SEED_DEMO_DATA"] = "1"
os.environ["SMS_GATEWAY_PROVIDER"] = "mock"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402
import app as portal  # noqa: E402


def make_photo(kind="good"):
    import random
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    if kind == "good":
        random.seed(1)
        px = img.load()
        for x in range(400):
            for y in range(300):
                px[x, y] = (random.randint(40, 200), random.randint(60, 180), random.randint(30, 150))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


class PortalTests(unittest.TestCase):
    def setUp(self):
        portal.app.config["TESTING"] = True
        portal._rate_hits.clear()
        self.c = portal.app.test_client()

    def login(self, phone, pw, portal_name="citizen"):
        return self.c.post("/api/auth/login", json={"phone": phone, "password": pw, "portal": portal_name})

    def test_public_pages_load(self):
        for url in ["/", "/track", "/citizen/login", "/admin/login", "/healthz"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertEqual(self.c.get("/nope").status_code, 404)
        self.assertIn("ಕನ್ನಡ", self.c.get("/").get_data(as_text=True))

    def test_landing_has_nonzero_stats(self):
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("Resolved", html)
        self.assertNotIn("<b>0</b><span>Resolved", html)

    def test_language_switch(self):
        self.c.get("/set-language/kn")
        self.assertIn("ಗ್ರಾಮ ಪಂಚಾಯತ್", self.c.get("/").get_data(as_text=True))

    def test_no_credentials_shown_publicly(self):
        for url in ["/", "/admin/login", "/citizen/login"]:
            html = self.c.get(url).get_data(as_text=True)
            self.assertNotIn("PanchayatAdmin@2026", html)
            self.assertNotIn("Citizen@123", html)

    def test_admin_pages_require_login(self):
        self.assertEqual(self.c.get("/admin/dashboard").status_code, 302)
        self.assertEqual(self.c.get("/citizen/dashboard").status_code, 302)
        self.assertEqual(self.c.post("/api/admin/complaints/1/update", json={"status": "Resolved"}).status_code, 403)

    def test_citizen_cannot_use_official_portal(self):
        r = self.login("9845012345", "Citizen@123", "admin")
        self.assertEqual(r.status_code, 403)

    def test_citizen_dashboard_and_statement(self):
        self.assertEqual(self.login("9845012345", "Citizen@123").status_code, 200)
        self.assertEqual(self.c.get("/citizen/dashboard").status_code, 200)
        self.assertEqual(self.c.get("/citizen/statement").status_code, 200)
        self.assertEqual(self.c.get("/admin/dashboard").status_code, 302)

    def test_admin_dashboard(self):
        r = self.login("9876543210", "PanchayatAdmin@2026", "admin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/admin/dashboard").status_code, 200)

    def test_wrong_password_lockout(self):
        for _ in range(5):
            self.assertEqual(self.login("9845012345", "wrong").status_code, 401)
        self.assertEqual(self.login("9845012345", "Citizen@123").status_code, 429)
        conn = portal.get_db()
        conn.execute("UPDATE users SET lockout_until = NULL, failed_login_attempts = 0 WHERE phone='9845012345'")
        conn.commit(); conn.close()

    def test_register_with_otp_flow(self):
        r = self.c.post("/api/auth/otp/send", json={"phone": "9811223344"}).get_json()
        self.assertTrue(r["success"]); otp = r["dev_otp"]
        bad = self.c.post("/api/auth/otp/verify", json={"phone": "9811223344", "otp": "000000"})
        self.assertEqual(bad.status_code, 400)
        v = self.c.post("/api/auth/otp/verify", json={"phone": "9811223344", "otp": otp}).get_json()
        self.assertTrue(v["success"])
        reg = self.c.post("/api/auth/register", json={
            "phone": "9811223344", "verification_token": v["verification_token"],
            "full_name": "Test Citizen", "password": "secret123", "ward": "Ward-2"})
        self.assertEqual(reg.status_code, 200)
        self.assertEqual(self.c.get("/citizen/dashboard").status_code, 200)

    def test_invalid_phone_rejected(self):
        self.assertEqual(self.c.post("/api/auth/otp/send", json={"phone": "12345"}).status_code, 400)

    def test_cannot_take_over_existing_account_with_demo_otp(self):
        r = self.c.post("/api/auth/otp/send", json={"phone": "9845012345"}).get_json()
        v = self.c.post("/api/auth/otp/verify", json={"phone": "9845012345", "otp": r["dev_otp"]}).get_json()
        reg = self.c.post("/api/auth/register", json={
            "phone": "9845012345", "verification_token": v["verification_token"],
            "full_name": "Hacker", "password": "newpass123"})
        self.assertEqual(reg.status_code, 409)

    def test_complaint_with_blank_photo_rejected_and_good_photo_accepted(self):
        self.login("9845012345", "Citizen@123")
        base = {"title": "Pothole near school", "category": "Village Roads", "severity": "High",
                "description": "Large pothole causing accidents on the main road."}
        bad = self.c.post("/api/complaints/submit", data={**base, "photo": (make_photo("blank"), "p.png")},
                          content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        self.assertTrue(bad.get_json().get("photo_rejected"))
        fake = self.c.post("/api/complaints/submit", data={**base, "photo": (io.BytesIO(b"not an image" * 100), "p.jpg")},
                           content_type="multipart/form-data")
        self.assertEqual(fake.status_code, 400)
        ok = self.c.post("/api/complaints/submit", data={**base, "photo": (make_photo("good"), "p.png")},
                         content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        tid = ok.get_json()["tracking_id"]
        t = self.c.get(f"/api/complaints/track/{tid}").get_json()
        self.assertEqual(t["complaint"]["status"], "Pending")
        self.assertNotIn("citizen_phone", t["complaint"])
        self.assertNotIn("citizen_name", t["complaint"])

    def test_track_by_phone_no_longer_leaks(self):
        self.assertEqual(self.c.get("/api/complaints/track/9845012345").status_code, 404)

    def test_gps_outside_region_rejected(self):
        self.login("9845012345", "Citizen@123")
        r = self.c.post("/api/complaints/submit", data={
            "title": "Water leakage", "category": "Drinking Water", "severity": "Low",
            "description": "Pipe is leaking since three days.", "latitude": "51.5", "longitude": "-0.12"})
        self.assertEqual(r.status_code, 400)

    def test_admin_reject_and_appeal_flow(self):
        self.login("9845012345", "Citizen@123")
        ok = self.c.post("/api/complaints/submit", data={
            "title": "Streetlight broken", "category": "Streetlights", "severity": "Low",
            "description": "The streetlight at the corner is not working."}).get_json()
        conn = portal.get_db()
        cid = conn.execute("SELECT id FROM complaints WHERE tracking_id=?", (ok["tracking_id"],)).fetchone()["id"]
        conn.close()
        # citizen cannot update status
        self.assertEqual(self.c.post(f"/api/admin/complaints/{cid}/update", json={"status": "Resolved"}).status_code, 403)
        self.c.get("/logout")
        self.login("9876543210", "PanchayatAdmin@2026", "admin")
        self.assertEqual(self.c.post(f"/api/admin/complaints/{cid}/update", json={"status": "Rejected"}).status_code, 400)
        r = self.c.post(f"/api/admin/complaints/{cid}/update",
                        json={"status": "Rejected", "officer_remarks": "Light is working, checked.", "mark_false": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Strike 1/3", r.get_json()["message"])
        audit = self.c.get(f"/api/admin/complaints/{cid}/audit").get_json()
        self.assertGreaterEqual(len(audit["audit_trail"]), 3)
        self.c.get("/logout")
        self.login("9845012345", "Citizen@123")
        ap = self.c.post(f"/api/complaints/{cid}/appeal", json={"reason": "The light has been off for days, please recheck."})
        self.assertEqual(ap.status_code, 200)
        self.assertEqual(self.c.post(f"/api/complaints/{cid}/appeal", json={"reason": "Second duplicate appeal here."}).status_code, 409)
        self.c.get("/logout")
        self.login("9876543210", "PanchayatAdmin@2026", "admin")
        page = self.c.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn("data-appeal-id", page)
        conn = portal.get_db()
        aid = conn.execute("SELECT id FROM complaint_appeals WHERE complaint_id=?", (cid,)).fetchone()["id"]
        conn.close()
        d = self.c.post(f"/api/admin/appeals/{aid}/decide", json={"decision": "UPHELD", "notes": "Re-inspection approved."})
        self.assertEqual(d.status_code, 200)
        conn = portal.get_db()
        row = conn.execute("SELECT status FROM complaints WHERE id=?", (cid,)).fetchone()
        strikes = conn.execute("SELECT false_strikes FROM users WHERE phone='9845012345'").fetchone()[0]
        conn.close()
        self.assertEqual(row["status"], "Pending")
        self.assertEqual(strikes, 0)

    def test_photo_access_control(self):
        self.assertEqual(self.c.get("/media/complaint/1/x.jpg").status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
