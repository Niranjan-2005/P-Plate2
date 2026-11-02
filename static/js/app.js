/* P-PLATE2T web UI — self-contained, no third-party dependencies. */
(function () {
  "use strict";

  const $ = (sel, ctx) => (ctx || document).querySelector(sel);
  const $$ = (sel, ctx) => Array.from((ctx || document).querySelectorAll(sel));
  const SVGNS = "http://www.w3.org/2000/svg";

  function parseJSON(str, fallback) {
    try { return JSON.parse(str); } catch (e) { return fallback; }
  }

  /* ---------- Bar chart: daily inspection volume ---------- */
  function barChart(el) {
    const days = parseJSON(el.dataset.days, []);
    const normal = parseJSON(el.dataset.normal, []);
    const defect = parseJSON(el.dataset.defect, []);
    if (!days.length) return;
    const max = Math.max(1, ...normal.map((v, i) => v + defect[i]));
    const W = 520, H = 220, pad = 26, bw = (W - pad * 2) / days.length;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

    // baseline
    addLine(svg, pad, H - pad, W - pad, H - pad, "#e6e9f0");

    days.forEach((d, i) => {
      const x = pad + i * bw;
      const total = normal[i] + defect[i];
      const h = (total / max) * (H - pad * 2);
      const dh = total ? (defect[i] / total) * h : 0;
      const nh = h - dh;
      const barW = bw * 0.5;
      const bx = x + (bw - barW) / 2;
      // normal (green) bottom
      addRect(svg, bx, H - pad - nh, barW, nh, "#16a34a", 3);
      // defective (red) stacked on top
      if (dh > 0) addRect(svg, bx, H - pad - nh - dh, barW, dh, "#e5484d", 3);
      // x label
      addText(svg, x + bw / 2, H - 8, d, "#6b7688", 10);
      if (total) addText(svg, bx + barW / 2, H - pad - nh - dh - 5, String(total), "#1c2430", 10);
    });
    el.appendChild(svg);
  }

  /* ---------- Donut chart: classification mix ---------- */
  function donutChart(el) {
    const normal = parseInt(el.dataset.normal || "0", 10);
    const defect = parseInt(el.dataset.defective || "0", 10);
    const total = normal + defect;
    const size = 180, cx = size / 2, cy = size / 2, r = 66, sw = 26;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
    const circ = 2 * Math.PI * r;

    const track = document.createElementNS(SVGNS, "circle");
    track.setAttribute("cx", cx); track.setAttribute("cy", cy); track.setAttribute("r", r);
    track.setAttribute("fill", "none"); track.setAttribute("stroke", "#eef1f6"); track.setAttribute("stroke-width", sw);
    svg.appendChild(track);

    if (total > 0) {
      const frac = defect / total;
      const arc = document.createElementNS(SVGNS, "circle");
      arc.setAttribute("cx", cx); arc.setAttribute("cy", cy); arc.setAttribute("r", r);
      arc.setAttribute("fill", "none"); arc.setAttribute("stroke", "#e5484d");
      arc.setAttribute("stroke-width", sw);
      arc.setAttribute("stroke-dasharray", `${frac * circ} ${circ}`);
      arc.setAttribute("transform", `rotate(-90 ${cx} ${cy})`);
      svg.appendChild(arc);
      const g = document.createElementNS(SVGNS, "circle");
      g.setAttribute("cx", cx); g.setAttribute("cy", cy); g.setAttribute("r", r);
      g.setAttribute("fill", "none"); g.setAttribute("stroke", "#16a34a");
      g.setAttribute("stroke-width", sw);
      g.setAttribute("stroke-dasharray", `${(1 - frac) * circ} ${circ}`);
      g.setAttribute("stroke-dashoffset", `${-frac * circ}`);
      g.setAttribute("transform", `rotate(-90 ${cx} ${cy})`);
      svg.appendChild(g);
    }
    const rate = total ? Math.round((defect / total) * 100) : 0;
    addText(svg, cx, cy - 2, String(total), "#1c2430", 22, "800");
    addText(svg, cx, cy + 18, rate + "% defect", "#6b7688", 11);
    el.appendChild(svg);
  }

  function addRect(svg, x, y, w, h, fill, r) {
    const el = document.createElementNS(SVGNS, "rect");
    el.setAttribute("x", x); el.setAttribute("y", Math.max(y, 0));
    el.setAttribute("width", Math.max(w, 0)); el.setAttribute("height", Math.max(h, 0));
    el.setAttribute("fill", fill); if (r) el.setAttribute("rx", r);
    svg.appendChild(el);
  }
  function addLine(svg, x1, y1, x2, y2, stroke) {
    const el = document.createElementNS(SVGNS, "line");
    el.setAttribute("x1", x1); el.setAttribute("y1", y1); el.setAttribute("x2", x2); el.setAttribute("y2", y2);
    el.setAttribute("stroke", stroke); svg.appendChild(el);
  }
  function addText(svg, x, y, str, fill, size, weight) {
    const el = document.createElementNS(SVGNS, "text");
    el.setAttribute("x", x); el.setAttribute("y", y); el.setAttribute("fill", fill);
    el.setAttribute("font-size", size); el.setAttribute("text-anchor", "middle");
    if (weight) el.setAttribute("font-weight", weight);
    el.textContent = str; svg.appendChild(el);
  }

  /* ---------- Image viewer (zoom / pan / annotation) ---------- */
  function initViewer() {
    const stage = $("#viewerStage");
    const img = $("#viewerImg");
    if (!stage || !img) return;
    let scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0;

    const apply = () => { img.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`; };
    $("#zoomIn") && $("#zoomIn").addEventListener("click", () => { scale = Math.min(4, scale + 0.25); apply(); });
    $("#zoomOut") && $("#zoomOut").addEventListener("click", () => { scale = Math.max(0.5, scale - 0.25); apply(); });
    $("#zoomReset") && $("#zoomReset").addEventListener("click", () => { scale = 1; tx = 0; ty = 0; apply(); });

    stage.addEventListener("mousedown", (e) => { dragging = true; sx = e.clientX - tx; sy = e.clientY - ty; stage.style.cursor = "grabbing"; });
    window.addEventListener("mouseup", () => { dragging = false; stage.style.cursor = ""; });
    window.addEventListener("mousemove", (e) => { if (!dragging) return; tx = e.clientX - sx; ty = e.clientY - sy; apply(); });
    stage.addEventListener("wheel", (e) => { e.preventDefault(); scale = Math.min(4, Math.max(0.5, scale + (e.deltaY < 0 ? 0.15 : -0.15))); apply(); }, { passive: false });

    // thumbnail selection
    const thumbs = $$(".thumb");
    const annot = $("#annot");
    const predPanel = $("#predPanel"), predLabel = $("#predLabel"), predConf = $("#predConf"), nameEl = $("#viewerName");
    function select(btn) {
      thumbs.forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      scale = 1; tx = 0; ty = 0; apply();
      img.src = btn.dataset.url;
      if (nameEl) nameEl.textContent = btn.dataset.name;
      const label = btn.dataset.label, conf = btn.dataset.conf;
      if (predLabel) predLabel.textContent = label || "—";
      if (predConf) predConf.textContent = conf && conf !== "None" ? `Confidence ${conf}` : "No confidence value";
      if (predPanel) { predPanel.className = "pred " + (label ? label.toLowerCase() : ""); }
      // annotation box for defective images
      if (annot) {
        if (label === "Defective") {
          annot.classList.add("active");
          annot.style.left = "30%"; annot.style.top = "34%";
          annot.style.width = "34%"; annot.style.height = "30%";
          annot.querySelector(".annot-label").textContent = "Defective" + (conf && conf !== "None" ? " · " + Math.round(conf * 100) + "%" : "");
        } else { annot.classList.remove("active"); }
      }
    }
    thumbs.forEach((t) => t.addEventListener("click", () => select(t)));
    if (thumbs.length) select(thumbs[0]);
  }

  /* ---------- Drag & drop upload ---------- */
  function initUpload() {
    const dz = $("#dropzone"), input = $("#fileInput"), list = $("#fileList"), browse = $("#browseBtn");
    if (!dz || !input) return;
    let files = [];

    const render = () => {
      list.innerHTML = "";
      files.forEach((f, i) => {
        const li = document.createElement("li");
        const nameEl = document.createElement("span");
        nameEl.textContent = f.name;
        const sizeEl = document.createElement("span");
        sizeEl.className = "muted";
        sizeEl.textContent = (f.size / 1024).toFixed(0) + " KB";
        const rm = document.createElement("span");
        rm.className = "rm";
        rm.textContent = "✕";
        rm.addEventListener("click", () => { files.splice(i, 1); sync(); });
        li.appendChild(nameEl); li.appendChild(sizeEl); li.appendChild(rm);
        list.appendChild(li);
      });
    };
    const sync = () => {
      const dt = new DataTransfer();
      files.forEach((f) => dt.items.add(f));
      input.files = dt.files;
      render();
    };
    const add = (arr) => {
      Array.from(arr).forEach((f) => {
        if (/image\/(png|jpe?g)/.test(f.type) && f.size <= 5 * 1024 * 1024) files.push(f);
      });
      sync();
    };

    browse && browse.addEventListener("click", (e) => { e.stopPropagation(); input.click(); });
    dz.addEventListener("click", () => input.click());
    input.addEventListener("change", () => add(input.files));
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => add(e.dataTransfer.files));
  }

  /* ---------- Poll batch results while processing ---------- */
  function initPolling() {
    const banner = $("#pollBanner");
    if (!banner) return;
    setTimeout(() => window.location.reload(), 4000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const vol = $("#volumeChart"); if (vol) barChart(vol);
    const mix = $("#mixChart"); if (mix) donutChart(mix);
    initViewer();
    initUpload();
    initPolling();
  });
})();
