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
