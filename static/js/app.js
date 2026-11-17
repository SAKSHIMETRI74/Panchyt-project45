(function () {
  "use strict";

  // ---------- helpers ----------
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var toastTimer;
  function toast(msg, type) {
    var el = $("#toast");
    if (!el) return alert(msg);
    el.textContent = msg;
    el.className = "toast show " + (type || "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast " + (type || ""); }, 5000);
  }

  function api(url, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (opts.json) {
      init.method = opts.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    } else if (opts.form) {
      init.method = "POST";
      init.body = opts.form;
    }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return { success: false, message: "Unexpected server response." }; })
        .then(function (data) { data._status = r.status; return data; });
    }).catch(function () {
      return { success: false, message: "Network problem. Please check your internet and try again." };
    });
  }

  function busy(btn, on) { if (btn) btn.disabled = !!on; }
  function formJSON(form) {
    var o = {};
    $$("input,select,textarea", form).forEach(function (el) {
      if (!el.name) return;
      o[el.name] = el.type === "checkbox" ? el.checked : el.value;
    });
    return o;
  }
  function openModal(id) { var m = $(id); if (m) m.classList.remove("hidden"); }
  function closeModals() { $$(".modal").forEach(function (m) { m.classList.add("hidden"); }); }

  document.addEventListener("click", function (e) {
    if (e.target.matches("[data-close]") || e.target.classList.contains("modal")) closeModals();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModals(); });

  // ---------- login (citizen + official) ----------
  var loginForm = $("#loginForm");
  if (loginForm) {
    loginForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = $("button[type=submit]", loginForm);
      busy(btn, true);
      api("/api/auth/login", { json: formJSON(loginForm) }).then(function (d) {
        busy(btn, false);
        if (d.success) { window.location.href = d.redirect || "/"; }
        else toast(d.message, "error");
      });
    });
  }

  // ---------- citizen registration (OTP) ----------
  var otpSend = $("#otpSendForm");
  if (otpSend) {
    var state = { phone: "", token: "" };
    $$(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        $$(".tab").forEach(function (t) { t.classList.toggle("active", t === tab); });
        $$(".panel").forEach(function (p) { p.classList.toggle("active", p.dataset.panel === tab.dataset.tab); });
      });
    });
    var showStep = function (n) {
      $$(".step").forEach(function (s) { s.classList.toggle("hidden", s.dataset.step !== String(n)); });
      $$(".steps li").forEach(function (li) { li.classList.toggle("on", Number(li.dataset.step) <= n); });
    };

    otpSend.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = $("button[type=submit]", otpSend);
      busy(btn, true);
      state.phone = otpSend.phone.value;
      api("/api/auth/otp/send", { json: { phone: state.phone } }).then(function (d) {
        busy(btn, false);
        if (!d.success) return toast(d.message, "error");
        toast(d.message, "success");
        var demo = $("#demoOtp");
        if (demo && d.dev_otp) {
          demo.textContent = demo.dataset.prefix + ": " + d.dev_otp;
          demo.classList.remove("hidden");
        }
        showStep(2);
      });
    });

    $("#backToPhone").addEventListener("click", function () { showStep(1); });

    $("#otpVerifyForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var form = e.target, btn = $("button[type=submit]", form);
      busy(btn, true);
      api("/api/auth/otp/verify", { json: { phone: state.phone, otp: form.otp.value } }).then(function (d) {
        busy(btn, false);
        if (!d.success) return toast(d.message, "error");
        state.token = d.verification_token;
        showStep(3);
      });
    });

    $("#registerForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var form = e.target, btn = $("button[type=submit]", form);
      busy(btn, true);
      var body = formJSON(form);
      body.phone = state.phone; body.verification_token = state.token;
      api("/api/auth/register", { json: body }).then(function (d) {
        busy(btn, false);
        if (d.success) window.location.href = "/citizen/dashboard";
        else toast(d.message, "error");
      });
    });
  }

  // ---------- citizen dashboard ----------
  var toggle = $("#toggleForm");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var card = $("#complaintCard");
      card.classList.toggle("hidden");
      if (!card.classList.contains("hidden")) card.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  var gpsBtn = $("#gpsBtn");
  if (gpsBtn) {
    gpsBtn.addEventListener("click", function () {
      if (!navigator.geolocation) return toast("Location is not supported on this device.", "error");
      gpsBtn.disabled = true;
      navigator.geolocation.getCurrentPosition(function (pos) {
        var f = $("#complaintForm");
        f.latitude.value = pos.coords.latitude.toFixed(6);
        f.longitude.value = pos.coords.longitude.toFixed(6);
        $("#gpsStatus").textContent = "✔ " + gpsBtn.dataset.done;
        gpsBtn.disabled = false;
      }, function () {
        gpsBtn.disabled = false;
        toast("Could not get your location. You can type the address instead.", "error");
      }, { enableHighAccuracy: true, timeout: 12000 });
    });
  }

  var cForm = $("#complaintForm");
  if (cForm) {
    cForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = $("button[type=submit]", cForm);
      busy(btn, true);
      api("/api/complaints/submit", { form: new FormData(cForm) }).then(function (d) {
        busy(btn, false);
        if (d.success) {
          toast(d.message, "success");
          setTimeout(function () { window.location.reload(); }, 1800);
        } else toast(d.message, "error");
      });
    });
  }

  var appealId = null;
  $$("[data-appeal]").forEach(function (b) {
    b.addEventListener("click", function () { appealId = b.dataset.appeal; openModal("#appealModal"); });
  });
  var aForm = $("#appealForm");
  if (aForm) {
    aForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = $("button[type=submit]", aForm);
      busy(btn, true);
      api("/api/complaints/" + appealId + "/appeal", { json: { reason: aForm.reason.value } }).then(function (d) {
        busy(btn, false);
        if (d.success) { toast(d.message, "success"); setTimeout(function () { location.reload(); }, 1500); }
        else toast(d.message, "error");
      });
    });
  }

  // ---------- public tracking ----------
  var trackForm = $("#trackForm");
  if (trackForm) {
    var out = $("#trackResult");
    var statuses = JSON.parse(out.dataset.statuses || "{}");
    var labels = JSON.parse(out.dataset.labels || "{}");
    var run = function (q) {
      if (!q) return;
      out.innerHTML = '<p class="muted">' + esc(labels.loading) + "</p>";
      api("/api/complaints/track/" + encodeURIComponent(q.trim())).then(function (d) {
        if (!d.success) { out.innerHTML = '<p class="notice">' + esc(d.message || out.dataset.notfound) + "</p>"; return; }
        var c = d.complaint, h = "";
        h += '<div class="card" style="margin-top:14px"><code>' + esc(c.tracking_id) + "</code> ";
        h += '<span class="badge st-' + esc(c.status.toLowerCase().replace(/ /g, "-")) + '">' + esc(statuses[c.status] || c.status) + "</span>";
        h += "<h3>" + esc(c.title) + "</h3>";
        h += '<p class="muted small">' + esc(c.category) + " · " + esc((c.created_at || "").slice(0, 10)) +
             (c.location_address ? " · " + esc(c.location_address) : "") + "</p>";
        if (c.officer_remarks) h += '<p class="remark"><b>' + esc(labels.remarks) + ":</b> " + esc(c.officer_remarks) + "</p>";
        h += "<h4>" + esc(labels.timeline) + "</h4><ul class='tl'>";
        d.timeline.forEach(function (t) {
          h += "<li><b>" + esc(statuses[t.status] || t.status) + '</b> <span class="muted small">' + esc(t.created_at) + "</span></li>";
        });
        h += "</ul></div>";
        out.innerHTML = h;
      });
    };
    trackForm.addEventListener("submit", function (e) { e.preventDefault(); run(trackForm.q.value); });
    if (out.dataset.auto) run(out.dataset.auto);
  }

  // ---------- official console ----------
  var table = $("#adminTable");
  if (table) {
    var search = $("#adminSearch"), sel = $("#adminStatus");
    var filter = function () {
      var q = search.value.trim().toLowerCase(), s = sel.value;
      $$("tbody tr[data-status]", table).forEach(function (tr) {
        var ok = (!s || tr.dataset.status === s) && (!q || tr.dataset.search.indexOf(q) !== -1);
        tr.classList.toggle("hidden", !ok);
      });
    };
    search.addEventListener("input", filter);
    sel.addEventListener("change", filter);

    var current = null;
    $$("[data-manage]").forEach(function (b) {
      b.addEventListener("click", function () {
        var id = Number(b.dataset.manage);
        current = (window.COMPLAINTS || []).filter(function (c) { return c.id === id; })[0];
        if (!current) return;
        var f = $("#manageForm");
        $("#manageTitle").textContent = current.tracking_id + " — " + current.title;
        $("#manageDesc").textContent = current.description;
        f.status.value = current.status;
        f.assigned_to.value = current.assigned_to || "";
        f.officer_remarks.value = current.officer_remarks || "";
        f.mark_false.checked = !!current.is_false_complaint;
        var p = $("#managePhoto");
        p.innerHTML = current.photo_filename
          ? '<a target="_blank" rel="noopener" href="/media/complaint/' + current.id + "/" + encodeURIComponent(current.photo_filename) + '">🖼 Photo</a>'
          : "";
        openModal("#manageModal");
      });
    });

    $("#manageForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var f = e.target, btn = $("button[type=submit]", f);
      busy(btn, true);
      api("/api/admin/complaints/" + current.id + "/update", { json: formJSON(f) }).then(function (d) {
        busy(btn, false);
        if (d.success) { toast(d.message, "success"); setTimeout(function () { location.reload(); }, 1000); }
        else toast(d.message, "error");
      });
    });

    $$("[data-history]").forEach(function (b) {
      b.addEventListener("click", function () {
        var body = $("#historyBody");
        body.textContent = "…";
        openModal("#historyModal");
        api("/api/admin/complaints/" + b.dataset.history + "/audit").then(function (d) {
          if (!d.success) { body.textContent = d.message; return; }
          var h = "<ul class='tl'>";
          d.audit_trail.forEach(function (l) {
            h += "<li><b>" + esc(l.action) + "</b>" +
                 (l.previous_state && l.previous_state !== l.new_state ? " · " + esc(l.previous_state) + " → " + esc(l.new_state) : "") +
                 '<br><span class="muted small">' + esc(l.created_at) + " · " + esc(l.actor_name || l.actor_role) + "</span>" +
                 (l.justification && l.justification !== "-" ? "<br>" + esc(l.justification) : "") + "</li>";
          });
          body.innerHTML = h + "</ul>";
        });
      });
    });
  }

  $$("[data-decide]").forEach(function (b) {
    b.addEventListener("click", function () {
      var box = b.closest(".appeal");
      b.disabled = true;
      api("/api/admin/appeals/" + box.dataset.appealId + "/decide", {
        json: { decision: b.dataset.decide, notes: $(".appeal-notes", box).value }
      }).then(function (d) {
        b.disabled = false;
        if (d.success) { toast(d.message, "success"); setTimeout(function () { location.reload(); }, 1000); }
        else toast(d.message, "error");
      });
    });
  });
})();
