// Blender Render front end: chunked upload, job list with live progress.
(function () {
    'use strict';

    var CHUNK = 32 * 1024 * 1024; // stays well under the tunnel's 100 MB request cap
    var GALLERY_MAX = 24;

    var form = document.getElementById('form');
    var fileInput = document.getElementById('file');
    var drop = document.getElementById('drop');
    var dropMain = document.getElementById('drop-main');
    var dropSub = document.getElementById('drop-sub');
    var goBtn = document.getElementById('go');
    var upBox = document.getElementById('up'), upFill = document.getElementById('up-fill'), upText = document.getElementById('up-text');
    var msg = document.getElementById('msg');
    var jobsEl = document.getElementById('jobs');
    var tpl = document.getElementById('job-tpl');
    var chosen = null, uploading = false;

    // ---- Helpers -----------------------------------------------------------------
    function size(n) {
        var u = ['B', 'KB', 'MB', 'GB'], i = 0;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return (i ? n.toFixed(1) : n) + ' ' + u[i];
    }
    function clock(sec) {
        sec = Math.max(0, Math.round(sec));
        var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
        return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }
    function showMsg(text, isErr) {
        msg.textContent = text || '';
        msg.hidden = !text;
        msg.classList.toggle('err', !!isErr);
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

    // ---- System info -------------------------------------------------------------
    function loadSystem() {
        api('GET', '/api/system').then(function (s) {
            var parts = [];
            if (s.gpu) parts.push('<b>' + esc(s.gpu.name) + '</b> · ' + (s.gpu.mem_used / 1024).toFixed(1) + '/' + (s.gpu.mem_total / 1024).toFixed(0) + ' GB · ' + s.gpu.util + '%');
            else parts.push('<span class="warn">No GPU - set Colab runtime to GPU</span>');
            parts.push(esc(s.blender) + ' · ' + size(s.disk_free) + ' free');
            document.getElementById('sys').innerHTML = parts.join('<br>');
        }).catch(function () {});
    }
    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; });
    }

    // ---- Form --------------------------------------------------------------------
    function setFile(f) {
        if (!f) return;
        if (!/\.(blend|zip)$/i.test(f.name)) {
            showMsg('Choose a .blend file, or a .zip that contains one.', true);
            return;
        }
        chosen = f;
        drop.classList.add('has');
        dropMain.textContent = f.name;
        dropSub.textContent = size(f.size) + ' · click to choose another file';
        goBtn.disabled = uploading;
        showMsg('');
    }
    fileInput.addEventListener('change', function () { setFile(fileInput.files[0]); });
    ['dragenter', 'dragover'].forEach(function (ev) {
        drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
        drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
    });
    drop.addEventListener('drop', function (e) { setFile(e.dataTransfer.files[0]); });

    // ---- Compositing node editor ---------------------------------------------------
    // A stack of real Blender compositor nodes between the render and the output.
    // The node list comes from compositing.py; the value is [{type, mute, params}].
    var COMP_NODES = JSON.parse(document.getElementById('comp-nodes').textContent);
    var NODE_BY_ID = {};
    COMP_NODES.forEach(function (n) { NODE_BY_ID[n.id] = n; });

    function make(tag, cls, text) {
        var el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text !== undefined) el.textContent = text;
        return el;
    }
    function toHex(rgb) {
        return '#' + rgb.map(function (v) { return ('0' + Math.round(v * 255).toString(16)).slice(-2); }).join('');
    }
    function fromHex(hex) {
        return [1, 3, 5].map(function (i) { return parseInt(hex.substr(i, 2), 16) / 255; });
    }
    function copyNodes(nodes) { return JSON.parse(JSON.stringify(nodes || [])); }
    function newNode(id) {
        var params = {};
        NODE_BY_ID[id].params.forEach(function (p) { params[p.id] = p.default; });
        return copyNodes([{ type: id, mute: false, params: params }])[0];
    }

    function paramField(p, item, onChange) {
        var field = make('label', p.kind === 'bool' ? 'check cparam' : 'cparam');
        var value = item.params[p.id];
        var set = function (v) { item.params[p.id] = v; onChange(); };
        if (p.kind === 'bool') {
            var cb = make('input');
            cb.type = 'checkbox';
            cb.checked = value;
            cb.addEventListener('change', function () { set(cb.checked); });
            field.appendChild(cb);
            field.appendChild(document.createTextNode(' ' + p.label));
            return field;
        }
        field.appendChild(make('span', 'lbl', p.kind === 'angle' ? p.label + ' (°)' : p.label));
        if (p.kind === 'choice') {
            var sel = make('select');
            p.choices.forEach(function (c) {
                var o = make('option', '', c[1]);
                o.value = c[0];
                sel.appendChild(o);
            });
            sel.value = value;
            sel.addEventListener('change', function () { set(sel.value); });
            field.appendChild(sel);
        } else if (p.kind === 'color') {
            var col = make('input');
            col.type = 'color';
            col.value = toHex(value);
            col.addEventListener('input', function () { set(fromHex(col.value)); });
            field.appendChild(col);
        } else {
            var row = make('div', 'cparam-num'), range = make('input'), box = make('input');
            range.type = 'range';
            box.type = 'number';
            [range, box].forEach(function (inp) {
                inp.min = p.min;
                inp.max = p.max;
                inp.step = p.kind === 'float' ? p.step : 1;
                inp.value = value;
            });
            range.addEventListener('input', function () { box.value = range.value; set(Number(range.value)); });
            box.addEventListener('input', function () {
                if (box.value === '' || isNaN(box.value)) return;
                range.value = box.value;
                set(Number(box.value));
            });
            row.appendChild(range);
            row.appendChild(box);
            field.appendChild(row);
        }
        return field;
    }

    function CompEditor(root, nodes) {
        var list = copyNodes(nodes).filter(function (n) { return NODE_BY_ID[n.type]; });
        var flow = make('div', 'comp-flow');
        var add = make('select', 'comp-add'), groups = {};
        add.appendChild(make('option', '', 'Add node…')).value = '';
        COMP_NODES.forEach(function (n) {
            if (!groups[n.group]) groups[n.group] = add.appendChild(make('optgroup'));
            groups[n.group].label = n.group;
            groups[n.group].appendChild(make('option', '', n.label)).value = n.id;
        });
        add.addEventListener('change', function () {
            if (!add.value) return;
            list.push(newNode(add.value));
            add.value = '';
            draw();
        });
        root.innerHTML = '';
        root.appendChild(flow);
        root.appendChild(add);

        function card(item, i) {
            var spec = NODE_BY_ID[item.type];
            var el = make('div', 'cnode g-' + spec.group.split(' ')[0].toLowerCase() + (item.mute ? ' muted' : ''));
            var head = make('div', 'cnode-head');
            var on = make('input');
            on.type = 'checkbox';
            on.checked = !item.mute;
            on.title = 'Turn this node on or off';
            on.addEventListener('change', function () { item.mute = !on.checked; el.classList.toggle('muted', item.mute); });
            head.appendChild(on);
            head.appendChild(make('b', '', spec.label));
            [['up', '↑', 'Move up'], ['down', '↓', 'Move down'], ['del', '✕', 'Remove']].forEach(function (b) {
                var btn = make('button', 'cnode-btn', b[1]);
                btn.type = 'button';
                btn.title = b[2];
                btn.disabled = (b[0] === 'up' && i === 0) || (b[0] === 'down' && i === list.length - 1);
                btn.addEventListener('click', function () {
                    list.splice(i, 1);
                    if (b[0] !== 'del') list.splice(b[0] === 'up' ? i - 1 : i + 1, 0, item);
                    draw();
                });
                head.appendChild(btn);
            });
            el.appendChild(head);
            if (spec.hint) el.appendChild(make('p', 'cnode-hint', spec.hint));
            var params = make('div', 'cnode-params'), fields = [];
            function showRelevant() {  // e.g. Glare's Size only for Bloom and Fog Glow
                fields.forEach(function (f) {
                    var when = f.p.when || {};
                    f.el.hidden = Object.keys(when).some(function (k) { return when[k].indexOf(item.params[k]) === -1; });
                });
            }
            spec.params.forEach(function (p) {
                fields.push({ p: p, el: params.appendChild(paramField(p, item, showRelevant)) });
            });
            showRelevant();
            el.appendChild(params);
            return el;
        }

        function draw() {
            flow.innerHTML = '';
            flow.appendChild(make('div', 'comp-end', 'Render result'));
            list.forEach(function (item, i) { flow.appendChild(card(item, i)); });
            if (!list.length) flow.appendChild(make('p', 'comp-empty', 'No nodes yet: the render is saved as it is. Add one below.'));
            flow.appendChild(make('div', 'comp-end', 'Output'));
        }
        draw();
        return { get: function () { return copyNodes(list); } };
    }

    var formComp = CompEditor(document.getElementById('comp'), []);

    // Show only the fields for the chosen render mode, engine and compositing.
    function syncForm() {
        var mode = form.mode.value, engine = form.engine.value, comp = form.comp_mode.value;
        Array.prototype.forEach.call(form.querySelectorAll('[data-show]'), function (el) {
            el.hidden = el.dataset.show !== mode;
        });
        Array.prototype.forEach.call(form.querySelectorAll('[data-engine]'), function (el) {
            el.hidden = el.dataset.engine.split(' ').indexOf(engine) === -1;
        });
        Array.prototype.forEach.call(form.querySelectorAll('[data-comp]'), function (el) {
            el.hidden = el.dataset.comp !== comp;
        });
    }
    Array.prototype.forEach.call(form.querySelectorAll('input[name=mode], input[name=engine], input[name=comp_mode]'), function (r) {
        r.addEventListener('change', syncForm);
    });
    syncForm();

    // Every visible setting; empty boxes and "From file" are left out so the .blend's value is kept.
    function options() {
        var o = {};
        Array.prototype.forEach.call(form.querySelectorAll('input[name], select[name]'), function (el) {
            if (el.type === 'file' || el.closest('[hidden]')) return;
            if (el.type === 'radio') { if (el.checked) o[el.name] = el.value; return; }
            if (el.type === 'checkbox') { o[el.name] = el.checked; return; }
            if (el.value !== '' && el.value !== 'file') o[el.name] = el.value;
        });
        o.compositing = { mode: o.comp_mode, nodes: o.comp_mode === 'custom' ? formComp.get() : [] };
        delete o.comp_mode;
        return o;
    }

    // ---- Chunked upload ----------------------------------------------------------
    function uploadFile(file, onProgress) {
        var id = '';
        var bytes = crypto.getRandomValues(new Uint8Array(12));
        for (var i = 0; i < bytes.length; i++) id += ('0' + bytes[i].toString(16)).slice(-2);

        function send(offset, tries) {
            if (offset >= file.size) return Promise.resolve(id);
            var blob = file.slice(offset, Math.min(file.size, offset + CHUNK));
            return new Promise(function (resolve, reject) {
                var xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/upload');
                xhr.setRequestHeader('X-Upload-Id', id);
                xhr.setRequestHeader('X-Offset', String(offset));
                xhr.setRequestHeader('Content-Type', 'application/octet-stream');
                xhr.upload.onprogress = function (e) { onProgress(offset + e.loaded, file.size); };
                xhr.onload = function () {
                    var d = {};
                    try { d = JSON.parse(xhr.responseText); } catch (e) { /* not json */ }
                    if (xhr.status === 200) resolve(d.size);
                    else if (xhr.status === 409 && typeof d.size === 'number') resolve(d.size);
                    else if (xhr.status === 401) { location.href = '/login'; reject(new Error('Not logged in')); }
                    else reject(new Error(d.error || ('Upload failed (HTTP ' + xhr.status + ')')));
                };
                xhr.onerror = function () { reject(new Error('Network error while uploading')); };
                xhr.send(blob);
            }).then(function (next) {
                return send(next, 0);
            }, function (err) {
                if (tries >= 4 || /Not logged in/.test(err.message)) throw err;
                return new Promise(function (r) { setTimeout(r, 1500 * (tries + 1)); }).then(function () {
                    return send(offset, tries + 1);
                });
            });
        }
        return send(0, 0);
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!chosen || uploading) return;
        uploading = true;
        goBtn.disabled = true;
        upBox.hidden = false;
        showMsg('');
        var started = Date.now(), file = chosen;
        uploadFile(file, function (done, total) {
            var pct = total ? done / total * 100 : 100;
            var secs = (Date.now() - started) / 1000;
            var speed = secs > 0.5 ? ' · ' + size(done / secs) + '/s' : '';
            upFill.style.width = pct.toFixed(1) + '%';
            upText.textContent = 'Uploading ' + pct.toFixed(0) + '%' + speed;
        }).then(function (uploadId) {
            upText.textContent = 'Starting render…';
            return api('POST', '/api/jobs', { upload_id: uploadId, filename: file.name, options: options() });
        }).then(function () {
            upText.textContent = 'Queued';
            fileInput.value = '';
            chosen = null;
            drop.classList.remove('has');
            dropMain.innerHTML = 'Drop a <b>.blend</b> file here, or click to choose';
            dropSub.textContent = 'Using textures that aren’t packed? Zip the .blend together with its texture folders and upload the .zip.';
            poll(true);
        }).catch(function (err) {
            showMsg(err.message, true);
        }).then(function () {
            uploading = false;
            goBtn.disabled = !chosen;
            setTimeout(function () { if (!uploading) upBox.hidden = true; upFill.style.width = '0'; }, 1200);
        });
    });

    // ---- Job list ----------------------------------------------------------------
    var cards = {};
    var STATUS = { queued: 'Queued', running: 'Rendering', compositing: 'Compositing', encoding: 'Making MP4', done: 'Done', failed: 'Failed', cancelled: 'Cancelled' };
    var BUSY = ['queued', 'running', 'compositing', 'encoding'];

    // v changes each time the frames are composited again, so the browser doesn't show old copies.
    function fileUrl(job, name, dl) {
        return '/api/jobs/' + job.id + '/files/' + encodeURIComponent(name) + '?v=' + job.version + (dl ? '&dl=1' : '');
    }

    function makeCard(job) {
        var el = tpl.content.firstElementChild.cloneNode(true);
        el.dataset.id = job.id;
        el._outputs = -1;
        el._showAll = false;
        el.addEventListener('click', function (e) {
            var b = e.target.closest('[data-act]');
            if (!b || b.tagName === 'A') return;
            var act = b.dataset.act;
            if (act === 'cancel') api('POST', '/api/jobs/' + job.id + '/cancel').then(function () { poll(true); });
            if (act === 'delete') {
                if (!confirm('Delete this render and its files?')) return;
                api('DELETE', '/api/jobs/' + job.id).then(function () { poll(true); });
            }
            if (act === 'log') {
                var log = el.querySelector('.log');
                log.hidden = !log.hidden;
                b.textContent = log.hidden ? 'Show log' : 'Hide log';
                if (!log.hidden) loadLog(job.id, el);
            }
            if (act === 'all') { el._showAll = true; renderGallery(el, el._job); }
            if (act === 'comp') {
                var panel = el.querySelector('.job-comp');
                panel.hidden = !panel.hidden;
                b.textContent = panel.hidden ? 'Edit compositing' : 'Close compositing';
                if (!panel.hidden && !el._comp) el._comp = CompEditor(panel.querySelector('.comp'), el._job.opts.compositing.nodes);
            }
            if (act === 'comp-apply') {
                var err = el.querySelector('.job-comp .msg');
                err.hidden = true;
                b.disabled = true;
                api('POST', '/api/jobs/' + job.id + '/composite', { nodes: el._comp.get() }).then(function () {
                    poll(true);
                }).catch(function (e) {
                    err.textContent = e.message;
                    err.hidden = false;
                    b.disabled = false;
                });
            }
        });
        return el;
    }

    function loadLog(id, el) {
        api('GET', '/api/jobs/' + id).then(function (d) {
            var log = el.querySelector('.log');
            var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
            log.textContent = (d.job.log || []).join('\n');
            if (atBottom) log.scrollTop = log.scrollHeight;
        }).catch(function () {});
    }

    function renderGallery(el, job) {
        var g = el.querySelector('.gallery');
        var outs = job.outputs.slice().reverse(); // newest first
        var shown = el._showAll ? outs : outs.slice(0, GALLERY_MAX);
        g.classList.toggle('single', outs.length === 1);
        g.innerHTML = '';
        shown.forEach(function (name) {
            var a = document.createElement('a');
            a.className = 'shot';
            a.href = fileUrl(job, name);
            a.target = '_blank';
            a.rel = 'noopener';
            var label = document.createElement('span');
            label.textContent = name;
            if (/\.exr$/i.test(name)) {
                a.classList.add('noimg');
                a.href = fileUrl(job, name, true);
                a.removeAttribute('target');
            } else {
                var img = document.createElement('img');
                img.loading = 'lazy';
                img.alt = name;
                img.src = fileUrl(job, name);
                a.appendChild(img);
            }
            a.appendChild(label);
            g.appendChild(a);
        });
        if (outs.length > shown.length) {
            var more = document.createElement('button');
            more.type = 'button';
            more.className = 'btn sm ghost more';
            more.dataset.act = 'all';
            more.textContent = 'Show all ' + outs.length + ' frames';
            g.appendChild(more);
        }
    }

    function updateCard(el, job) {
        el._job = job;
        el.className = 'card job ' + job.status;
        el.querySelector('.job-name').textContent = job.name;
        var o = job.opts, meta = [];
        meta.push(o.mode === 'animation' ? 'Animation' : 'Still');
        if (job.info.engine) meta.push(job.info.engine.replace('BLENDER_', '').replace('_NEXT', '').toLowerCase());
        if (job.info.width) meta.push(job.info.width + '×' + job.info.height);
        if (o.samples) meta.push(o.samples + ' samples');
        if (o.noise_threshold !== null && o.noise_threshold !== undefined) meta.push(o.noise_threshold ? 'noise ' + o.noise_threshold : 'adaptive off');
        if (o.eevee_samples) meta.push(o.eevee_samples + ' samples');
        var comp = o.compositing || {};
        if (comp.mode === 'custom') meta.push(comp.nodes.length + ' compositing node' + (comp.nodes.length === 1 ? '' : 's'));
        if (comp.mode === 'off') meta.push('no compositing');
        if (job.info.blender) meta.push('Blender ' + job.info.blender);
        el.querySelector('.job-meta').textContent = meta.join(' · ');

        var badge = el.querySelector('.badge');
        badge.className = 'badge ' + job.status;
        badge.textContent = STATUS[job.status] || job.status;
        el.querySelector('.bar-fill').style.width = (job.progress * 100).toFixed(1) + '%';

        var stats = [];
        if (job.frames_total) stats.push('Frames <b>' + job.frames_done + '/' + job.frames_total + '</b>');
        if (job.status === 'running' && job.frame !== null) stats.push('Frame <b>' + job.frame + '</b>');
        if (job.status === 'running' && job.samples[1]) stats.push('Sample <b>' + job.samples[0] + '/' + job.samples[1] + '</b>');
        if (job.elapsed) stats.push('Time <b>' + clock(job.elapsed) + '</b>');
        if (job.status === 'running' && job.frames_done && job.frames_total > 1) {
            var left = job.elapsed / job.frames_done * (job.frames_total - job.frames_done);
            stats.push('About <b>' + clock(left) + '</b> left');
        } else if (job.status === 'running' && job.remaining) {
            stats.push('Frame done in <b>' + esc(job.remaining) + '</b>');
        }
        el.querySelector('.job-stats').innerHTML = stats.join('');

        var errEl = el.querySelector('.job-error');
        errEl.textContent = job.error;
        errEl.hidden = !job.error;

        var zip = el.querySelector('[data-act="zip"]');
        var busy = BUSY.indexOf(job.status) !== -1;
        zip.hidden = !job.outputs.length || busy;
        zip.href = '/api/jobs/' + job.id + '/zip';
        var vidLink = el.querySelector('[data-act="video"]'), vid = el.querySelector('.job-video');
        vidLink.hidden = vid.hidden = !job.video;
        if (job.video && vid.getAttribute('src') !== fileUrl(job, job.video)) {
            vid.src = fileUrl(job, job.video);
            vidLink.href = fileUrl(job, job.video, true);
        }
        el.querySelector('[data-act="cancel"]').hidden = !busy;
        el.querySelector('[data-act="comp"]').hidden = !job.raw_frames;
        el.querySelector('[data-act="comp-apply"]').disabled = busy;

        if (el._outputs !== job.outputs.length || el._version !== job.version) {
            el._outputs = job.outputs.length;
            el._version = job.version;
            renderGallery(el, job);
        }
        var log = el.querySelector('.log');
        if (!log.hidden && (busy || !log.textContent)) loadLog(job.id, el);
    }

    var pollTimer = null, busy = false;
    function poll(now) {
        clearTimeout(pollTimer);
        if (busy && !now) return;
        busy = true;
        api('GET', '/api/jobs').then(function (d) {
            var seen = {};
            if (!d.jobs.length) jobsEl.innerHTML = '<p class="empty">Nothing rendered yet. Upload a file above.</p>';
            else {
                var empty = jobsEl.querySelector('.empty');
                if (empty) empty.remove();
            }
            d.jobs.forEach(function (job, i) {
                seen[job.id] = true;
                var el = cards[job.id] || (cards[job.id] = makeCard(job));
                updateCard(el, job);
                if (jobsEl.children[i] !== el) jobsEl.insertBefore(el, jobsEl.children[i] || null);
            });
            Object.keys(cards).forEach(function (id) {
                if (!seen[id]) { cards[id].remove(); delete cards[id]; }
            });
            var active = d.jobs.some(function (j) { return BUSY.indexOf(j.status) !== -1; });
            pollTimer = setTimeout(poll, active ? 2000 : 8000);
            if (active) loadSystem();
        }).catch(function () {
            pollTimer = setTimeout(poll, 5000);
        }).then(function () { busy = false; });
    }

    loadSystem();
    poll(true);
})();
