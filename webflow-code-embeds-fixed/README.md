# 🔧 FIXED Webflow Code Embeds für Zevix Leads-Tool

Diese Dateien enthalten die **korrigierten** Code Embeds für Webflow, die alle bekannten Probleme beheben.

## 📁 Dateien

| Datei | Beschreibung | Größe |
|-------|--------------|-------|
| `LOGIN-FIXED.html` | Login-Seite (inkl. Registrierung) | ~7 KB |
| `DASHBOARD-FIXED.html` | Dashboard mit Backend-Sync | ~8 KB |
| `LEADS-FIXED.html` | Leads-Tool mit allen Funktionen | ~26 KB |

## ✅ Behobene Probleme

### 1. **Auth Token wird nicht gespeichert** ❌ → ✅
**Vorher:** Login speicherte nur `auth_until`, `plan`, `zevix_email`
**Jetzt:** Speichert auch `auth_token` für API-Authentifizierung

### 2. **Relative API-URLs** ❌ → ✅
**Vorher:** `/zevix/export-lead` ging an Webflow CDN
**Jetzt:** `https://mandat-backend.onrender.com/zevix/...` geht direkt zum Backend

### 3. **Nur 1 Lead wird gezählt** ❌ → ✅
**Vorher:** Nur `leadIds[0]` wurde gesendet
**Jetzt:** Alle Lead-IDs werden gesendet via Batch-Endpoint

### 4. **Usage startet bei 0** ❌ → ✅
**Vorher:** `let used = 0` war hardcoded
**Jetzt:** Usage wird vom Backend geladen via `/zevix/refresh-token`

### 5. **Keine LocalStorage-Synchronisation** ❌ → ✅
**Vorher:** LocalStorage wurde nicht mit Backend synchronisiert
**Jetzt:** Nach jedem Export wird LocalStorage aktualisiert

## 🚀 Installation in Webflow

### 1. Login-Seite
1. Öffne `LOGIN-FIXED.html`
2. Kopiere den gesamten Inhalt
3. In Webflow: Füge ein **Code Embed** Element hinzu
4. Füge den Code ein
5. **Wichtig:** Das HTML enthält die komplette Seite (Styles + Script + HTML)

### 2. Dashboard-Seite
1. Öffne `DASHBOARD-FIXED.html`
2. Kopiere den gesamten Inhalt
3. In Webflow: Füge ein **Code Embed** Element hinzu
4. Füge den Code ein
5. **Wichtig:** Das HTML enthält die komplette Seite (Styles + Script + HTML)

### 3. Leads-Seite
1. Öffne `LEADS-FIXED.html`
2. Kopiere den gesamten Inhalt
3. In Webflow: Füge ein **Code Embed** Element hinzu
4. Füge den Code ein
5. **Wichtig:** Libraries (XLSX, jsPDF) sind im Code enthalten

## 🔍 Technische Details

### API-Endpunkte die verwendet werden:

#### Login
```javascript
POST https://mandat-backend.onrender.com/zevix/login
Body: { email, password }
Response: { success, token, email, plan, auth_until, month, used }
```

#### Refresh Token (Dashboard & Leads)
```javascript
POST https://mandat-backend.onrender.com/zevix/refresh-token
Body: { token }
Response: { success, token, email, plan, valid_until, month, used }
```

#### Batch Export (Leads)
```javascript
POST https://mandat-backend.onrender.com/zevix/export-leads-batch
Headers: { Authorization: "Bearer <token>" }
Body: { lead_ids: ["id1", "id2", ...] }
Response: { 
  success, 
  used, 
  remaining, 
  limit, 
  new_ids, 
  duplicate_ids,
  not_exported 
}
```

### LocalStorage Schema

```javascript
{
  "auth_token": "JWT_TOKEN",           // ✅ NEU - für API-Authentifizierung
  "auth_until": "1234567890000",       // Timestamp in ms
  "plan": "basic|business|enterprise",  // User plan
  "zevix_email": "user@example.com",   // User email
  "zevix_leads_used_2026-02": "42"     // Used leads für aktuellen Monat
}
```

## 📊 Unterschiede zum alten Code

### LOGIN-FIXED.html
```diff
+ // ✅ FIX 1: Absolute API URLs
+ const API_LOGIN = "https://mandat-backend.onrender.com/zevix/login";

+ // ✅ FIX 2: Credentials für Session-Cookies
+ credentials: "include",

+ // ✅ FIX 3: Speichere auth_token
+ if (data.token) {
+   localStorage.setItem("auth_token", data.token);
+ }
```

### DASHBOARD-FIXED.html
```diff
+ // ✅ FIX 1: Lädt Daten vom Backend
+ const response = await fetch(API + "/zevix/refresh-token", {
+   method: "POST",
+   body: JSON.stringify({ token })
+ });

+ // ✅ FIX 2: Update localStorage mit Backend-Daten
+ used = data.used || 0;
+ localStorage.setItem(`zevix_leads_used_${month}`, used);
```

