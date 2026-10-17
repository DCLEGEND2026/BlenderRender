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

    function syncMode() {
        var mode = form.mode.value;
        Array.prototype.forEach.call(form.querySelectorAll('[data-show]'), function (el) {
            el.hidden = el.dataset.show !== mode;
        });
    }
    Array.prototype.forEach.call(form.mode, function (r) { r.addEventListener('change', syncMode); });
    form.engine.addEventListener('change', function () {
        document.getElementById('eevee-hint').hidden = form.engine.value !== 'eevee';
        form.device.disabled = form.engine.value === 'eevee' || form.engine.value === 'workbench';
    });
    syncMode();

    function options() {
        var o = {};
        ['mode', 'engine', 'device', 'format', 'frame', 'frame_start', 'frame_end', 'samples', 'resolution_pct'].forEach(function (k) {
            var v = form[k].value;
            if (v !== '') o[k] = v;
        });
        o.video = form.video.checked;
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
    var STATUS = { queued: 'Queued', running: 'Rendering', encoding: 'Making MP4', done: 'Done', failed: 'Failed', cancelled: 'Cancelled' };

    function fileUrl(job, name, dl) {
        return '/api/jobs/' + job.id + '/files/' + encodeURIComponent(name) + (dl ? '?dl=1' : '');
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
        zip.hidden = !job.outputs.length || job.status === 'running' || job.status === 'queued';
        zip.href = '/api/jobs/' + job.id + '/zip';
        var vidLink = el.querySelector('[data-act="video"]'), vid = el.querySelector('.job-video');
        vidLink.hidden = vid.hidden = !job.video;
        if (job.video && !vid.src) {
            vid.src = fileUrl(job, job.video);
            vidLink.href = fileUrl(job, job.video, true);
        }
        el.querySelector('[data-act="cancel"]').hidden = ['queued', 'running', 'encoding'].indexOf(job.status) === -1;

        if (el._outputs !== job.outputs.length) {
            el._outputs = job.outputs.length;
            renderGallery(el, job);
        }
        var log = el.querySelector('.log');
        if (!log.hidden && (job.status === 'running' || job.status === 'encoding' || !log.textContent)) loadLog(job.id, el);
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
            var active = d.jobs.some(function (j) { return ['queued', 'running', 'encoding'].indexOf(j.status) !== -1; });
            pollTimer = setTimeout(poll, active ? 2000 : 8000);
            if (active) loadSystem();
        }).catch(function () {
            pollTimer = setTimeout(poll, 5000);
        }).then(function () { busy = false; });
    }

    loadSystem();
    poll(true);
})();
