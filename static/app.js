// Vanilla JS, no framework -- two independent small things:
// 1) the mobile sidebar toggle (hamburger button).
// 2) the "koppel factuur" search picker (replaces what used to be a single
//    <select> listing every open factuur as an <option>, rendered once per
//    unresolved transaction -- 2,3 miljoen <option>-tags on a busy
//    production dashboard). Debounced, fetches a handful of matches from
//    /invoices/search-options instead of shipping the whole list up front.

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

  document.addEventListener("DOMContentLoaded", function () {
    setupSidebar();
    setupInvoicePickers();
  });
})();
