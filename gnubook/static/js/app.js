/* gnubook front-end helpers (no inline scripts: CSP script-src 'self'). */
(function () {
  'use strict';

  function store(key, value) {
    try {
      if (value === undefined) { return localStorage.getItem(key); }
      localStorage.setItem(key, value);
    } catch (e) { return null; }
    return null;
  }

  /* ---------------------------------------------------------------- theme toggle */
  var themeBtn = document.getElementById('theme-toggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var html = document.documentElement;
      var next = html.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-bs-theme', next);
      store('gnubook-theme', next);
    });
  }

  /* ---------------------------------------------------------------- clickable rows, confirms */
  document.addEventListener('click', function (ev) {
    var row = ev.target.closest('tr.clickable[data-href]');
    if (!row || ev.target.closest('a, button, input, select, label, form')) { return; }
    if (window.getSelection && String(window.getSelection()).length > 0) { return; }
    window.location.href = row.getAttribute('data-href');
  });
  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      if (!window.confirm(form.getAttribute('data-confirm'))) { ev.preventDefault(); }
    });
  });
  document.querySelectorAll('button[data-confirm-button]').forEach(function (btn) {
    btn.addEventListener('click', function (ev) {
      if (!window.confirm(btn.getAttribute('data-confirm-button'))) { ev.preventDefault(); }
    });
  });
  document.querySelectorAll('[data-select-all]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var boxes = document.querySelectorAll(btn.getAttribute('data-select-all'));
      var allOn = Array.prototype.every.call(boxes, function (b) { return b.checked || b.disabled; });
      boxes.forEach(function (b) { if (!b.disabled) { b.checked = !allOn; } });
    });
  });

  /* ---------------------------------------------------------------- account tree */
  var tree = document.getElementById('account-tree');
  if (tree) {
    var rows = Array.prototype.slice.call(tree.querySelectorAll('tbody tr'));
    var byParent = {};
    rows.forEach(function (r) {
      var p = r.getAttribute('data-parent');
      (byParent[p] = byParent[p] || []).push(r);
    });
    var collapsed = {};
    try { collapsed = JSON.parse(store('gnubook-tree') || 'null') || null; } catch (e) { collapsed = null; }
    if (!collapsed) {  // default: show three levels
      collapsed = {};
      rows.forEach(function (r) {
        if (parseInt(r.getAttribute('data-depth'), 10) >= 2 && byParent[r.getAttribute('data-guid')]) {
          collapsed[r.getAttribute('data-guid')] = true;
        }
      });
    }
    var apply = function () {
      var hiddenBelow = {};
      rows.forEach(function (r) {
        var guid = r.getAttribute('data-guid');
        var parent = r.getAttribute('data-parent');
        var hide = !!hiddenBelow[parent];
        r.classList.toggle('d-none', hide);
        r.classList.toggle('collapsed', !!collapsed[guid]);
        hiddenBelow[guid] = hide || !!collapsed[guid];
      });
    };
    var save = function () { store('gnubook-tree', JSON.stringify(collapsed)); };
    tree.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.tree-toggle');
      if (!btn) { return; }
      var guid = btn.closest('tr').getAttribute('data-guid');
      collapsed[guid] = !collapsed[guid];
      apply(); save();
    });
    document.querySelectorAll('[data-tree]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        collapsed = {};
        if (btn.getAttribute('data-tree') === 'collapse') {
          rows.forEach(function (r) { if (byParent[r.getAttribute('data-guid')]) { collapsed[r.getAttribute('data-guid')] = true; } });
        }
        apply(); save();
      });
    });
    var filter = document.getElementById('tree-filter');
    if (filter) {
      filter.addEventListener('input', function () {
        var q = filter.value.trim().toLowerCase();
        if (!q) { apply(); return; }
        rows.forEach(function (r) { r.classList.toggle('d-none', r.getAttribute('data-name').indexOf(q) === -1); });
      });
    }
    apply();
  }

  /* ---------------------------------------------------------------- amounts */
  function parseNumber(s) {
    s = s.replace(/[\s' ]/g, '');
    if (s.indexOf(',') !== -1) {
      var parts = s.split(',');
      if (parts.length !== 2) { return NaN; }
      if (parts[0].indexOf('.') !== -1 && !/^\d{1,3}(\.\d{3})+$/.test(parts[0])) { return NaN; }
      s = parts[0].replace(/\./g, '') + '.' + parts[1];
    } else if ((s.match(/\./g) || []).length === 1) {
      var p = s.split('.');
      if (p[1].length === 3 && p[0] && p[0] !== '0') { s = p[0] + p[1]; }
    } else if ((s.match(/\./g) || []).length > 1) {
      if (!/^\d{1,3}(\.\d{3})+$/.test(s)) { return NaN; }
      s = s.replace(/\./g, '');
    }
    if (!/^(\d+\.?\d*|\.\d+)$/.test(s)) { return NaN; }
    return parseFloat(s);
  }

  function evalAmount(text) {  // returns number, null for empty, NaN for errors
    if (text === undefined || text === null || !String(text).trim()) { return null; }
    var src = String(text).replace(/−/g, '-').replace(/[×]/g, '*').replace(/:/g, '/');
    var tokens = [];
    var re = /\s*(\d[\d.,']*|[.,]\d+|[-+*/()])/y;
    var pos = 0;
    while (pos < src.length) {
      if (/\s/.test(src[pos])) { pos++; continue; }
      re.lastIndex = pos;
      var m = re.exec(src);
      if (!m) { return NaN; }
      tokens.push(m[1]);
      pos = re.lastIndex;
    }
    var i = 0;
    function peek() { return tokens[i]; }
    function take() { return tokens[i++]; }
    function factor() {
      var t = take();
      if (t === '-') { return -factor(); }
      if (t === '+') { return factor(); }
      if (t === '(') { var v = expr(); if (take() !== ')') { return NaN; } return v; }
      if (t === undefined) { return NaN; }
      return parseNumber(t);
    }
    function term() {
      var v = factor();
      while (peek() === '*' || peek() === '/') {
        var op = take(); var r = factor();
        v = op === '*' ? v * r : (r === 0 ? NaN : v / r);
      }
      return v;
    }
    function expr() {
      var v = term();
      while (peek() === '+' || peek() === '-') {
        var op = take(); var r = term();
        v = op === '+' ? v + r : v - r;
      }
      return v;
    }
    var value = expr();
    if (i < tokens.length) { return NaN; }
    return Math.round(value * 100) / 100;
  }

  function fmt(value) {
    var neg = value < 0;
    var s = Math.abs(value).toFixed(2).split('.');
    s[0] = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return (neg ? '−' : '') + s[0] + ',' + s[1];
  }

  /* ---------------------------------------------------------------- transaction editor */
  var form = document.getElementById('tx-form');
  if (form) {
    var tbody = form.querySelector('#split-table tbody');
    var master = document.getElementById('account-master');
    var template = document.getElementById('row-template');
    var imbalanceEl = document.getElementById('imbalance');
    var imbalanceBox = document.getElementById('imbalance-box');
    var nextIndex = tbody.querySelectorAll('tr.split-row').length;
    var touched = false;

    var initSelect = function (sel) {
      if (typeof TomSelect === 'undefined' || sel.disabled || sel.tomselect) { return; }
      new TomSelect(sel, {
        maxOptions: 400, allowEmptyOption: true, placeholder: 'Konto wählen …',
        searchField: ['text'], sortField: [{ field: '$score' }, { field: '$order' }],
        onChange: function () { touched = true; },
      });
    };
    tbody.querySelectorAll('select.account-select').forEach(initSelect);

    var rowsArr = function () { return Array.prototype.slice.call(tbody.querySelectorAll('tr.split-row')); };
    var field = function (row, name) { return row.querySelector('[name$="-' + name + '"]'); };

    var addRow = function (data) {
      var frag = template.content.cloneNode(true);
      var row = frag.querySelector('tr');
      row.querySelectorAll('[data-name]').forEach(function (el) {
        el.name = 'split-' + nextIndex + '-' + el.getAttribute('data-name');
      });
      var sel = row.querySelector('select');
      sel.innerHTML = master.innerHTML;
      data = data || {};
      if (data.account) { sel.value = data.account; }
      if (data.memo) { field(row, 'memo').value = data.memo; }
      if (data.debit) { field(row, 'debit').value = data.debit; }
      if (data.credit) { field(row, 'credit').value = data.credit; }
      nextIndex += 1;
      tbody.appendChild(row);
      initSelect(sel);
      return row;
    };

    var rowValue = function (row) {
      var d = evalAmount(field(row, 'debit').value);
      var c = evalAmount(field(row, 'credit').value);
      if ((d !== null && isNaN(d)) || (c !== null && isNaN(c))) { return NaN; }
      return (d || 0) - (c || 0);
    };

    var recalc = function () {
      var total = 0; var bad = false;
      rowsArr().forEach(function (row) {
        var v = rowValue(row);
        if (isNaN(v)) { bad = true; } else { total += v; }
        ['debit', 'credit'].forEach(function (side) {
          var inp = field(row, side);
          var val = evalAmount(inp.value);
          inp.classList.toggle('is-invalid', val !== null && isNaN(val));
        });
      });
      total = Math.round(total * 100) / 100;
      imbalanceEl.textContent = bad ? 'Eingabefehler' : fmt(total);
      imbalanceBox.classList.toggle('unbalanced', bad || total !== 0);
      imbalanceBox.classList.toggle('balanced', !bad && total === 0);
      return bad ? NaN : total;
    };

    var autoMirror = function (sourceRow) {  // two rows: second row mirrors the first
      var rs = rowsArr();
      if (rs.length !== 2 || rs[0] !== sourceRow) { return; }
      var other = rs[1];
      var od = field(other, 'debit'); var oc = field(other, 'credit');
      if ((od.value || oc.value) && other.getAttribute('data-auto') !== '1') { return; }
      var v = rowValue(sourceRow);
      if (isNaN(v)) { return; }
      od.value = v < 0 ? fmt(-v).replace('−', '') : '';
      oc.value = v > 0 ? fmt(v) : '';
      other.setAttribute('data-auto', '1');
    };

    tbody.addEventListener('input', function (ev) {
      var inp = ev.target;
      if (!inp.classList.contains('amount-input')) { return; }
      touched = true;
      var row = inp.closest('tr');
      row.removeAttribute('data-auto');
      if (inp.value.trim()) {  // a split is either debit or credit
        var other = field(row, inp.getAttribute('data-side') === 'debit' ? 'credit' : 'debit');
        if (other.value && !/[+\-*/]/.test(inp.value)) { other.value = ''; }
      }
      autoMirror(row);
      recalc();
    });
    tbody.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.remove-row');
      if (!btn) { return; }
      var rs = rowsArr();
      if (rs.length <= 1) { return; }
      btn.closest('tr').remove();
      touched = true;
      recalc();
    });
    document.getElementById('add-row').addEventListener('click', function () { addRow(); });
    document.getElementById('balance-btn').addEventListener('click', function () {
      var total = recalc();
      if (isNaN(total) || total === 0) { return; }
      var target = null;
      rowsArr().forEach(function (row) {
        if (!field(row, 'debit').value && !field(row, 'credit').value && !field(row, 'debit').readOnly) { target = row; }
      });
      if (!target) { target = addRow(); }
      if (total > 0) { field(target, 'credit').value = fmt(total); } else { field(target, 'debit').value = fmt(-total).replace('−', ''); }
      recalc();
    });
    form.addEventListener('submit', function (ev) {
      var total = recalc();
      if (isNaN(total)) {
        ev.preventDefault();
        window.alert('Bitte die markierten Beträge korrigieren.');
      } else if (total !== 0) {
        ev.preventDefault();
        window.alert('Die Buchung ist nicht ausgeglichen (Differenz ' + fmt(total) + '). ' +
                     '„Ausgleichen“ übernimmt die Differenz in eine offene Zeile.');
      }
    });

    /* description suggestions and GnuCash-like quickfill */
    var desc = document.getElementById('description');
    var list = document.getElementById('description-list');
    var suggestUrl = form.getAttribute('data-suggest-url');
    var templateUrl = form.getAttribute('data-template-url');
    var context = form.getAttribute('data-context');
    var lastSuggestions = [];
    var timer = null;
    desc.addEventListener('input', function () {
      clearTimeout(timer);
      var q = desc.value.trim();
      if (q.length < 2) { return; }
      timer = setTimeout(function () {
        fetch(suggestUrl + '?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (items) {
            lastSuggestions = items;
            list.innerHTML = '';
            items.forEach(function (it) { var o = document.createElement('option'); o.value = it; list.appendChild(o); });
          }).catch(function () {});
      }, 200);
    });
    desc.addEventListener('change', function () {
      if (form.getAttribute('data-mode') !== 'new' || touched) { return; }
      if (lastSuggestions.indexOf(desc.value) === -1) { return; }
      fetch(templateUrl + '?description=' + encodeURIComponent(desc.value) + '&account=' + encodeURIComponent(context),
            { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (tpl) {
          if (!tpl || !tpl.splits || touched) { return; }
          rowsArr().forEach(function (row) {
            var sel = row.querySelector('select');
            if (sel && sel.tomselect) { sel.tomselect.destroy(); }
            row.remove();
          });
          tpl.splits.forEach(function (s) { addRow(s); });
          var notes = document.getElementById('notes');
          if (notes && !notes.value && tpl.notes) { notes.value = tpl.notes; }
          recalc();
        }).catch(function () {});
    });
    recalc();
  }
})();
