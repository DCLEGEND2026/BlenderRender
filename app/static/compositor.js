// Compositor page: a node editor like Blender's for one render, with a live preview.
// Nodes and wires live in `graph` ({nodes, links}, see compositing.py); every change
// asks the server for a new preview, which a running Blender makes in a moment.
(function () {
    'use strict';

    var NODES = JSON.parse(document.getElementById('comp-nodes').textContent);
    var DEFAULT_GRAPH = JSON.parse(document.getElementById('default-graph').textContent);
    var JOB_ID = document.body.dataset.job;
    var DRAFT_KEY = 'comp-draft:' + JOB_ID;
    var NODE_W = 200;

    var SPEC = {};
    NODES.forEach(function (n) { SPEC[n.id] = n; });

    var canvas = document.getElementById('canvas');
    var world = document.getElementById('world');
    var wires = document.getElementById('wires');
    var menu = document.getElementById('menu'), menuSearch = document.getElementById('menu-search'), menuList = document.getElementById('menu-list');
    var pimg = document.getElementById('pimg'), pmsg = document.getElementById('pmsg'), pstat = document.getElementById('pstat');
    var msg = document.getElementById('msg');
    var applyBtn = document.getElementById('apply');
    var frameInput = document.getElementById('frame'), sizeSelect = document.getElementById('size');

    var graph = null, job = null, applied = '';
    var view = { x: 0, y: 0, zoom: 1 };
    var selected = {};          // node id -> true
    var els = {};               // node id -> element
    var undoStack = [], redoStack = [];
    var drag = null;            // what the pointer is doing right now
    var pointer = { x: 0, y: 0 };  // last pointer position, in world units

    // ---- Small helpers ---------------------------------------------------------------
    function make(tag, cls, text) {
        var el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text !== undefined) el.textContent = text;
        return el;
    }
    function svg(tag, cls) {
        var el = document.createElementNS('http://www.w3.org/2000/svg', tag);
        if (cls) el.setAttribute('class', cls);
        return el;
    }
    function api(method, url, body) {
        return fetch(url, {
            method: method,
            headers: body ? { 'Content-Type': 'application/json' } : {},
            body: body ? JSON.stringify(body) : undefined,
            credentials: 'same-origin'
        }).then(function (r) {
            if (r.status === 401) { location.href = '/login'; throw new Error('Not logged in'); }
            return r.json().catch(function () { return {}; }).then(function (d) {
                if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
                return d;
            });
        });
    }
    function showMsg(text, isErr) {
        msg.textContent = text || '';
        msg.hidden = !text;
        msg.classList.toggle('err', !!isErr);
    }
    function toHex(rgb) {
        return '#' + rgb.map(function (v) { return ('0' + Math.round(v * 255).toString(16)).slice(-2); }).join('');
    }
    function fromHex(hex) {
        return [1, 3, 5].map(function (i) { return parseInt(hex.substr(i, 2), 16) / 255; });
    }
    function copy(v) { return JSON.parse(JSON.stringify(v)); }
    // JSON with sorted keys, to compare graphs (the server sends its keys sorted).
    function canon(v) {
        return JSON.stringify(v, function (k, x) {
            if (!x || typeof x !== 'object' || Array.isArray(x)) return x;
            var o = {};
            Object.keys(x).sort().forEach(function (key) { o[key] = x[key]; });
            return o;
        });
    }
    function nodeById(id) {
        for (var i = 0; i < graph.nodes.length; i++) if (graph.nodes[i].id === id) return graph.nodes[i];
        return null;
    }
    function linkInto(id, input) {
        for (var i = 0; i < graph.links.length; i++) {
            var l = graph.links[i];
            if (l.to[0] === id && l.to[1] === input) return l;
        }
        return null;
    }
    function newId(type) {
        var i = 1;
        while (nodeById(type + '_' + i)) i++;
        return type + '_' + i;
    }
    function valuesOf(spec) {
        return spec.params.concat(spec.inputs.filter(function (i) { return i.value; }).map(function (i) { return i.value; }));
    }
    function round(v, step) {
        var d = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step)));
        return Number(v.toFixed(d));
    }
    function fmt(v, p) {
        if (p.kind === 'int') return String(Math.round(v));
        if (p.kind === 'angle') return Math.round(v) + '°';
        return String(round(v, p.step || 0.01));
    }

    // ---- Undo and saving -------------------------------------------------------------
    // before() is called once at the start of every change, so one undo step = one change.
    function before() {
        var now = JSON.stringify(graph);
        if (undoStack[undoStack.length - 1] !== now) undoStack.push(now);
        if (undoStack.length > 100) undoStack.shift();
        redoStack = [];
    }
    function restore(from, to) {
        if (!from.length) return;
        to.push(JSON.stringify(graph));
        graph = JSON.parse(from.pop());
        selected = {};
        drawAll();
        changed(true);
    }
    function changed(preview) {
        try { localStorage.setItem(DRAFT_KEY, JSON.stringify(graph)); } catch (e) { /* private mode */ }
        updateApply();
        if (preview) schedulePreview();
    }
    function updateApply() {
        var busy = job && ['preparing', 'queued', 'running', 'compositing', 'encoding'].indexOf(job.status) !== -1;
        applyBtn.disabled = !job || !job.raw_frames || busy;
        var live = job && (job.status === 'compositing' || job.status === 'encoding');
        applyBtn.textContent = live ? 'Applying… ' + clockText(applyElapsed()) : busy && job.status !== 'running' ? 'Applying…' :
            (canon(graph) !== applied ? 'Apply to render •' : 'Apply to render');
    }

    // ---- Node widgets ----------------------------------------------------------------
    // A Blender-style number field: drag sideways to change, click to type.
    function numberField(p, label, get, set) {
        var f = make('div', 'nf'), fill = make('i', 'nf-fill'), name = make('span', 'nf-name', label), val = make('span', 'nf-val');
        var span = p.max - p.min, showFill = span <= 10 && p.min >= 0;
        function paint() {
            var v = get();
            val.textContent = fmt(v, p);
            fill.style.width = showFill ? ((v - p.min) / span * 100) + '%' : '0';
        }
        f.appendChild(fill);
        f.appendChild(name);
        f.appendChild(val);
        f.addEventListener('pointerdown', function (e) {
            if (e.button !== 0 || f.querySelector('input')) return;
            e.stopPropagation();
            e.preventDefault();
            var startX = e.clientX, start = get(), moved = false;
            var perPx = span <= 400 ? span / 400 : 1;
            f.setPointerCapture(e.pointerId);
            function move(ev) {
                var dx = ev.clientX - startX;
                if (!moved && Math.abs(dx) < 3) return;
                if (!moved) before();
                moved = true;
                var v = start + dx * perPx * (ev.shiftKey ? 0.1 : 1);  // Shift = fine
                v = Math.min(p.max, Math.max(p.min, v));
                set(p.kind === 'int' ? Math.round(v) : round(v, Math.min(p.step || 0.01, 0.001)));
                paint();
            }
            function up() {
                f.removeEventListener('pointermove', move);
                f.removeEventListener('pointerup', up);
                f.removeEventListener('pointercancel', up);
                if (!moved) edit();
            }
            f.addEventListener('pointermove', move);
            f.addEventListener('pointerup', up);
            f.addEventListener('pointercancel', up);
        });
        function edit() {
            var box = make('input', 'nf-input');
            box.type = 'number';
            box.step = 'any';
            box.value = round(get(), p.step || 0.01);
            f.appendChild(box);
            box.focus();
            box.select();
            var done = false;
            function finish(save) {
                if (done) return;
                done = true;
                var v = Number(box.value);
                if (save && box.value !== '' && !isNaN(v)) {
                    before();
                    set(Math.min(p.max, Math.max(p.min, p.kind === 'int' ? Math.round(v) : v)));
                }
                box.remove();
                paint();
            }
            box.addEventListener('keydown', function (e) {
                e.stopPropagation();
                if (e.key === 'Enter') finish(true);
                if (e.key === 'Escape') finish(false);
            });
            box.addEventListener('blur', function () { finish(true); });
        }
        paint();
        return f;
    }

    function widget(p, label, n) {
        var get = function () { return n.values[p.id]; };
        var set = function (v) { n.values[p.id] = v; refreshNode(n); changed(true); };
        if (p.kind === 'choice') {
            var pick = make('label', 'cn-pick'), sel = make('select');
            p.choices.forEach(function (c) { sel.appendChild(make('option', '', c[1])).value = c[0]; });
            sel.value = get();
            sel.addEventListener('change', function () { before(); set(sel.value); });
            pick.appendChild(make('span', '', label));
            pick.appendChild(sel);
            return pick;
        }
        if (p.kind === 'bool') {
            var lab = make('label', 'cn-check'), cb = make('input');
            cb.type = 'checkbox';
            cb.checked = get();
            cb.addEventListener('change', function () { before(); set(cb.checked); });
            lab.appendChild(cb);
            lab.appendChild(document.createTextNode(label));
            return lab;
        }
        if (p.kind === 'color') {
            var wrap = make('label', 'cn-color'), col = make('input');
            col.type = 'color';
            col.value = toHex(get());
            col.addEventListener('focus', before);
            col.addEventListener('input', function () { set(fromHex(col.value)); });
            if (label) wrap.appendChild(make('span', '', label));
            wrap.appendChild(col);
            return wrap;
        }
        return numberField(p, label, get, set);
    }

    // ---- Nodes -----------------------------------------------------------------------
    function nodeEl(n) {
        var spec = SPEC[n.type];
        var el = make('div', 'cn g-' + spec.group.toLowerCase());
        el.dataset.id = n.id;
        var head = make('div', 'cn-head', spec.label);
        head.title = spec.hint || '';
        el.appendChild(head);
        spec.outputs.forEach(function (o) {
            var row = make('div', 'cn-row out');
            row.appendChild(make('span', 'cn-label', o.label));
            var s = make('i', 'sock t-' + o.type);
            s.dataset.out = o.id;
            row.appendChild(s);
            el.appendChild(row);
        });
        spec.params.forEach(function (p) {
            var row = make('div', 'cn-row param');
            row.dataset.param = p.id;
            row.appendChild(widget(p, p.label, n));
            el.appendChild(row);
        });
        spec.inputs.forEach(function (i) {
            var row = make('div', 'cn-row in');
            row.dataset.input = i.id;
            var s = make('i', 'sock t-' + i.type);
            s.dataset.in = i.id;
            row.appendChild(s);
            row.appendChild(make('span', 'cn-label', i.label));
            if (i.value) {
                // While unplugged the input shows its value; numbers show it in place of the name.
                if (i.value.kind !== 'color') row.classList.add('has-val');
                var w = widget(i.value, i.value.kind === 'color' ? '' : i.label, n);
                w.classList.add('cn-inval');
                row.appendChild(w);
            }
            el.appendChild(row);
        });
        return el;
    }

    // Things that depend on values or wires: hidden settings, input widgets, mute, selection.
    function refreshNode(n) {
        var el = els[n.id], spec = SPEC[n.type];
        if (!el) return;
        el.style.left = n.x + 'px';
        el.style.top = n.y + 'px';
        el.classList.toggle('muted', !!n.mute);
        el.classList.toggle('sel', !!selected[n.id]);
        spec.params.forEach(function (p) {
            var when = p.when || {};
            el.querySelector('[data-param="' + p.id + '"]').hidden = Object.keys(when).some(function (k) {
                return when[k].indexOf(n.values[k]) === -1;
            });
        });
        spec.inputs.forEach(function (i) {
            var row = el.querySelector('[data-input="' + i.id + '"]');
            row.classList.toggle('linked', !!linkInto(n.id, i.id));
        });
    }

    function drawAll() {
        Object.keys(els).forEach(function (id) { els[id].remove(); });
        els = {};
        graph.nodes.forEach(function (n) {
            els[n.id] = world.appendChild(nodeEl(n));
            refreshNode(n);
        });
        drawWires();
    }

    // ---- Wires -----------------------------------------------------------------------
    function sockPos(id, kind, sock) {
        var el = els[id] && els[id].querySelector('[data-' + kind + '="' + sock + '"]');
        if (!el || el.offsetParent === null) return null;
        var r = el.getBoundingClientRect(), c = canvas.getBoundingClientRect();
        return { x: (r.left + r.width / 2 - c.left - view.x) / view.zoom, y: (r.top + r.height / 2 - c.top - view.y) / view.zoom };
    }
    function curve(a, b) {
        var dx = Math.max(40, Math.abs(b.x - a.x) / 2);
        return 'M' + a.x + ' ' + a.y + ' C' + (a.x + dx) + ' ' + a.y + ' ' + (b.x - dx) + ' ' + b.y + ' ' + b.x + ' ' + b.y;
    }
    function outType(id, sock) {
        var n = nodeById(id), o = n && SPEC[n.type].outputs.filter(function (x) { return x.id === sock; })[0];
        return o ? o.type : 'color';
    }
    function drawWires() {
        wires.innerHTML = '';
        graph.links.forEach(function (l, i) {
            var a = sockPos(l.from[0], 'out', l.from[1]), b = sockPos(l.to[0], 'in', l.to[1]);
            if (!a || !b) return;
            var d = curve(a, b);
            var hit = wires.appendChild(svg('path', 'wire-hit'));
            hit.setAttribute('d', d);
            hit.dataset.link = i;
            var muted = (nodeById(l.from[0]) || {}).mute;
            var path = wires.appendChild(svg('path', 'wire t-' + outType(l.from[0], l.from[1]) + (muted ? ' muted' : '')));
            path.setAttribute('d', d);
            path.dataset.link = i;
        });
        if (drag && drag.type === 'link' && drag.end) {
            var tmp = wires.appendChild(svg('path', 'wire temp'));
            tmp.setAttribute('d', drag.fromOut ? curve(drag.start, drag.end) : curve(drag.end, drag.start));
        }
    }

    function connect(from, to) {
        graph.links = graph.links.filter(function (l) { return !(l.to[0] === to[0] && l.to[1] === to[1]); });
        if (from[0] === to[0] || reaches(to[0], from[0])) return false;  // no loops
        graph.links.push({ from: from, to: to });
        return true;
    }
    function reaches(a, b) {
        var seen = {}, todo = [a];
        while (todo.length) {
            var cur = todo.pop();
            if (cur === b) return true;
            if (seen[cur]) continue;
            seen[cur] = true;
            graph.links.forEach(function (l) { if (l.from[0] === cur) todo.push(l.to[0]); });
        }
        return false;
    }

    // ---- View ------------------------------------------------------------------------
    function applyView() {
        world.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.zoom + ')';
        var g = 24 * view.zoom;
        canvas.style.backgroundSize = g + 'px ' + g + 'px';
        canvas.style.backgroundPosition = view.x + 'px ' + view.y + 'px';
    }
    function zoomAt(factor, cx, cy) {
        var z = Math.min(2.5, Math.max(0.2, view.zoom * factor));
        view.x = cx - (cx - view.x) * z / view.zoom;
        view.y = cy - (cy - view.y) * z / view.zoom;
        view.zoom = z;
        applyView();
    }
    function frameAll() {
        if (!graph.nodes.length) return;
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        graph.nodes.forEach(function (n) {
            var el = els[n.id];
            minX = Math.min(minX, n.x);
            minY = Math.min(minY, n.y);
            maxX = Math.max(maxX, n.x + NODE_W);
            maxY = Math.max(maxY, n.y + (el ? el.offsetHeight : 120));
        });
        var c = canvas.getBoundingClientRect(), pad = 50;
        view.zoom = Math.min(1.2, Math.max(0.2, Math.min((c.width - pad * 2) / (maxX - minX), (c.height - pad * 2) / (maxY - minY))));
        view.x = (c.width - (maxX - minX) * view.zoom) / 2 - minX * view.zoom;
        view.y = (c.height - (maxY - minY) * view.zoom) / 2 - minY * view.zoom;
        applyView();
    }
    function toWorld(e) {
        var c = canvas.getBoundingClientRect();
        return { x: (e.clientX - c.left - view.x) / view.zoom, y: (e.clientY - c.top - view.y) / view.zoom };
    }
    function viewCenter() {
        var c = canvas.getBoundingClientRect();
        return { x: (c.width / 2 - view.x) / view.zoom - NODE_W / 2, y: (c.height / 3 - view.y) / view.zoom };
    }

    // ---- Adding, deleting, muting ------------------------------------------------------
    function addNode(type, at, grab) {
        var spec = SPEC[type], values = {};
        valuesOf(spec).forEach(function (p) { values[p.id] = copy(p.default); });
        before();
        var n = { id: newId(type), type: type, x: Math.round(at.x), y: Math.round(at.y), mute: false, values: values };
        graph.nodes.push(n);
        selected = {};
        selected[n.id] = true;
        els[n.id] = world.appendChild(nodeEl(n));
        graph.nodes.forEach(refreshNode);
        drawWires();
        changed(false);
        if (grab) {  // follows the pointer until you click, like Blender's grab after adding
            drag = { type: 'move', ids: [n.id], start: { x: at.x + NODE_W / 2, y: at.y + 14 }, orig: {}, grab: true, moved: true };
            drag.orig[n.id] = { x: n.x, y: n.y };
            canvas.classList.add('grabbing');
            markWire(wireUnder(n));  // added right on a wire: one click puts it in
        }
        return n;
    }
    function deleteSelected() {
        var ids = Object.keys(selected).filter(function (id) { var n = nodeById(id); return n && !SPEC[n.type].fixed; });
        if (!ids.length) return;
        before();
        ids.forEach(function (id) {
            // Like Blender's dissolve: keep the image flowing past a deleted node.
            var into = graph.links.filter(function (l) { return l.to[0] === id; })[0];
            var outs = graph.links.filter(function (l) { return l.from[0] === id; });
            graph.links = graph.links.filter(function (l) { return l.from[0] !== id && l.to[0] !== id; });
            graph.nodes = graph.nodes.filter(function (n) { return n.id !== id; });
            if (into) outs.forEach(function (l) { connect(into.from, l.to); });
            els[id].remove();
            delete els[id];
        });
        selected = {};
        graph.nodes.forEach(refreshNode);
        drawWires();
        changed(true);
    }
    function muteSelected() {
        var ids = Object.keys(selected).filter(function (id) { var n = nodeById(id); return n && !SPEC[n.type].fixed && n.type !== 'viewer'; });
        if (!ids.length) return;
        before();
        ids.forEach(function (id) { var n = nodeById(id); n.mute = !n.mute; refreshNode(n); });
        drawWires();
        changed(true);
    }
    function duplicateSelected() {
        var ids = Object.keys(selected).filter(function (id) { var n = nodeById(id); return n && !SPEC[n.type].fixed && !SPEC[n.type].single; });
        if (!ids.length) return;
        before();
        var map = {};
        selected = {};
        ids.forEach(function (id) {
            var n = copy(nodeById(id));
            map[id] = n.id = newId(n.type);
            n.x += 40;
            n.y += 40;
            graph.nodes.push(n);
            selected[n.id] = true;
            els[n.id] = world.appendChild(nodeEl(n));
        });
        graph.links.slice().forEach(function (l) {  // keep wires between the copies and wires coming in
            if (map[l.to[0]]) graph.links.push({ from: [map[l.from[0]] || l.from[0], l.from[1]], to: [map[l.to[0]], l.to[1]] });
        });
        graph.nodes.forEach(refreshNode);
        drawWires();
        changed(true);
    }
    // Ctrl+Shift+click: show this node in the preview through the Viewer node.
    function viewNode(n) {
        var spec = SPEC[n.type];
        if (!spec.outputs.length) return;
        before();
        var viewer = graph.nodes.filter(function (x) { return x.type === 'viewer'; })[0];
        if (!viewer) {  // just below the Composite node
            var comp = graph.nodes.filter(function (x) { return x.type === 'composite'; })[0];
            viewer = { id: newId('viewer'), type: 'viewer', x: comp.x, y: comp.y + 110, mute: false, values: {} };
            graph.nodes.push(viewer);
            els[viewer.id] = world.appendChild(nodeEl(viewer));
        }
        connect([n.id, spec.outputs[0].id], [viewer.id, 'image']);
        graph.nodes.forEach(refreshNode);
        drawWires();
        changed(true);
    }
    // Dropping a node on a wire puts it in between, like in Blender: the wire has to pass
    // under the node (the one closest to its middle wins). Only for a node with no wires yet.
    function canInsert(n) {
        var spec = SPEC[n.type];
        return spec.inputs.length && spec.outputs.length &&
            !graph.links.some(function (x) { return x.from[0] === n.id || x.to[0] === n.id; });
    }
    function wireUnder(n) {
        if (!n || !canInsert(n)) return -1;
        var h = els[n.id] ? els[n.id].offsetHeight : 100;
        var cx = n.x + NODE_W / 2, cy = n.y + h / 2, best = -1, bestD = Infinity;
        graph.links.forEach(function (l, i) {
            var a = sockPos(l.from[0], 'out', l.from[1]), b = sockPos(l.to[0], 'in', l.to[1]);
            if (!a || !b) return;
            var dx = Math.max(40, Math.abs(b.x - a.x) / 2);
            for (var k = 0; k <= 40; k++) {  // points along the wire's curve (see curve())
                var t = k / 40, u = 1 - t;
                var x = u * u * u * a.x + 3 * u * u * t * (a.x + dx) + 3 * u * t * t * (b.x - dx) + t * t * t * b.x;
                var y = u * u * u * a.y + 3 * u * u * t * a.y + 3 * u * t * t * b.y + t * t * t * b.y;
                if (x < n.x || x > n.x + NODE_W || y < n.y || y > n.y + h) continue;
                var d = Math.abs(x - cx) + Math.abs(y - cy);
                if (d < bestD) { bestD = d; best = i; }
            }
        });
        return best;
    }
    function markWire(index) {
        Array.prototype.forEach.call(wires.querySelectorAll('.wire'), function (w) {
            w.classList.toggle('hot', Number(w.dataset.link) === index);
        });
    }
    function insertOnWire(n, linkIndex) {
        var spec = SPEC[n.type], l = graph.links[linkIndex];
        if (!l || !canInsert(n) || l.from[0] === n.id || l.to[0] === n.id) return;
        var type = outType(l.from[0], l.from[1]);
        var input = spec.inputs.filter(function (i) { return i.type === type; })[0] || spec.inputs[0];
        var output = spec.outputs.filter(function (o) { return o.type === type; })[0] || spec.outputs[0];
        graph.links.splice(linkIndex, 1);
        connect(l.from, [n.id, input.id]);
        connect([n.id, output.id], l.to);
    }

    // ---- Add menu (Shift+A): categories that open their nodes, like Blender's -----------
    // Typing searches every node instead. Arrow keys move, Enter picks, Esc closes.
    var menuSub = document.getElementById('menu-sub');
    var menuAt = null, menuGrab = false, menuHi = null;
    function openMenu(at, screen, grab) {
        menuAt = at;
        menuGrab = grab;
        menuSearch.value = '';
        fillMenu();
        menu.hidden = false;
        var c = canvas.getBoundingClientRect();
        menu.style.left = Math.max(8, Math.min(screen.x, c.width - menu.offsetWidth - 8)) + 'px';
        menu.style.top = Math.max(8, Math.min(screen.y, c.height - menu.offsetHeight - 8)) + 'px';
        menuSearch.focus();
    }
    function closeMenu() {
        menu.hidden = true;
        menuSub.hidden = true;
        canvas.focus();
    }
    function addable() {
        var hasViewer = graph.nodes.some(function (n) { return n.type === 'viewer'; });
        return NODES.filter(function (n) { return !n.fixed && !(n.single && hasViewer); });
    }
    function menuItem(n, showGroup) {
        var b = make('button', 'ce-menu-item g-' + n.group.toLowerCase());
        b.type = 'button';
        b.dataset.type = n.id;
        b.title = n.hint || '';
        b.appendChild(make('span', '', n.label));
        if (showGroup) b.appendChild(make('small', '', n.group));
        return b;
    }
    function highlight(b) {
        if (menuHi) menuHi.classList.remove('hi');
        menuHi = b;
        if (b) { b.classList.add('hi'); b.scrollIntoView({ block: 'nearest' }); }
    }
    function fillMenu() {
        var q = menuSearch.value.trim().toLowerCase(), nodes = addable(), groups = [];
        menuList.innerHTML = '';
        menuSub.hidden = true;
        if (q) {
            nodes.forEach(function (n) {
                if ((n.label + ' ' + n.group).toLowerCase().indexOf(q) !== -1) menuList.appendChild(menuItem(n, true));
            });
            if (!menuList.firstChild) menuList.appendChild(make('p', 'ce-menu-none', 'No node matches'));
        } else {
            nodes.forEach(function (n) { if (groups.indexOf(n.group) === -1) groups.push(n.group); });
            groups.forEach(function (g) {
                var b = make('button', 'ce-menu-item ce-menu-cat g-' + g.toLowerCase());
                b.type = 'button';
                b.dataset.group = g;
                b.appendChild(make('span', '', g));
                b.appendChild(make('i', '', '›'));
                menuList.appendChild(b);
            });
        }
        highlight(menuList.querySelector('button'));
    }
    // The category's nodes, in a list next to the menu (left of it if there's no room).
    // On a narrow screen the list covers the menu instead, with a row to go back.
    function roomBeside() {
        var c = canvas.getBoundingClientRect(), m = menu.getBoundingClientRect(), w = 210;  // .ce-sub width
        return m.right + w < c.right - 8 || m.left - w > c.left + 8;
    }
    function openGroup(cat) {
        Array.prototype.forEach.call(menuList.querySelectorAll('.ce-menu-cat'), function (b) { b.classList.toggle('open', b === cat); });
        if (!menuSub.hidden && menuSub.dataset.group === cat.dataset.group) return;
        menuSub.dataset.group = cat.dataset.group;
        menuSub.innerHTML = '';
        addable().forEach(function (n) { if (n.group === cat.dataset.group) menuSub.appendChild(menuItem(n, false)); });
        menuSub.hidden = false;
        var c = canvas.getBoundingClientRect(), m = menu.getBoundingClientRect(), r = cat.getBoundingClientRect();
        var w = menuSub.offsetWidth, left = m.right - c.left - 2, top = r.top - c.top - 5;
        if (left + w > c.width - 8) left = m.left - c.left - w + 2;
        if (left < 8) {
            var back = make('button', 'ce-menu-item ce-menu-back', '‹ ' + cat.dataset.group);
            back.type = 'button';
            back.dataset.back = '1';
            menuSub.insertBefore(back, menuSub.firstChild);
            left = m.left - c.left;
            top = m.top - c.top;
        }
        menuSub.style.left = left + 'px';
        menuSub.style.top = Math.max(8, Math.min(top, c.height - menuSub.offsetHeight - 8)) + 'px';
    }
    function pick(b) {
        if (b.dataset.back) { menuSub.hidden = true; highlight(menuList.querySelector('.ce-menu-cat.open')); return; }
        if (b.dataset.group) { openGroup(b); highlight(menuSub.querySelector('[data-type]')); return; }
        closeMenu();
        addNode(b.dataset.type, menuAt, menuGrab);
    }
    menuSearch.addEventListener('input', fillMenu);
    menuSearch.addEventListener('keydown', function (e) {
        e.stopPropagation();
        var inSub = menuHi && menuSub.contains(menuHi);
        var list = Array.prototype.slice.call((inSub ? menuSub : menuList).querySelectorAll('button'));
        var i = list.indexOf(menuHi);
        if (e.key === 'Escape') closeMenu();
        else if (e.key === 'Enter') { e.preventDefault(); if (menuHi) pick(menuHi); }
        else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            highlight(list[(i + (e.key === 'ArrowDown' ? 1 : list.length - 1)) % list.length]);
            if (!inSub) menuSub.hidden = true;
        }
        else if (e.key === 'ArrowRight' && menuHi && menuHi.dataset.group) { e.preventDefault(); pick(menuHi); }
        else if (e.key === 'ArrowLeft' && inSub) {
            e.preventDefault();
            menuSub.hidden = true;
            highlight(menuList.querySelector('.ce-menu-cat.open'));
        }
    });
    [menuList, menuSub].forEach(function (box) {
        box.addEventListener('pointerover', function (e) {
            var b = e.target.closest('button');
            if (!b) return;
            highlight(b);
            if (b.dataset.group && roomBeside()) openGroup(b);  // otherwise it opens on a tap
        });
        box.addEventListener('click', function (e) {
            var b = e.target.closest('button');
            if (b) pick(b);
            if (!menu.hidden) menuSearch.focus();
        });
    });
    document.getElementById('add-btn').addEventListener('click', function (e) {
        var c = canvas.getBoundingClientRect(), at = viewCenter();
        openMenu(at, { x: c.width / 2 - 120, y: 16 }, false);
        e.stopPropagation();
    });

    // ---- Pointer: select, move, connect, pan -------------------------------------------
    canvas.addEventListener('pointerdown', function (e) {
        if (!menu.hidden && !menu.contains(e.target) && !menuSub.contains(e.target)) closeMenu();
        if (drag && drag.grab) { finishMove(); e.preventDefault(); return; }
        if (e.target.closest('.ce-menu, .ce-zoom, .ce-cover, select, input, label, .nf')) return;
        if (e.button !== 0 && e.button !== 1) return;
        var at = toWorld(e), sock = e.target.closest('.sock'), nodeEl = e.target.closest('.cn');
        canvas.focus({ preventScroll: true });
        if (sock) {
            var id = nodeEl.dataset.id;
            if (sock.dataset.out) {
                drag = { type: 'link', fromOut: true, from: [id, sock.dataset.out], start: sockPos(id, 'out', sock.dataset.out) };
            } else {
                var existing = linkInto(id, sock.dataset.in);
                if (existing) {  // pick the wire up from its input, like Blender
                    before();
                    graph.links.splice(graph.links.indexOf(existing), 1);
                    refreshNode(nodeById(id));
                    drag = { type: 'link', fromOut: true, from: existing.from, start: sockPos(existing.from[0], 'out', existing.from[1]), picked: true };
                } else {
                    drag = { type: 'link', fromOut: false, to: [id, sock.dataset.in], start: sockPos(id, 'in', sock.dataset.in) };
                }
            }
            drag.end = at;
        } else if (nodeEl && e.button === 0) {
            var nid = nodeEl.dataset.id;
            if (e.ctrlKey && e.shiftKey) { viewNode(nodeById(nid)); return; }
            if (e.shiftKey) selected[nid] = !selected[nid];
            else if (!selected[nid]) { selected = {}; selected[nid] = true; }
            var ids = Object.keys(selected).filter(function (k) { return selected[k]; });
            drag = { type: 'move', ids: ids, start: at, orig: {}, moved: false };
            ids.forEach(function (k) { var n = nodeById(k); drag.orig[k] = { x: n.x, y: n.y }; });
            graph.nodes.forEach(refreshNode);
        } else {
            drag = { type: 'pan', sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
        }
        canvas.setPointerCapture(e.pointerId);
        e.preventDefault();
    });

    canvas.addEventListener('pointermove', function (e) {
        pointer = toWorld(e);
        if (!drag) return;
        if (drag.type === 'pan') {
            if (Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) > 3) drag.moved = true;
            view.x = drag.vx + e.clientX - drag.sx;
            view.y = drag.vy + e.clientY - drag.sy;
            applyView();
        } else if (drag.type === 'move') {
            var dx = pointer.x - drag.start.x, dy = pointer.y - drag.start.y;
            if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3 / view.zoom) return;
            if (!drag.moved) before();
            drag.moved = true;
            drag.ids.forEach(function (id) {
                var n = nodeById(id);
                n.x = Math.round(drag.orig[id].x + dx);
                n.y = Math.round(drag.orig[id].y + dy);
                refreshNode(n);
            });
            drawWires();
            if (drag.ids.length === 1) markWire(wireUnder(nodeById(drag.ids[0])));
        } else if (drag.type === 'link') {
            drag.end = pointer;
            drawWires();
        }
    });

    function finishMove() {
        var d = drag;
        drag = null;
        canvas.classList.remove('grabbing');
        if (d.moved && d.ids.length === 1) {
            var hot = wireUnder(nodeById(d.ids[0]));
            if (hot !== -1) insertOnWire(nodeById(d.ids[0]), hot);
        }
        graph.nodes.forEach(refreshNode);
        drawWires();
        if (d.moved) changed(true);
    }

    canvas.addEventListener('pointerup', function (e) {
        if (!drag || drag.grab) return;
        var d = drag;
        if (d.type === 'move') { finishMove(); return; }
        drag = null;
        if (d.type === 'pan') {
            if (!d.moved && Object.keys(selected).length) { selected = {}; graph.nodes.forEach(refreshNode); }
            return;
        }
        // Connecting: find the socket (or node) under the pointer.
        var hit = document.elementFromPoint(e.clientX, e.clientY);
        var sock = hit && hit.closest('.sock'), target = hit && hit.closest('.cn'), done = false;
        if (target) {
            var tid = target.dataset.id, spec = SPEC[nodeById(tid).type];
            if (d.fromOut) {
                var input = sock && sock.dataset.in ? sock.dataset.in :
                    ((spec.inputs.filter(function (i) { return !linkInto(tid, i.id); })[0] || spec.inputs[0]) || {}).id;
                if (input) { if (!d.picked) before(); done = connect(d.from, [tid, input]); }
            } else {
                var output = sock && sock.dataset.out ? sock.dataset.out : (spec.outputs[0] || {}).id;
                if (output) { before(); done = connect([tid, output], d.to); }
            }
        }
        graph.nodes.forEach(refreshNode);
        drawWires();
        if (done || d.picked) changed(true);
    });

    canvas.addEventListener('wheel', function (e) {
        if (e.target.closest('.ce-menu')) return;
        e.preventDefault();
        var c = canvas.getBoundingClientRect();
        if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
            zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX - c.left, e.clientY - c.top);
        } else {
            view.x -= e.deltaX;
            applyView();
        }
        drawWires();
    }, { passive: false });

    Array.prototype.forEach.call(document.querySelectorAll('[data-zoom]'), function (b) {
        b.addEventListener('click', function () {
            var c = canvas.getBoundingClientRect();
            zoomAt(Number(b.dataset.zoom), c.width / 2, c.height / 2);
            drawWires();
        });
    });
    document.getElementById('fit').addEventListener('click', function () { frameAll(); drawWires(); });
    document.getElementById('undo').addEventListener('click', function () { restore(undoStack, redoStack); });
    document.getElementById('redo').addEventListener('click', function () { restore(redoStack, undoStack); });

    document.addEventListener('keydown', function (e) {
        if (e.target.closest('input, select, textarea') || !graph) return;
        var key = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
        if (mod && key === 'z') { e.preventDefault(); restore(e.shiftKey ? redoStack : undoStack, e.shiftKey ? undoStack : redoStack); }
        else if (mod && key === 'y') { e.preventDefault(); restore(redoStack, undoStack); }
        else if (mod) return;
        else if (e.shiftKey && key === 'a') {
            e.preventDefault();
            openMenu({ x: pointer.x - NODE_W / 2, y: pointer.y - 14 },
                     { x: pointer.x * view.zoom + view.x, y: pointer.y * view.zoom + view.y }, true);
        }
        else if (e.shiftKey && key === 'd') { e.preventDefault(); duplicateSelected(); }
        else if (key === 'x' || key === 'delete' || key === 'backspace') { e.preventDefault(); deleteSelected(); }
        else if (key === 'm') muteSelected();
        else if (key === 'home') { frameAll(); drawWires(); }
        else if (key === 'escape') {
            if (drag && drag.grab) {  // cancel a grab right after adding: remove the new node
                var id = drag.ids[0];
                drag = null;
                canvas.classList.remove('grabbing');
                selected = {};
                selected[id] = true;
                deleteSelected();
                undoStack.pop();
                undoStack.pop();
            } else if (!menu.hidden) closeMenu();
        }
    });

    // ---- Live preview ------------------------------------------------------------------
    // Previews already made (same nodes, settings, frame and size) are kept, so going back
    // to one (undo, another frame, another size) shows it at once without asking Blender.
    var previewTimer = null, previewBusy = false, wanted = '', clock = null;
    var made = new Map(), madeBytes = 0;
    var SCALE_KEY = 'comp-preview-scale';
    try {
        var savedScale = localStorage.getItem(SCALE_KEY);
        if (savedScale && sizeSelect.querySelector('option[value="' + savedScale + '"]')) sizeSelect.value = savedScale;
    } catch (e) { /* private mode */ }

    function secs(s) { return s < 60 ? s.toFixed(s < 10 ? 2 : 1) + ' s' : clockText(s); }
    function clockText(s) {
        s = Math.max(0, Math.round(s));
        var m = Math.floor(s / 60), h = Math.floor(m / 60);
        return (h ? h + ':' + ('0' + m % 60).slice(-2) : m) + ':' + ('0' + s % 60).slice(-2);
    }
    function schedulePreview() {
        clearTimeout(previewTimer);
        previewTimer = setTimeout(runPreview, 40);
    }
    function previewKey() {  // node positions don't change the picture
        return canon({
            nodes: graph.nodes.map(function (n) { return [n.id, n.type, !!n.mute, n.values]; }),
            links: graph.links, frame: Number(frameInput.value), scale: sizeSelect.value
        });
    }
    function remember(key, entry) {
        made.set(key, entry);
        madeBytes += entry.bytes;
        while (made.size > 1 && (made.size > 60 || madeBytes > 300e6)) {  // full-size previews of big renders are large
            var oldKey = made.keys().next().value, old = made.get(oldKey);
            made.delete(oldKey);
            madeBytes -= old.bytes;
            if (old.url) URL.revokeObjectURL(old.url);
        }
    }
    function showPreview(entry, note) {
        pimg.hidden = !entry.url;
        pmsg.hidden = !!entry.url;
        if (!entry.url) {
            pmsg.textContent = 'Nothing is plugged into the ' + (entry.viewer ? 'Viewer' : 'Composite') + ' node.';
            pstat.textContent = note;
            return;
        }
        pimg.onload = function () { pstat.textContent = note + ' · ' + pimg.naturalWidth + ' × ' + pimg.naturalHeight + ' px'; };
        if (pimg.src === entry.url) pimg.onload();
        else pimg.src = entry.url;
    }
    function runPreview() {
        if (!job || !job.raw_frames) return;
        var key = previewKey();
        var hasViewer = graph.links.some(function (l) { var n = nodeById(l.to[0]); return n && n.type === 'viewer'; });
        document.getElementById('viewer-badge').hidden = !hasViewer;
        wanted = key;
        var known = made.get(key);
        if (known) {
            made.delete(key);  // now the most recently used
            made.set(key, known);
            showPreview(known, 'Shown again without redoing it (it took ' + secs(known.secs) + ')');
            return;
        }
        if (previewBusy) return;  // runs again for `wanted` when the current one is back
        previewBusy = true;
        var started = Date.now();
        clearInterval(clock);
        pstat.textContent = 'Updating… 0.00 s';
        clock = setInterval(function () {
            if (wanted === key) pstat.textContent = 'Updating… ' + secs((Date.now() - started) / 1000);
        }, 100);
        fetch('/api/jobs/' + JOB_ID + '/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ graph: graph, frame: Number(frameInput.value), scale: Number(sizeSelect.value) })
        }).then(function (r) {
            if (r.status === 204) return null;
            if (!r.ok) return r.json().catch(function () { return {}; }).then(function (d) { throw new Error(d.error || ('HTTP ' + r.status)); });
            return r.blob();
        }).then(function (blob) {
            var entry = { url: blob ? URL.createObjectURL(blob) : null, bytes: blob ? blob.size : 0,
                          viewer: hasViewer, secs: (Date.now() - started) / 1000 };
            remember(key, entry);
            if (wanted === key) showPreview(entry, 'Updated in ' + secs(entry.secs));
        }).catch(function (err) {
            if (wanted !== key) return;
            pstat.textContent = '';
            pmsg.hidden = false;
            pimg.hidden = true;
            pmsg.textContent = 'Preview failed: ' + err.message;
        }).then(function () {
            previewBusy = false;
            clearInterval(clock);
            if (wanted !== key) runPreview();
        });
    }
    frameInput.addEventListener('input', function () {
        document.getElementById('frame-num').textContent = Number(frameInput.value) + 1 + ' / ' + job.raw_frames;
        schedulePreview();
    });
    sizeSelect.addEventListener('change', function () {
        try { localStorage.setItem(SCALE_KEY, sizeSelect.value); } catch (e) { /* private mode */ }
        schedulePreview();
    });

    // ---- Apply to the render -------------------------------------------------------------
    applyBtn.addEventListener('click', function () {
        showMsg('');
        applyBtn.disabled = true;
        api('POST', '/api/jobs/' + JOB_ID + '/composite', { graph: graph }).then(function (d) {
            job = d.job;
            updateApply();
            watchJob();
        }).catch(function (err) {
            showMsg(err.message, true);
            updateApply();
        });
    });

    // How long Apply has been running: the server's count, plus the time since we asked.
    var watchTimer = null, applyClock = null, seenAt = 0;
    function applyElapsed() {
        var live = job.status === 'compositing' || job.status === 'encoding';
        return (job.comp_elapsed || 0) + (live ? (Date.now() - seenAt) / 1000 : 0);
    }
    function applyProgress() {
        var t = applyElapsed(), done = job.frames_done, total = job.frames_total;
        if (job.status === 'encoding') return 'Making the MP4… ' + clockText(t);
        var text = 'Applying: ' + (total > 1 ? done + ' of ' + total + ' frames · ' : '') + clockText(t);
        if (done > 0 && done < total) text += ' · about ' + clockText(t / done * (total - done)) + ' left';
        return text;
    }
    function watchJob() {
        clearTimeout(watchTimer);
        api('GET', '/api/jobs/' + JOB_ID).then(function (d) {
            var first = !job || !job.raw_frames;
            var wasBusy = job && ['queued', 'compositing', 'encoding'].indexOf(job.status) !== -1;
            job = d.job;
            seenAt = Date.now();
            setupFrames();
            var busy = ['preparing', 'queued', 'running', 'compositing', 'encoding'].indexOf(job.status) !== -1;
            if (job.opts.compositing && job.opts.compositing.graph) applied = canon(job.opts.compositing.graph);
            clearInterval(applyClock);
            if (job.status === 'compositing' || job.status === 'encoding') {
                showMsg(applyProgress());
                applyClock = setInterval(function () { showMsg(applyProgress()); updateApply(); }, 250);
            } else if (wasBusy && job.status === 'done') {
                showMsg('Applied in ' + clockText(job.comp_elapsed) + '. The render\'s images' + (job.video ? ' and MP4 are' : ' are') + ' updated.');
            } else if (job.status === 'failed' && wasBusy) {
                showMsg(job.error || 'Applying failed', true);
            }
            if (first && job.raw_frames) start();
            cover();
            updateApply();
            if (busy) watchTimer = setTimeout(watchJob, job.status === 'running' ? 3000 : 1000);
        }).catch(function (err) { showMsg(err.message, true); });
    }

    function setupFrames() {
        var n = job.raw_frames || 0;
        document.getElementById('frame-box').hidden = n < 2;
        frameInput.max = Math.max(0, n - 1);
        document.getElementById('frame-num').textContent = Number(frameInput.value) + 1 + ' / ' + n;
    }

    function cover() {
        var box = document.getElementById('cover'), text = document.getElementById('cover-text');
        if (job.raw_frames) { box.hidden = true; return; }
        box.hidden = false;
        var custom = job.opts.compositing && job.opts.compositing.mode === 'custom';
        if (!custom) text.textContent = 'This render was made without Custom nodes compositing, so there are no raw frames to work on. Render the file again with Compositing → Custom nodes.';
        else if (job.status === 'failed' || job.status === 'cancelled') text.textContent = 'This render ' + job.status + ' before any frame was done.';
        else text.textContent = 'Rendering… the compositor opens as soon as the first frame is done.';
        pmsg.textContent = '';
    }

    // ---- Start ---------------------------------------------------------------------------
    function start() {
        var saved = null;
        try { saved = JSON.parse(localStorage.getItem(DRAFT_KEY)); } catch (e) { saved = null; }
        var server = job.opts.compositing && job.opts.compositing.graph;
        graph = copy(saved && saved.nodes ? saved : (server || DEFAULT_GRAPH));
        graph.nodes = graph.nodes.filter(function (n) { return SPEC[n.type]; });
        graph.nodes.forEach(function (n) {  // values added to the node list later get their defaults
            n.values = n.values || {};
            valuesOf(SPEC[n.type]).forEach(function (p) { if (!(p.id in n.values)) n.values[p.id] = copy(p.default); });
        });
        drawAll();
        frameAll();
        drawWires();
        changed(true);
    }

    window.addEventListener('resize', drawWires);
    applyView();
    watchJob();
})();
