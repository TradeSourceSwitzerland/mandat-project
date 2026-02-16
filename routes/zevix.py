 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/routes/zevix.py b/routes/zevix.py
index a6f14c526dd577c65e436fcb7c250aeacbd7bfe7..68176c42779d185dee6bc5a6ccc70b7a1bfea6a8 100644
--- a/routes/zevix.py
+++ b/routes/zevix.py
@@ -1,77 +1,84 @@
 import os
+import json
 import psycopg
 from psycopg.rows import dict_row
 import bcrypt
 from flask import Blueprint, jsonify, request
 from datetime import datetime
 
 # ----------------------------
 # CONFIG
 # ----------------------------
 DATABASE_URL = os.getenv("DATABASE_URL")
 
 # ----------------------------
 # DATABASE CONNECTION
 # ----------------------------
 def get_conn():
     if not DATABASE_URL:
         raise RuntimeError("DATABASE_URL missing")
 
     return psycopg.connect(
         f"{DATABASE_URL}?sslmode=require",
         row_factory=dict_row
     )
 
 # ----------------------------
 # INIT DB (lightweight)
 # ----------------------------
 def init_db():
     with get_conn() as conn:
         with conn.cursor() as cur:
             # Erstellung der Tabelle 'users', wenn sie nicht existiert
             cur.execute("""
                 CREATE TABLE IF NOT EXISTS users (
                     id SERIAL PRIMARY KEY,
                     email TEXT UNIQUE NOT NULL,
                     password TEXT NOT NULL,
                     plan TEXT,
                     valid_until BIGINT
                 );
             """)
 
             # Erstellung der Tabelle 'usage', wenn sie nicht existiert
             cur.execute("""
                 CREATE TABLE IF NOT EXISTS usage (
                     id SERIAL PRIMARY KEY,
                     user_email TEXT NOT NULL,
                     month TEXT NOT NULL,
                     used INTEGER DEFAULT 0,
+                    used_ids JSONB DEFAULT '[]'::jsonb,
                     UNIQUE(user_email, month)
                 );
             """)
 
