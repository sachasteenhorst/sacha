// Vanilla JS, no framework -- three independent small things:
// 1) the mobile sidebar toggle (hamburger button).
// 2) the "koppel factuur" search picker (replaces what used to be a single
//    <select> listing every open factuur as an <option>, rendered once per
//    unresolved transaction -- 2,3 miljoen <option>-tags on a busy
//    production dashboard). Debounced, fetches a handful of matches from
//    /invoices/search-options instead of shipping the whole list up front.
// 3) dashboard list actions (Klopt/Koppelen/Al betaald/etc.) sent via
//    fetch() so the row just disappears and the page never jumps back to
//    the top -- see app/main.py's _is_ajax/_action_response for the
//    server side of this (it returns JSON here, the normal redirect
//    otherwise, so this still works fine with JS off).

(function () {
  "use strict";

  function setupSidebar() {
    var toggle = document.getElementById("sidebar-toggle");
    var sidebar = document.getElementById("sidebar");
    var backdrop = document.getElementById("sidebar-backdrop");
    if (!toggle || !sidebar || !backdrop) return;

    function close() {
      sidebar.classList.remove("open");
      backdrop.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
    }
    function open() {
      sidebar.classList.add("open");
      backdrop.classList.add("open");
      toggle.setAttribute("aria-expanded", "true");
    }
    toggle.addEventListener("click", function () {
      if (sidebar.classList.contains("open")) close();
      else open();
    });
    backdrop.addEventListener("click", close);
  }

  var DEBOUNCE_MS = 250;
  var debounceTimers = new WeakMap();

  function debounce(el, fn) {
    var existing = debounceTimers.get(el);
    if (existing) clearTimeout(existing);
    debounceTimers.set(el, setTimeout(fn, DEBOUNCE_MS));
  }

  function renderResults(picker, items) {
    var results = picker.querySelector(".invoice-picker-results");
    results.innerHTML = "";
    if (!items.length) {
      results.hidden = true;
      return;
    }
    items.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "invoice-picker-result";
      row.textContent = item.label;
      row.dataset.id = item.id;
      row.dataset.label = item.label;
      row.addEventListener("click", function () {
        addChip(picker, item.id, item.label);
        var input = picker.querySelector(".invoice-picker-search");
        input.value = "";
        results.hidden = true;
        results.innerHTML = "";
      });
      results.appendChild(row);
    });
    results.hidden = false;
  }

  function addChip(picker, id, label) {
    var chips = picker.querySelector(".invoice-picker-chips");
    if (chips.querySelector('input[value="' + id + '"]')) return; // already added
    var chip = document.createElement("span");
    chip.className = "invoice-picker-chip";
    var text = document.createElement("span");
    text.textContent = label;
    var hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "invoice_ids";
    hidden.value = id;
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "invoice-picker-chip-remove";
    remove.textContent = "×";
    remove.addEventListener("click", function () {
      chip.remove();
      updateSubmitState(picker);
    });
    chip.appendChild(text);
    chip.appendChild(hidden);
    chip.appendChild(remove);
    chips.appendChild(chip);
    updateSubmitState(picker);
  }

  function updateSubmitState(picker) {
    var button = picker.querySelector('button[type="submit"]');
    var hasChips = picker.querySelectorAll(".invoice-picker-chips input").length > 0;
    button.disabled = !hasChips;
  }

  function fetchAndRender(picker, query) {
    var amountCents = picker.dataset.amountCents ? Math.abs(parseInt(picker.dataset.amountCents, 10)) : "";
    var params = new URLSearchParams();
    if (query) params.set("q", query);
    if (amountCents) params.set("amount_cents", amountCents);
    fetch("/invoices/search-options?" + params.toString())
      .then(function (response) { return response.ok ? response.json() : []; })
      .then(function (items) { renderResults(picker, items || []); })
      .catch(function () { /* a failed search is never fatal -- just no suggestions */ });
  }

  function setupInvoicePickers() {
    document.querySelectorAll(".invoice-picker").forEach(function (picker) {
      var input = picker.querySelector(".invoice-picker-search");
      if (!input) return;
      input.addEventListener("input", function () {
        debounce(input, function () { fetchAndRender(picker, input.value.trim()); });
      });
      input.addEventListener("focus", function () {
        if (!input.value) fetchAndRender(picker, "");
      });
    });
    document.addEventListener("click", function (event) {
      document.querySelectorAll(".invoice-picker-results").forEach(function (results) {
        if (!results.hidden && !results.closest(".invoice-picker").contains(event.target)) {
          results.hidden = true;
        }
      });
    });
  }

  function decrementCounters(namesStr) {
    (namesStr || "").split(",").forEach(function (raw) {
      var name = raw.trim();
      if (!name) return;
      document.querySelectorAll('[data-counter="' + name + '"]').forEach(function (el) {
        var n = parseInt(el.textContent, 10);
        if (!isNaN(n) && n > 0) el.textContent = String(n - 1);
      });
    });
  }

  function removeRow(row, counterTargets) {
    if (row) {
      row.classList.add("ajax-row-removing");
      setTimeout(function () {
        if (row.parentNode) row.parentNode.removeChild(row);
      }, 200);
    }
    decrementCounters(counterTargets);
  }

  function showRowError(row, message) {
    if (!row) return;
    var cell = row.querySelector("td.actions") || row;
    var existing = cell.querySelector(".ajax-row-error");
    if (existing) existing.remove();
    var msg = document.createElement("div");
    msg.className = "ajax-row-error";
    msg.textContent = "⚠ " + (message || "Mislukt, probeer het opnieuw.");
    cell.appendChild(msg);
  }

  // Fallback when fetch itself fails (offline, JS error, non-JSON reply):
  // builds a plain hidden form with the same fields and submits it for
  // real, so the existing redirect-with-anchor behaviour on the server
  // still kicks in -- same end result as if JS had never intercepted it.
  function submitViaUrl(sourceForm, url) {
    var tempForm = document.createElement("form");
    tempForm.method = "post";
    tempForm.action = url;
    tempForm.style.display = "none";
    sourceForm.querySelectorAll("input[name], select[name], textarea[name]").forEach(function (el) {
      if (el.disabled) return;
      if ((el.type === "checkbox" || el.type === "radio") && !el.checked) return;
      var clone = document.createElement("input");
      clone.type = "hidden";
      clone.name = el.name;
      clone.value = el.value;
      tempForm.appendChild(clone);
    });
    document.body.appendChild(tempForm);
    tempForm.submit();
  }

  function postRow(form, url, row, counterTargets) {
    var formData = new FormData(form);
    fetch(url, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
      body: formData,
    })
      .then(function (response) {
        return response.json().then(function (data) {
          return { data: data };
        });
      })
      .then(function (result) {
        if (result.data && result.data.ok === false) {
          showRowError(row, result.data.error);
          return;
        }
        removeRow(row, counterTargets);
      })
      .catch(function () {
        submitViaUrl(form, url);
      });
  }

  function setupAjaxRowActions() {
    document.querySelectorAll("form.ajax-row-form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var submitter = event.submitter;
        // A submit button with its own formaction (the bulk "Niet naar
        // Basecone" table) is handled by setupAjaxRowButtons instead --
        // let this one through untouched.
        if (submitter && submitter.hasAttribute("formaction")) return;
        event.preventDefault();
        var row = form.closest("tr");
        postRow(form, form.getAttribute("action"), row, form.dataset.counterTargets);
      });
    });

    document.querySelectorAll("button.ajax-row-button[formaction]").forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        var form = button.closest("form");
        var row = button.closest("tr");
        postRow(form, button.getAttribute("formaction"), row, button.dataset.counterTargets);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupSidebar();
    setupInvoicePickers();
    setupAjaxRowActions();
  });
})();
