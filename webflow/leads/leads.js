/**
 * LEADS-TOOL JavaScript
 * Extracted from LEADS-FIXED.html for modular Webflow embed
 * 
 * Dependencies:
 * - XLSX (https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js)
 * - jsPDF (https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js)
 */

document.addEventListener("DOMContentLoaded", () => {

  // ✅ FIX 1: Absolute API URL
  const API = "https://mandat-backend.onrender.com";

  const status = document.getElementById("status");
  const statusText = document.getElementById("statusText");
  const filteredLeadsEl = document.getElementById("filteredLeads");
  const willExportEl = document.getElementById("willExport");
  const remainingLeadsEl = document.getElementById("remainingLeads");
  const filterKanton = document.getElementById("filterKanton");
  const filterOrt = document.getElementById("filterOrt");
  const filterBranche = document.getElementById("filterBranche");
  const filterFrom = document.getElementById("filterFrom");
  const filterTo = document.getElementById("filterTo");
  const enableLetter = document.getElementById("enableLetter");
  const letterSection = document.getElementById("letterSection");

  const plan = localStorage.getItem("plan") || "basic";

  if (plan === "basic") {
    enableLetter.disabled = true;
    const toggleLabel = document.getElementById("toggleLabel");
    if (toggleLabel) toggleLabel.style.opacity = "0.5";
  }

  const brandClaim = document.getElementById("brandClaim");
  const senderBlock = document.getElementById("senderBlock");
  const letterText = document.getElementById("letterText");
  const sign1 = document.getElementById("sign1");

  const btnCSV = document.getElementById("btnCSV");
  const btnXLSX = document.getElementById("btnXLSX");
  const btnPDF = document.getElementById("btnPDF");

  /* ======================== SIGNATURE CANVAS ======================== */
  const canvas = document.getElementById("signaturePad");
  const ctx = canvas ? canvas.getContext("2d") : null;

  let drawing = false;
  let lastPoint = null;
  let initialized = false;

  function initSignaturePad() {
    if (initialized || !canvas) return;

    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;

    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#000";

    initialized = true;
  }

  function getPos(e) {
    const r = canvas.getBoundingClientRect();
    const p = e.touches ? e.touches[0] : e;
    return {
      x: p.clientX - r.left,
      y: p.clientY - r.top
    };
  }

  function drawSmooth(p) {
    if (!lastPoint) {
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      lastPoint = p;
      return;
    }

    if (Math.hypot(p.x - lastPoint.x, p.y - lastPoint.y) < 4) return;

    const mx = (p.x + lastPoint.x) / 2;
    const my = (p.y + lastPoint.y) / 2;

    ctx.quadraticCurveTo(lastPoint.x, lastPoint.y, mx, my);
    ctx.stroke();
    lastPoint = p;
  }

  function startDraw(e) {
    initSignaturePad();
    drawing = true;
    lastPoint = null;
    drawSmooth(getPos(e));
  }

  function moveDraw(e) {
    if (!drawing) return;
    drawSmooth(getPos(e));
  }

  function endDraw() {
    drawing = false;
    lastPoint = null;
    if (ctx) ctx.beginPath();
  }

  if (canvas) {
    canvas.addEventListener("mousedown", startDraw);
    canvas.addEventListener("mousemove", moveDraw);
    canvas.addEventListener("mouseup", endDraw);
    canvas.addEventListener("mouseleave", endDraw);

    canvas.addEventListener("touchstart", startDraw, { passive: true });
    canvas.addEventListener("touchmove", moveDraw, { passive: true });
    canvas.addEventListener("touchend", endDraw);
  }

  function isSignatureEmpty() {
    initSignaturePad();
    if (!ctx) return true;
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    for (let i = 3; i < data.length; i += 4) {
      if (data[i] !== 0) return false;
    }
    return true;
  }

  function clearSignature() {
    initSignaturePad();
    if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  window.clearSignature = clearSignature;

  function getSignatureDataURL() {
    initSignaturePad();
    if (isSignatureEmpty()) return null;
    return canvas.toDataURL("image/png");
  }

  /* ======================== AUTO-SIGNATURE ======================== */
  function generateAutoSignature() {
    const nameInput = document.getElementById("autoSigName");
    const name = nameInput ? nameInput.value.trim() : "";
    if (!name) {
      alert("Bitte gib einen Namen ein.");
      return;
    }

    initSignaturePad();
    if (!ctx) return;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const rect = canvas.getBoundingClientRect();
    ctx.font = "italic 28px 'Dancing Script', 'Brush Script MT', 'Segoe Script', cursive";
    ctx.fillStyle = "#1a1a2e";
    ctx.textBaseline = "middle";
    ctx.fillText(name, 20, rect.height / 2);
  }

  window.generateAutoSignature = generateAutoSignature;

  /* ======================== LETTER TOGGLE ======================== */
  if (enableLetter) {
    enableLetter.addEventListener("change", () => {
      if (letterSection) letterSection.classList.toggle("letter-section--visible", enableLetter.checked);
      if (enableLetter.checked) requestAnimationFrame(() => initSignaturePad());
    });
  }

  /* ======================== DATA SOURCES ======================== */
  const EXCEL_URLS = [
    "https://cdn.prod.website-files.com/697fc01635a17b168514ed0d/6981481c65c177045e58fbd2_shab_zefix_adressen_2026-02-03_0157.xlsx",
    "https://cdn.prod.website-files.com/697fc01635a17b168514ed0d/6981e013411c191e07751015_shab_zefix_adressen_2026-02-03_1144.xlsx"
  ];

  const AMTOVZ_URL =
    "https://cdn.prod.website-files.com/697fc01635a17b168514ed0d/6988e6d3f981ef94ec83994a_AMTOVZ_CSV_LV95.csv";

  let rows = [];
  let amtovzByPlzOrt = {};
  let amtovzByPlzOnly = {};

  // ✅ FIX 2: Load usage from backend
  let used = 0;
  const PLAN_LIMITS = {
    basic: 500,
    business: 1000,
    enterprise: 4500
  };
  const limit = PLAN_LIMITS[plan] || 500;

  // ✅ FIX 3: Load usage from backend on page load
  async function loadUsageFromBackend() {
    const token = localStorage.getItem("auth_token");

    if (!token) {
      alert("🔐 Du musst eingeloggt sein, um Leads zu exportieren");
      window.location.href = "/login";
      return;
    }

    try {
      const response = await fetch(API + "/zevix/refresh-token", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        credentials: "include",
        body: JSON.stringify({ token })
      });

      const data = await response.json();

      if (data.success) {
        // Update localStorage with fresh data
        localStorage.setItem("auth_token", data.token);
        localStorage.setItem("auth_until", data.valid_until);
        localStorage.setItem("plan", data.plan);
        localStorage.setItem("zevix_email", data.email);

        const month = data.month;
        used = data.used || 0;

        // Update localStorage
        localStorage.setItem(`zevix_leads_used_${month}`, used);

        updateStatus();
      } else {
        alert("❌ Fehler beim Laden der Nutzungsdaten");
        window.location.href = "/login";
      }
    } catch (error) {
      console.error("Load usage error:", error);
      alert("❌ Fehler beim Laden der Nutzungsdaten");
    }
  }

  /* ======================== UTILITIES ======================== */
  const norm = v => String(v ?? "").toLowerCase().trim();

  function padPLZ(v) {
    return String(v ?? "").trim().padStart(4, "0");
  }

  function normOrt(v) {
    return String(v ?? "")
      .toLowerCase()
      .replace(/\(.*?\)/g, " ")
      .replace(/[.,]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\s+\d+$/g, "");
  }

  function excelDateToJS(v) {
    if (!v) return null;
    if (typeof v === "number") {
      const d = new Date((v - 25569) * 86400 * 1000);
      d.setHours(12, 0, 0, 0);
      return d;
    }
    const d = new Date(v);
    d.setHours(12, 0, 0, 0);
    return d;
  }

  function formatDateCH(v) {
    const d = excelDateToJS(v);
    return d ? d.toLocaleDateString("de-CH") : "";
  }

  function uniqSorted(arr) {
    return [...new Set(arr.map(x => String(x || "").trim()).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b, "de"));
  }

  function fillSelect(selectEl, values, allLabel) {
    const current = selectEl.value;
    selectEl.innerHTML =
      `<option value="">${allLabel}</option>` +
      values.map(v => `<option value="${v}">${v}</option>`).join("");
    if (values.includes(current)) selectEl.value = current;
  }

  function normalizeText(t) {
    return String(t || "")
      .replace(/\r\n/g, "\n")
      .replace(/\n{2,}/g, "§§")
      .replace(/\n/g, " ")
      .replace(/§§/g, "\n\n");
  }

  function getCHDate() {
    const d = new Date();
    const m = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
    return `${d.getDate()}. ${m[d.getMonth()]} ${d.getFullYear()}`;
  }

  /* ======================== AMTOVZ (Kanton mapping) ======================== */
  async function loadAMTOVZ() {
    const res = await fetch(AMTOVZ_URL, { cache: "no-store" });
    const textRaw = await res.text();
    const text = textRaw.replace(/^^\uFEFF/, "");

    const lines = text.split(/\r?\n/).filter(l => l.trim().length);
    if (!lines.length) { amtovzByPlzOrt = {}; amtovzByPlzOnly = {}; return; }

    const delim = lines[0].includes(";") ? ";" : ",";

    const splitCSV = (line) => {
      const out = [];
      let cur = "";
      let q = false;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (ch === '"') { q = !q; continue; }
        if (!q && ch === delim) { out.push(cur); cur = ""; continue; }
        cur += ch;
      }
      out.push(cur);
      return out.map(x => x.trim());
    };

    const headers = splitCSV(lines[0]).map(h =>
      h.replace(/^\uFEFF/, "").replace(/"/g, "").trim().toUpperCase()
    );

    const iPLZ = headers.findIndex(h => h === "PLZ" || h.startsWith("PLZ"));
    const iORT = headers.findIndex(h => h === "ORT" || h.includes("ORT"));
    const iKT = headers.findIndex(h => h === "KT" || h.includes("KANTON"));

    if (iPLZ < 0 || iORT < 0 || iKT < 0) {
      amtovzByPlzOrt = {};
      amtovzByPlzOnly = {};
      return;
    }

    const mapPlzOrt = {};
    const plzToSet = {};

    for (const line of lines.slice(1)) {
      const c = splitCSV(line);
      const plz = padPLZ(c[iPLZ]);
      const ort = normOrt(c[iORT] ?? "");
      const kt = String(c[iKT] ?? "").trim();

      if (!/^\d{4}$/.test(plz) || !ort || !kt) continue;

      mapPlzOrt[`${plz}|${ort}`] = kt;

      if (!plzToSet[plz]) plzToSet[plz] = new Set();
      plzToSet[plz].add(kt);
    }

    const mapPlzOnly = {};
    Object.keys(plzToSet).forEach(plz => {
      const s = plzToSet[plz];
      if (s.size === 1) mapPlzOnly[plz] = [...s][0];
    });

    amtovzByPlzOrt = mapPlzOrt;
    amtovzByPlzOnly = mapPlzOnly;
  }

  function getKantonFromRow(r) {
    const plz = padPLZ(r.PLZ);
    const ort = normOrt(r.Ort);
    const key = `${plz}|${ort}`;

    return amtovzByPlzOrt[key] || amtovzByPlzOnly[plz] || "";
  }

  /* ======================== FILTERING ======================== */
  function getFilteredRows() {
    const k = filterKanton.value;
    const o = norm(filterOrt.value);
    const b = filterBranche.value;

    const f = filterFrom.value ? new Date(filterFrom.value) : null;
    const t = filterTo.value ? new Date(filterTo.value) : null;
    if (f) f.setHours(0, 0, 0, 0);
    if (t) t.setHours(23, 59, 59, 999);

    return rows.filter(r => {
      const d = excelDateToJS(r.Datum);
      if (f && (!d || d < f)) return false;
      if (t && (!d || d > t)) return false;

      if (k && getKantonFromRow(r) !== k) return false;
      if (o && !norm(r.Ort).includes(o)) return false;

      if (b && String(r.Branche_AI || "").trim() !== b) return false;

      return true;
    });
  }

  function getRemainingLeads() {
    return Math.max(0, limit - used);
  }

  function updateStatus() {
    const remaining = getRemainingLeads();
    const filtered = getFilteredRows().length;
    const willExport = Math.min(filtered, remaining);

    if (statusText) statusText.textContent = `${remaining} von ${limit} Leads verfügbar (${plan})`;
    if (filteredLeadsEl) filteredLeadsEl.textContent = filtered.toLocaleString("de-CH");
    if (willExportEl) willExportEl.textContent = willExport.toLocaleString("de-CH");
    if (remainingLeadsEl) remainingLeadsEl.textContent = remaining.toLocaleString("de-CH");

    // Update dot color
    const dot = status ? status.querySelector(".dot") : null;
    if (dot) {
      dot.classList.remove("loading", "success", "warning");
      if (remaining <= 0) dot.classList.add("warning");
      else dot.classList.add("success");
    }

    const disable = remaining <= 0;
    if (btnCSV) btnCSV.disabled = disable;
    if (btnXLSX) btnXLSX.disabled = disable;
    if (btnPDF) btnPDF.disabled = disable || plan === "basic";
  }

  [filterKanton, filterOrt, filterBranche, filterFrom, filterTo].forEach(e => {
    if (e) {
      e.oninput = updateStatus;
      e.onchange = updateStatus;
    }
  });

  const EXPORT_FIELDS = ["Firma", "Strasse", "PLZ", "Ort", "Branche_AI", "Datum"]; 

  let exportLock = false;

  /* ======================== API: CONSUME LEADS ======================== */
  // ✅ FIX 4: Use batch endpoint with all lead IDs
  async function consumeLeadsViaAPI(leadIds) {
    try {
      const token = localStorage.getItem("auth_token");

      if (!token) {
        alert("🔐 Du musst eingeloggt sein, um Leads zu exportieren");
        return false;
      }

      // ✅ FIX 5: Use batch endpoint and send ALL lead IDs
      const response = await fetch(API + "/zevix/export-leads-batch", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${token}`
        },
        credentials: "include",
        body: JSON.stringify({
          lead_ids: leadIds  // ✅ Send ALL IDs, not just first one
        })
      });

      // ✅ FIX 7: Check HTTP status before parsing JSON
      if (!response.ok) {
        let errorMessage = `HTTP ${response.status}`;
        try {
          const errorData = await response.json();
          errorMessage = errorData.message || errorData.error || errorMessage;
        } catch (e) {
          errorMessage = response.statusText || errorMessage;
        }

        if (response.status === 401) {
          alert("🔐 Sitzung abgelaufen. Bitte melde dich erneut an.");
          window.location.href = "/login";
        } else if (response.status === 403) {
          alert(`🚫 Zugriff verweigert: ${errorMessage}`);
        } else if (response.status === 500) {
          alert(`⚠️ Server-Fehler: ${errorMessage}\n\nBitte versuche es später erneut.`);
        } else {
          alert(`❌ Fehler beim Lead-Export: ${errorMessage}`);
        }
        return false;
      }

      const result = await response.json();

      if (!result.success) {
        alert(`🚫 ${result.message || result.error || "Lead-Verbrauch fehlgeschlagen"}`);
        return false;
      }

      // ✅ FIX 6: Update used count from backend response
      used = result.used;

      // Update localStorage
      const month = new Date().toISOString().slice(0, 7);
      localStorage.setItem(`zevix_leads_used_${month}`, used);

      updateStatus();

      // Show detailed feedback
      const messageParts = [result.message];

      if (result.duplicate_ids && result.duplicate_ids.length > 0) {
        messageParts.push(`⚠️ ${result.duplicate_ids.length} Lead(s) bereits exportiert (übersprungen)`);
      }
      if (result.not_exported && result.not_exported.length > 0) {
        messageParts.push(`⚠️ ${result.not_exported.length} Lead(s) nicht exportiert (Limit erreicht)`);
      }

      if (messageParts.length > 1) {
        alert(messageParts.join('\n'));
      }

      return true;
    } catch (err) {
      console.error("Export-Lead API Error:", err);
      // ✅ FIX 8: Show detailed error message instead of generic one
      const errorDetails = err.message || err.toString() || 'Unknown error';
      const errorStr = typeof errorDetails === 'string' ? errorDetails.toLowerCase() : '';
      const isNetworkError = err instanceof TypeError ||
        errorStr.includes('network') ||
        errorStr.includes('failed to fetch');

      if (isNetworkError) {
        alert("🌐 Netzwerkfehler: Bitte überprüfe deine Internetverbindung und versuche es erneut.");
      } else {
        alert(`🚫 Fehler beim Lead-Verbrauch: ${errorDetails}`);
      }
      return false;
    }
  }

  /* ======================== EXPORT: CSV ======================== */
  window.exportCSV = async () => {
    if (exportLock) return;
    exportLock = true;

    try {
      const data = getFilteredRows();
      if (!data.length) {
        alert("Keine Treffer");
        return;
      }

      const leadIds = data.map(r =>
        `${r.Firma}|${r.Strasse}|${padPLZ(r.PLZ)}|${r.Ort}`.toLowerCase()
      );

      if (!await consumeLeadsViaAPI(leadIds)) {
        return;
      }

      const lines = [
        EXPORT_FIELDS.join(";"),
        ...data.map(r =>
          EXPORT_FIELDS.map(h => {
            if (h === "Datum") return formatDateCH(r[h]);
            return String(r[h] ?? "").replace(/;/g, ",");
          }).join(";")
        )
      ];

      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([lines.join("\n")]));
      a.download = "leads.csv";
      a.click();
    } finally {
      exportLock = false;
    }
  };

  /* ======================== EXPORT: EXCEL ======================== */
  window.exportExcel = async () => {
    if (exportLock) return;
    exportLock = true;

    try {
      const raw = getFilteredRows();
      if (!raw.length) {
        alert("Keine Treffer");
        return;
      }

      const leadIds = raw.map(r =>
        `${r.Firma}|${r.Strasse}|${padPLZ(r.PLZ)}|${r.Ort}`.toLowerCase()
      );

      if (!await consumeLeadsViaAPI(leadIds)) {
        return;
      }

      const data = raw.map(r => {
        const o = {};
        EXPORT_FIELDS.forEach(k => {
          o[k] = (k === "Datum") ? formatDateCH(r[k]) : r[k];
        });
        return o;
      });

      const wb = XLSX.utils.book_new();
      const ws = XLSX.utils.json_to_sheet(data);
      XLSX.utils.book_append_sheet(wb, ws, "Leads");
      XLSX.writeFile(wb, "leads.xlsx");
    } finally {
      exportLock = false;
    }
  };

  /* ======================== EXPORT: PDF (Serienbrief) ======================== */
  window.generatePDF = () => {
    const plan = localStorage.getItem("plan") || "basic";

    if (plan === "basic") {
      alert("📄 Serienbrief ist erst ab dem Business-Abo verfügbar.");
      window.location.href = "/preise";
      return;
    }
    const dataAll = getFilteredRows();
    if (!dataAll.length) {
      alert("Keine Treffer");
      return;
    }

    const { jsPDF } = window.jspdf;
    const pdf = new jsPDF("p", "mm", "a4");
    const w = pdf.internal.pageSize.getWidth();
    const m = 20;

    // QR Code
    const qrInput = document.getElementById("qrLinkInput");
    const qrLink = qrInput ? qrInput.value.trim() : "";
    let qrDataURL = null;
    const qrCanvas = document.querySelector("#qrPreviewBox canvas");
    if (qrLink && qrCanvas) {
      qrDataURL = qrCanvas.toDataURL("image/png");
    }

    dataAll.forEach((r, i) => {
      if (i) pdf.addPage();
      let y = 20;

      pdf.setFontSize(9);
      pdf.text(brandClaim.value, m, y);
      pdf.text(senderBlock.value, w - m - 40, y);

      y = 65;
      pdf.setFontSize(11);
      pdf.text(`${r.Firma}\n${r.Strasse}\n${padPLZ(r.PLZ)} ${r.Ort}`, m, y);

      y += 30;
      pdf.text(getCHDate(), m, y);
      y += 15;

      const text = normalizeText(letterText.value)
        .replace(/{{firma}}/gi, r.Firma)
        .replace(/{{strasse}}/gi, r.Strasse)
        .replace(/{{plz}}/gi, padPLZ(r.PLZ))
        .replace(/{{ort}}/gi, r.Ort);

      pdf.text(text, m, y, { maxWidth: w - m * 2 });
      y += pdf.getTextDimensions(text, { maxWidth: w - m * 2 }).h + 8;

      const sigImg = getSignatureDataURL();
      if (sigImg) {
        pdf.addImage(sigImg, "PNG", m, y, 50, 18);
        y += 22;
      }

      pdf.text(sign1.value, m, y);

      // QR Code in bottom-right corner
      if (qrDataURL) {
        const pageH = pdf.internal.pageSize.getHeight();
        pdf.addImage(qrDataURL, "PNG", w - m - 25, pageH - m - 25, 25, 25);
      }
    });

    pdf.save("serienbrief_gefiltert.pdf");
  };

  /* ======================== BUTTON EVENT LISTENERS ======================== */
  if (btnCSV) btnCSV.addEventListener("click", () => window.exportCSV());
  if (btnXLSX) btnXLSX.addEventListener("click", () => window.exportExcel());
  if (btnPDF) btnPDF.addEventListener("click", () => window.generatePDF());

  /* ======================== QR CODE PREVIEW ======================== */
  const qrLinkInput = document.getElementById("qrLinkInput");
  const qrPreviewBox = document.getElementById("qrPreviewBox");
  const qrStatus = document.getElementById("qrStatus");

  if (qrLinkInput) {
    let qrTimeout;
    qrLinkInput.addEventListener("input", () => {
      clearTimeout(qrTimeout);
      qrTimeout = setTimeout(() => {
        const link = qrLinkInput.value.trim();
        if (!link) {
          if (qrPreviewBox) qrPreviewBox.style.display = "none";
          if (qrStatus) qrStatus.textContent = "";
          return;
        }

        try {
          new URL(link);
        } catch (e) {
          if (qrStatus) qrStatus.textContent = "⚠️ Bitte eine gültige URL eingeben.";
          if (qrPreviewBox) qrPreviewBox.style.display = "none";
          return;
        }

        // Generate QR Code using a simple canvas-based QR generator
        if (qrPreviewBox) {
          qrPreviewBox.style.display = "block";
          qrPreviewBox.innerHTML = `<img src="https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=${encodeURIComponent(link)}" alt="QR Code" style="border-radius:8px;">`;
        }
        if (qrStatus) qrStatus.textContent = "✅ QR-Code wird im Brief eingefügt.";
      }, 500);
    });
  }

  /* ======================== INIT: LOAD DATA ======================== */
  (async () => {
    if (statusText) statusText.textContent = "Laden…";

    await loadAMTOVZ();

    const sheets = await Promise.all(EXCEL_URLS.map(async u => {
      const r = await fetch(u);
      const b = await r.arrayBuffer();
      const wb = XLSX.read(b, { type: "array" });
      return XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]]);
    }));
    rows = sheets.flat();

    const kantone = uniqSorted(rows.map(r => getKantonFromRow(r)).filter(Boolean));
    const branchen = uniqSorted(rows.map(r => String(r.Branche_AI || "").trim()).filter(Boolean));

    fillSelect(filterKanton, kantone, "Alle Kantone");
    fillSelect(filterBranche, branchen, "Alle Branchen");

    if (btnCSV) btnCSV.disabled = false;
    if (btnXLSX) btnXLSX.disabled = false;

    // ✅ FIX 7: Load usage from backend after data loads
    await loadUsageFromBackend();
  })();

});

/* ======================== SESSION CHECK ======================== */
(function () {
  const until = Number(localStorage.getItem("auth_until") || 0);

  if (!until || Date.now() > until) {
    localStorage.removeItem("auth_until");
    localStorage.removeItem("auth_ip");
    localStorage.removeItem("auth_token");
    window.location.replace("/login");
    return;
  }
})();

setInterval(() => {
  const until = Number(localStorage.getItem("auth_until") || 0);
  if (!until || Date.now() > until) {
    localStorage.removeItem("auth_until");
    localStorage.removeItem("auth_ip");
    localStorage.removeItem("auth_token");
    window.location.replace("/login");
  }
}, 30000);