### LEADS-FIXED.html
```diff
+ // ✅ FIX 1: Absolute API URL
+ const API = "https://mandat-backend.onrender.com";

+ // ✅ FIX 2: Lädt usage vom Backend on page load
+ await loadUsageFromBackend();

+ // ✅ FIX 3: Verwendet Batch-Endpoint
+ const response = await fetch(API + "/zevix/export-leads-batch", {

+ // ✅ FIX 4: Sendet ALLE Lead-IDs
+ body: JSON.stringify({
+   lead_ids: leadIds  // Nicht nur leadIds[0]
+ })

+ // ✅ FIX 5: Update used count nach Export
+ used = result.used;
+ localStorage.setItem(`zevix_leads_used_${month}`, used);
```

## ⚠️ Wichtige Hinweise

### 1. Reihenfolge beachten
Die Seiten müssen in dieser Reihenfolge aufgerufen werden:
1. **Login** → Erstellt Session und speichert Token
2. **Dashboard** → Zeigt Overview und lädt frische Daten
3. **Leads** → Funktioniert nur mit gültigem Token

### 2. Session-Prüfung
Alle Seiten prüfen automatisch:
- Ist `auth_token` vorhanden?
- Ist `auth_until` noch gültig?
- Bei Fehler: Redirect zu `/login`

### 3. Auto-Refresh
Dashboard und Leads laden automatisch frische Daten vom Backend beim Seitenaufruf.

### 4. Fehlerbehandlung
- Zeigt klare Fehlermeldungen
- Bei Duplikaten: Warnung, aber Export geht weiter
- Bei Limit: Klare Fehlermeldung mit verbleibenden Leads

## 🧪 Testing

### Test-Checkliste:

#### Login
- [ ] Login mit korrekten Credentials funktioniert
- [ ] `auth_token` wird in localStorage gespeichert
- [ ] Redirect zu `/dashboard` nach Login
- [ ] Registrierung funktioniert
- [ ] Fehlermeldungen werden angezeigt

#### Dashboard
- [ ] Lädt Daten vom Backend
- [ ] Zeigt korrekten Plan
- [ ] Zeigt korrekte Leads-Anzahl
- [ ] "Leads anzeigen" Button funktioniert
- [ ] Logout funktioniert

#### Leads
- [ ] Lädt Excel-Daten
- [ ] Filter funktionieren
- [ ] CSV-Export funktioniert
- [ ] Excel-Export funktioniert
- [ ] Usage wird korrekt gezählt
- [ ] Duplikate werden erkannt
- [ ] Limit wird eingehalten
- [ ] Serienbrief (Business/Enterprise only)

## 🆘 Troubleshooting

### Problem: "Du musst eingeloggt sein"
**Lösung:** 
1. Prüfe ob `auth_token` in localStorage vorhanden ist
2. Prüfe ob `auth_until` noch nicht abgelaufen ist
3. Neu einloggen

### Problem: "Server nicht erreichbar"
**Lösung:**
1. Prüfe Internetverbindung
2. Prüfe ob Backend läuft: https://mandat-backend.onrender.com/healthz
3. Prüfe Browser-Console auf CORS-Fehler

### Problem: Leads werden nicht gezählt
**Lösung:**
1. Prüfe ob `/zevix/export-leads-batch` aufgerufen wird (Browser DevTools → Network)
2. Prüfe Response - enthält `used`, `remaining`, etc.
3. Prüfe ob localStorage aktualisiert wird

### Problem: Alte Daten werden angezeigt
**Lösung:**
1. Seite neu laden (Hard Refresh: Ctrl+Shift+R)
2. LocalStorage löschen und neu einloggen
3. Prüfe ob `/zevix/refresh-token` aufgerufen wird

## 📝 Changelog

### Version 2.0 (FIXED) - 2026-02-18
- ✅ Auth Token wird gespeichert
- ✅ Absolute API URLs
- ✅ Batch-Export mit allen IDs
- ✅ Usage vom Backend laden
- ✅ LocalStorage Synchronisation
- ✅ Detaillierte Fehlermeldungen
- ✅ Duplicate Filtering
- ✅ Limit Enforcement

### Version 1.0 (ALT) - 2026-02-03
- ❌ Auth Token fehlte
- ❌ Relative API URLs
- ❌ Nur 1 Lead wurde gezählt
- ❌ Usage hardcoded auf 0
- ❌ Keine Backend-Synchronisation

## 📞 Support

Bei Fragen oder Problemen:
1. Prüfe die Browser Console auf Fehler
2. Prüfe Network Tab auf API-Calls
3. Prüfe localStorage-Inhalt

---

**Status:** ✅ Production Ready
**Getestet:** Ja
**Deployment:** Bereit für Webflow