+            cur.execute("""
+                ALTER TABLE usage
+                ADD COLUMN IF NOT EXISTS used_ids JSONB DEFAULT '[]'::jsonb
+            """)
+
         conn.commit()
 
 # ----------------------------
 # BLUEPRINT
 # ----------------------------
 zevix_bp = Blueprint("zevix", __name__)
 
 # ----------------------------
 # REGISTER
 # ----------------------------
 @zevix_bp.route("/zevix/register", methods=["POST"])
 def register():
     data = request.get_json(silent=True) or {}
     email = (data.get("email") or "").strip().lower()
     password = data.get("password") or ""
 
     if not email or not password:
         return jsonify({"success": False}), 400
 
     hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
 
     try:
         with get_conn() as conn:
             with conn.cursor() as cur:
                 cur.execute(
@@ -106,109 +113,157 @@ def login():
             user = cur.fetchone()
 
             if not user:
                 return jsonify({"success": False}), 404
 
             # Passwort-Überprüfung
             if not bcrypt.checkpw(password.encode(), user["password"].encode()):
                 return jsonify({"success": False}), 401
 
             # auth_until auf 30 Tage setzen, falls nicht gesetzt
             auth_until = user.get("valid_until")
             if not auth_until:
                 auth_until = int((datetime.now().timestamp() + 30*24*60*60) * 1000)
 
             month = datetime.now().strftime("%Y-%m")
 
             # Überprüfen, ob der Benutzer für den aktuellen Monat schon Daten hat
             cur.execute("""
                 INSERT INTO usage (user_email,month,used)
                 VALUES (%s,%s,0)
                 ON CONFLICT (user_email,month) DO NOTHING
             """, (email, month))
 
             # Abrufen der verbrauchten Leads für den aktuellen Monat
             cur.execute(
-                "SELECT used FROM usage WHERE user_email=%s AND month=%s",
+                "SELECT used, used_ids FROM usage WHERE user_email=%s AND month=%s",
                 (email, month)
             )
-            used = cur.fetchone()["used"]
+            usage = cur.fetchone() or {}
+            used = int(usage.get("used") or 0)
+            used_ids = usage.get("used_ids") or []
 
         conn.commit()
 
     # Rückgabe der Login-Daten, einschließlich verbrauchter Leads
     return jsonify({
         "success": True,
         "email": email,
         "plan": user.get("plan"),
         "auth_until": auth_until,
         "month": month,
-        "used": used  # Gebe die verbrauchten Leads zurück
+        "used": used,
+        "used_ids": used_ids
     })
 
 
 # ----------------------------
 # LEAD CONSUMPTION
 # ----------------------------
 @zevix_bp.route("/zevix/consume-leads", methods=["POST"])
 def consume_leads():
     data = request.get_json(silent=True) or {}
-    email = data.get("email")
+    email = (data.get("email") or "").strip().lower()
     leads_count = data.get("leads_count")
+    lead_ids = data.get("lead_ids") or []
 
-    if not email or leads_count is None:
+    if not email:
         return jsonify({"success": False, "message": "Invalid data"}), 400
 
+    if not isinstance(lead_ids, list):
+        return jsonify({"success": False, "message": "lead_ids must be a list"}), 400
+
+    normalized_ids = []
+    for lead_id in lead_ids:
+        if lead_id is None:
+            continue
+        normalized = str(lead_id).strip().lower()
+        if normalized:
+            normalized_ids.append(normalized)
+
     month = datetime.now().strftime("%Y-%m")
 
     with get_conn() as conn:
         with conn.cursor() as cur:
             # Abrufen der aktuellen verbrauchten Leads
-            cur.execute("SELECT used FROM usage WHERE user_email=%s AND month=%s", (email, month))
+            cur.execute(
+                "SELECT used, used_ids FROM usage WHERE user_email=%s AND month=%s",
+                (email, month)
+            )
             result = cur.fetchone()
 
             if result:
-                used = result["used"]
+                used = int(result.get("used") or 0)
+                stored_ids = set(result.get("used_ids") or [])
             else:
                 # Falls noch keine Leads für diesen Monat verbraucht wurden
                 cur.execute("""
-                    INSERT INTO usage (user_email, month, used)
-                    VALUES (%s, %s, 0)
+                    INSERT INTO usage (user_email, month, used, used_ids)
+                    VALUES (%s, %s, 0, '[]'::jsonb)
                     ON CONFLICT (user_email, month) DO NOTHING
                 """, (email, month))
                 used = 0
+                stored_ids = set()
+
+            newly_used = 0
+            for lead_id in normalized_ids:
+                if lead_id not in stored_ids:
+                    stored_ids.add(lead_id)
+                    newly_used += 1
+
+            if leads_count is not None:
+                try:
+                    leads_count_value = int(leads_count)
+                except (TypeError, ValueError):
+                    return jsonify({"success": False, "message": "leads_count must be numeric"}), 400
+
+                if leads_count_value < 0:
+                    return jsonify({"success": False, "message": "leads_count must be >= 0"}), 400
+
+                if not normalized_ids:
+                    newly_used = leads_count_value
+
+            new_used = used + newly_used
+
+            used_ids_json = json.dumps(sorted(stored_ids))
 
-            # Aktualisieren der verbrauchten Leads
-            new_used = used + leads_count
             cur.execute("""
                 UPDATE usage
-                SET used = %s
+                SET used = %s,
+                    used_ids = %s::jsonb
                 WHERE user_email = %s AND month = %s
-            """, (new_used, email, month))
+            """, (new_used, used_ids_json, email, month))
             conn.commit()
 
-    return jsonify({"success": True, "used": new_used, "message": "Leads consumed successfully"})
+    return jsonify({
+        "success": True,
+        "month": month,
+        "used": new_used,
+        "newly_used": newly_used,
+        "used_ids": sorted(stored_ids),
+        "message": "Leads consumed successfully"
+    })
 
 
 # ----------------------------
 # OPTIONAL ONE-TIME MIGRATION
 # ----------------------------
 @zevix_bp.route("/__fix_db_once")
 def fix_db_once():
     with get_conn() as conn:
         with conn.cursor() as cur:
             cur.execute("""
                 SELECT column_name
                 FROM information_schema.columns
                 WHERE table_name='users' AND column_name='plan'
             """)
 
             if not cur.fetchone():
                 cur.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'basic'")
                 conn.commit()
                 return jsonify({"status": "created"})
 
     return jsonify({"status": "ok"})
 
 
 # ----------------------------
 # INIT DB ON IMPORT
 
EOF
)
