# 🚀 Quick Start Guide - Webflow Code Embeds (FIXED)

## Was ist neu?

Diese **FIXED** Versionen beheben alle bekannten Probleme:
- ✅ Auth Token wird gespeichert
- ✅ Alle Leads werden gezählt (nicht nur der erste)
- ✅ Usage wird vom Backend synchronisiert
- ✅ Duplikate werden automatisch gefiltert

## 📋 Installation (3 Schritte)

### Schritt 1: Login-Seite
```
1. Öffne LOGIN-FIXED.html
2. Kopiere ALLES (Ctrl+A, Ctrl+C)
3. In Webflow: Code Embed Element hinzufügen
4. Einfügen (Ctrl+V)
5. Speichern
```

### Schritt 2: Dashboard-Seite
```
1. Öffne DASHBOARD-FIXED.html
2. Kopiere ALLES (Ctrl+A, Ctrl+C)
3. In Webflow: Code Embed Element hinzufügen
4. Einfügen (Ctrl+V)
5. Speichern
```

### Schritt 3: Leads-Seite
```
1. Öffne LEADS-FIXED.html
2. Kopiere ALLES (Ctrl+A, Ctrl+C)
3. In Webflow: Code Embed Element hinzufügen
4. Einfügen (Ctrl+V)
5. Speichern
```

## ✅ Fertig!

Nach dem Publish in Webflow sollte alles funktionieren:
- Login speichert Token
- Dashboard zeigt korrekte Leads-Anzahl
- Leads-Export zählt alle ausgewählten Leads

## 🧪 Testen

### Test 1: Login
1. Gehe zu `/login`
2. Logge dich ein
3. Öffne Browser Console (F12)
4. Tippe: `localStorage.getItem("auth_token")`
5. ✅ Sollte einen Token zeigen (nicht `null`)

### Test 2: Dashboard
1. Gehe zu `/dashboard`
2. Prüfe ob Leads-Anzahl korrekt ist
3. ✅ Sollte echte Daten vom Backend zeigen

### Test 3: Leads Export
1. Gehe zu `/leads`
2. Wähle mehrere Leads aus (z.B. 5 Leads)
3. Klicke "CSV exportieren"
4. Prüfe die Meldung
5. ✅ Sollte "5 Lead(s) exportiert" zeigen (nicht nur 1)

## ❓ Probleme?

### "Du musst eingeloggt sein"
➡️ Neu einloggen - Token ist abgelaufen

### "Server nicht erreichbar"
➡️ Backend prüfen: https://mandat-backend.onrender.com/healthz

### Leads werden nicht gezählt
➡️ Browser Console öffnen (F12) → Network Tab → Prüfe ob `/zevix/export-leads-batch` aufgerufen wird

## 📖 Mehr Infos

Siehe `README.md` für:
- Detaillierte technische Dokumentation
- API-Endpunkte
- Troubleshooting
- Changelog

---

**Ready to deploy!** 🎉
