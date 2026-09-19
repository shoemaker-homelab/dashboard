/* Splash page logic: system stats + service health checks, evaluated once on load. */
"use strict";

function fmtUptime(s) {
  var d = Math.floor(s / 86400);
  var h = Math.floor((s % 86400) / 3600);
  var m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

function setText(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

async function loadStats() {
  try {
    var res = await fetch("/api/stats", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    var s = await res.json();
    setText("st-cpu", s.cpu_percent + "% (" + s.cpu_count + " cores)");
    setText("st-ram", s.ram_used_gb + " / " + s.ram_total_gb + " GB (" + s.ram_percent + "%)");
    setText("st-disk", s.disk_used_gb + " / " + s.disk_total_gb + " GB (" + s.disk_percent + "%)");
    setText("st-temp", s.cpu_temp_c != null ? s.cpu_temp_c + " C" : "n/a");
    setText("st-uptime", fmtUptime(s.uptime_s));
    setText("st-ip", location.hostname);
  } catch (err) {
    ["st-cpu", "st-ram", "st-disk", "st-temp", "st-uptime"].forEach(function (id) {
      setText(id, "offline");
    });
  }
}

function checkServices() {
  var base = "/api/pb";
  // Rewrite PocketBase health entry to the direct base URL from config.js.
  var pbHealth = document.getElementById("pb-health");
  if (pbHealth) pbHealth.dataset.url = base + "/api/health";
  var pbLeads = document.getElementById("pb-leads");
  if (pbLeads) {
    pbLeads.dataset.url = base + "/api/collections/leads/records?perPage=1";
    pbLeads.dataset.health = "leads";   // green only when records exist
  }
  var pbAdmin = document.getElementById("pb-admin");
  if (pbAdmin) pbAdmin.href = base + "/_/";
  var dotChecks = document.querySelectorAll("#services li[data-url], #services li[data-api]");
  dotChecks.forEach(function (li) {
    var dot = li.querySelector(".dot");
    var detail = li.querySelector(".detail");
    if (li.dataset.api) {
      // Same-origin BFF probe (via /api/probe contract): the probe endpoint
      // performs the upstream request server-side and folds 2xx/3xx into
      // ok=true, so healthy === (probe HTTP 200 AND body.ok === true).
      // data-api may be relative (/api/...) or an absolute URL to probe.
      var probeTarget = /^https?:\/\//i.test(li.dataset.api)
        ? "/api/probe?url=" + encodeURIComponent(li.dataset.api)
        : li.dataset.api;
      fetch(probeTarget, { cache: "no-store" })
        .then(function (res) {
          if (!res.ok) throw new Error("probe HTTP " + res.status);
          return res.json();
        })
        .then(function (probe) {
          if (probe.ok === true) {
            dot.className = "dot ok";
            detail.textContent = "";
          } else {
            dot.className = "dot bad";
            detail.textContent = "HTTP " + probe.status;
          }
        })
        .catch(function () {
          dot.className = "dot bad";
          detail.textContent = "offline";
        });
    } else if (li.dataset.url.charAt(0) === "/") {
      // Relative data-url (e.g. /api/pb/api/health, /api/stats): fetch it
      // DIRECTLY same-origin. The probe endpoint would reject these with
      // 400 (its scheme allowlist only covers http/https upstreams).
      fetch(li.dataset.url, { cache: "no-store" })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          // data-health="leads": 200 alone isn't enough - the collection
          // must actually serve at least one record, else red.
          if (li.dataset.health === "leads") {
            return fetch(base + "/api/collections/leads/records?perPage=1", { cache: "no-store" })
              .then(function (res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
              })
              .then(function (data) {
                if (!data || !(data.totalItems >= 1)) {
                  throw new Error("no leads in DB");
                }
                return null;
              });
          }
          return null;
        })
        .then(function () {
          dot.className = "dot ok";
          detail.textContent = "";
          detail.title = "";
        })
        .catch(function (err) {
          dot.className = "dot bad";
          detail.textContent = err.message || "offline";
          detail.title = err.message || "";
        });
    } else {
      // Direct cross-origin fetches caused CORS "TypeError: Failed to fetch"
      // (e.g. hermes.tailda8422.ts.net). Route through the BFF's
      // same-origin probe endpoint instead; it folds 2xx/3xx into ok=true.
      fetch("/api/probe?url=" + encodeURIComponent(li.dataset.url), { cache: "no-store" })
        .then(function (res) {
          if (!res.ok) throw new Error("probe HTTP " + res.status);
          return res.json();
        })
        .then(function (probe) {
          if (probe.ok !== true) throw new Error("HTTP " + (probe.status != null ? probe.status : "n/a"));
          // data-health="leads": 200 alone isn't enough - the collection
          // must actually serve at least one record, else red.
          if (li.dataset.health === "leads") {
            return fetch(base + "/api/collections/leads/records?perPage=1", { cache: "no-store" })
              .then(function (res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
              })
              .then(function (data) {
                if (!data || !(data.totalItems >= 1)) {
                  throw new Error("no leads in DB");
                }
                return null;
              });
          }
          return null;
        })
        .then(function () {
          dot.className = "dot ok";
          detail.textContent = "";
          detail.title = "";
        })
        .catch(function (err) {
          dot.className = "dot bad";
          detail.textContent = err && err.message === "no leads in DB" ? "" : (err ? err.message : "offline");
          detail.title = err && err.message === "no leads in DB" ? "no leads in DB" : "";
        });
    }
  });
}

loadStats();
checkServices();
