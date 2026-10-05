(function () {
  document.documentElement.classList.add("js");

  // Tabs on the run page, driven by the URL hash so they can be linked.
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  function showTab(id) {
    var found = tabs.some(function (t) { return t.getAttribute("href") === "#" + id; });
    if (!found && tabs.length) id = tabs[0].getAttribute("href").slice(1);
    tabs.forEach(function (t) {
      var on = t.getAttribute("href") === "#" + id;
      t.setAttribute("aria-selected", on ? "true" : "false");
      var panel = document.getElementById(t.getAttribute("href").slice(1));
      if (panel) panel.classList.toggle("active", on);
    });
    var filters = document.querySelector("[data-filters]");
    if (filters) filters.hidden = id === "fix";
  }
  if (tabs.length) {
    tabs.forEach(function (t) {
      t.addEventListener("click", function (e) {
        e.preventDefault();
        var id = t.getAttribute("href").slice(1);
        history.replaceState(null, "", "#" + id);
        showTab(id);
      });
    });
    showTab(location.hash.slice(1));
    window.addEventListener("hashchange", function () { showTab(location.hash.slice(1)); });
  }

  // Severity and text filter for findings tables.
  var filters = document.querySelector("[data-filters]");
  if (filters) {
    var apply = function () {
      var severities = Array.prototype.slice.call(filters.querySelectorAll("input[type=checkbox]"))
        .filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
      var text = filters.querySelector("input[type=search]").value.trim().toLowerCase();
      document.querySelectorAll("[data-filterable] tbody tr").forEach(function (row) {
        var visible = severities.indexOf(row.dataset.severity) !== -1 &&
          (!text || row.dataset.text.toLowerCase().indexOf(text) !== -1);
        row.hidden = !visible;
      });
    };
    filters.addEventListener("input", apply);
    filters.addEventListener("change", apply);
  }

  // Copy buttons.
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.querySelector(button.dataset.copy);
      if (!target || !navigator.clipboard) return;
      navigator.clipboard.writeText(target.textContent).then(function () {
        var label = button.textContent;
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = label; }, 1500);
      });
    });
  });

  // Long-running forms: disable the button and say what is happening.
  document.querySelectorAll("[data-busy-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]");
      if (button) button.disabled = true;
      var hint = form.querySelector(".busy-hint");
      if (hint) hint.hidden = false;
    });
  });
})();

(function () {
  // Filter editor: add / remove condition rows.
  document.querySelectorAll("[data-filter-editor]").forEach(function (editor) {
    editor.addEventListener("click", function (e) {
      if (e.target.matches("[data-add-filter]")) {
        var rows = editor.querySelectorAll("[data-filter-row]");
        var copy = rows[rows.length - 1].cloneNode(true);
        copy.querySelectorAll("input").forEach(function (i) { i.value = ""; });
        copy.querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
        e.target.before(copy);
      } else if (e.target.matches("[data-remove-filter]")) {
        var row = e.target.closest("[data-filter-row]");
        if (editor.querySelectorAll("[data-filter-row]").length > 1) row.remove();
        else row.querySelectorAll("input").forEach(function (i) { i.value = ""; });
      }
    });
  });

  // Table search on the tables page.
  var search = document.querySelector("[data-table-search]");
  if (search) {
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      document.querySelectorAll("[data-searchable] tbody tr").forEach(function (tr) {
        tr.hidden = q && tr.dataset.name.indexOf(q) === -1;
      });
    });
  }

  // Danger confirmation: the button stays disabled until the phrase matches exactly.
  document.querySelectorAll("[data-confirm-form]").forEach(function (form) {
    var phrase = form.querySelector("[data-phrase]").textContent.trim();
    var input = form.querySelector("[data-confirm-input]");
    var ack = form.querySelector("[data-confirm-ack]");
    var button = form.querySelector("[data-confirm-button]");
    var check = function () {
      var ok = input.value.trim() === phrase && (!ack || ack.checked);
      input.classList.toggle("matches", input.value.trim() === phrase);
      button.disabled = !ok;
    };
    input.addEventListener("input", check);
    if (ack) ack.addEventListener("change", check);
    input.addEventListener("paste", function (e) { e.preventDefault(); });  // type it, don't paste it
    form.addEventListener("submit", function () { button.disabled = true; button.textContent = "Running…"; });
  });

  // Simple confirm() for low-stakes deletes (recipes).
  document.querySelectorAll("[data-confirm-message]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirmMessage)) e.preventDefault();
    });
  });

  // Job page: poll progress, go to the result when done.
  var jobEl = document.querySelector("[data-job]");
  if (jobEl && ["queued", "running"].indexOf(jobEl.dataset.jobStatus) !== -1) {
    var poll = function () {
      fetch(jobEl.dataset.job, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          jobEl.querySelector("[data-job-bar]").style.width = data.progress + "%";
          jobEl.querySelector("[data-job-state]").textContent = data.status;
          jobEl.querySelector("[data-job-message]").textContent = data.message;
          if (data.finished) {
            if (data.status === "done" && data.result_url) window.location = data.result_url;
            else window.location.reload();
          } else {
            setTimeout(poll, 1000);
          }
        })
        .catch(function () { setTimeout(poll, 3000); });
    };
    setTimeout(poll, 800);
  }
})();

(function () {
  // Compare form: show the fields for the chosen mode.
  var sw = document.querySelector("[data-mode-switch]");
  if (!sw) return;
  var apply = function () {
    var checked = sw.querySelector("input:checked");
    var mode = checked ? checked.value : "branch";
    document.querySelectorAll("[data-mode]").forEach(function (el) { el.hidden = el.dataset.mode !== mode; });
  };
  sw.addEventListener("change", apply);
  apply();
})();
