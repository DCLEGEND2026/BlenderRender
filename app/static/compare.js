// Compare renders: two renders (or two frames) on top of each other with a divider to
// drag, or side by side. The choice is kept in the address, so a reload keeps it.
(function () {
    'use strict';

    var stage = document.getElementById('stage'), canvas = document.getElementById('canvas');
    var imgA = document.getElementById('img-a'), imgB = document.getElementById('img-b');
    var line = document.getElementById('line'), note = document.getElementById('note'), empty = document.getElementById('empty');
    var same = document.getElementById('same'), sameBox = document.getElementById('same-box');
    var sides = {};
    Array.prototype.forEach.call(document.querySelectorAll('.cmp-side'), function (el) {
        sides[el.dataset.side] = {
            el: el, job: null, frame: null,
            jobSel: el.querySelector('.cmp-job'), frameSel: el.querySelector('.cmp-frame'), meta: el.querySelector('.cmp-meta')
        };
    });
    var jobs = [], pos = 0.5, view = 'slider', zoom = 0, about = '', mismatch = '';
    var IMAGE = /\.(png|jpe?g|webp)$/i;

    // ---- Helpers -----------------------------------------------------------------
    function api(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (r) {
            if (r.status === 401) { location.href = '/login'; throw new Error('Not logged in'); }
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        });
    }
    function clock(sec) {
        sec = Math.max(0, Math.round(sec));
        var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
        return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }
    function frames(job) { return job.outputs.filter(function (n) { return IMAGE.test(n); }).sort(); }
    function frameNo(name) { var m = /(\d+)\.\w+$/.exec(name); return m ? Number(m[1]) : null; }
    function fileUrl(job, name) {
        return '/api/jobs/' + job.id + '/files/' + encodeURIComponent(name) + '?v=' + job.version;
    }
    function byId(id) { return jobs.filter(function (j) { return j.id === id; })[0] || null; }

    // The settings that make two renders of a file look different, in words.
    function settings(job) {
        var o = job.opts, comp = (o.compositing || {}).mode;
        var nodes = comp === 'custom' ? o.compositing.graph.nodes.length - 2 : 0;
        return [
            ['Engine', job.info.engine ? job.info.engine.replace('BLENDER_', '').replace('_NEXT', '').toLowerCase() : o.engine],
            ['Size', job.info.width ? job.info.width + '×' + job.info.height : ''],
            ['Samples', (o.samples || o.eevee_samples || 'file') + ' samples'],
            ['Noise threshold', o.noise_threshold === null || o.noise_threshold === undefined ? 'from file' : o.noise_threshold],
            ['Denoise', o.denoise === 'file' ? 'from file' : o.denoise],
            ['Light bounces', o.max_bounces === null || o.max_bounces === undefined ? 'from file' : o.max_bounces],
            ['Compositing', comp === 'custom' ? nodes + ' custom node' + (nodes === 1 ? '' : 's') : comp === 'off' ? 'off' : 'as in file'],
            ['Blender', job.info.blender || '']
        ];
    }
    function label(job) {
        var n = jobs.length - jobs.indexOf(job);  // #1 is the oldest
        var t = new Date(job.created * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        var s = settings(job);
        return '#' + n + ' ' + job.name + ' · ' + s[2][1] + (s[1][1] ? ' · ' + s[1][1] : '') + ' · ' + t;
    }

    // ---- Choosing what to compare ------------------------------------------------
    function fillJobs(side) {
        var s = sides[side];
        s.jobSel.innerHTML = '';
        jobs.forEach(function (j) {
            var opt = document.createElement('option');
            opt.value = j.id;
            opt.textContent = label(j);
            s.jobSel.appendChild(opt);
        });
        s.jobSel.value = s.job.id;
    }
    function fillFrames(side) {
        var s = sides[side], list = frames(s.job);
        s.frameSel.innerHTML = '';
        list.forEach(function (name) {
            var opt = document.createElement('option');
            opt.value = name;
            opt.textContent = frameNo(name) !== null ? 'Frame ' + frameNo(name) : name;
            s.frameSel.appendChild(opt);
        });
        s.frameSel.hidden = list.length < 2;
        if (list.indexOf(s.frame) === -1) s.frame = list[0];
        s.frameSel.value = s.frame;
    }
    // B follows A's frame (by frame number) while "Same frame" is ticked.
    function linked() {
        return sides.a.job !== sides.b.job && frames(sides.a.job).length > 1 && frames(sides.b.job).length > 1;
    }
    function follow(from, to) {
        if (!linked() || !same.checked) return;
        var want = frameNo(sides[from].frame), hit = frames(sides[to].job).filter(function (n) { return frameNo(n) === want; })[0];
        if (hit) { sides[to].frame = hit; sides[to].frameSel.value = hit; }
    }
    function set(side, job, frame) {
        var s = sides[side];
        s.job = job;
        s.frame = frame || s.frame;
        fillJobs(side);
        fillFrames(side);
    }
    function show() {
        var a = sides.a, b = sides.b;
        sameBox.hidden = !linked();
        imgA.src = fileUrl(a.job, a.frame);
        imgB.src = fileUrl(b.job, b.frame);
        ['a', 'b'].forEach(function (k) {
            var j = sides[k].job;
            sides[k].meta.textContent = settings(j).filter(function (x) { return x[1] !== ''; }).slice(0, 3)
                .map(function (x) { return x[1]; }).join(' · ') + (j.elapsed ? ' · rendered in ' + clock(j.elapsed) : '');
        });
        describe();
        var q = new URLSearchParams({ a: a.job.id, fa: a.frame, b: b.job.id, fb: b.frame });
        history.replaceState(null, '', '/compare?' + q.toString());
    }
    // What differs between A and B, so you know what you're looking at.
    function describe() {
        var a = sides.a, b = sides.b, sa = settings(a.job), sb = settings(b.job), diff = [];
        if (a.job === b.job) {
            about = a.frame === b.frame ? 'Both sides show the same image. Pick another render or frame for one of them.' :
                'Two frames of the same render.';
            return writeNote();
        }
        sa.forEach(function (x, i) {
            if (String(x[1]) === String(sb[i][1])) return;
            var named = String(x[1]).indexOf(x[0].toLowerCase()) !== -1;  // "64 samples" says what it is
            diff.push((named ? '' : x[0] + ': ') + (x[1] || '?') + ' → ' + (sb[i][1] || '?'));
        });
        if (a.job.name !== b.job.name) diff.unshift('File: ' + a.job.name + ' → ' + b.job.name);
        about = diff.length ? 'A → B: ' + diff.join(' · ') : 'Same settings on both.';
        writeNote();
    }
    function writeNote() { note.textContent = about + (mismatch ? ' ' + mismatch : ''); }

    ['a', 'b'].forEach(function (k) {
        var s = sides[k], other = k === 'a' ? 'b' : 'a';
        s.jobSel.addEventListener('change', function () {
            set(k, byId(s.jobSel.value));
            follow(other, k);
            if (s.job === sides[other].job && s.frame === sides[other].frame) {  // not the very same picture
                var next = frames(s.job).filter(function (n) { return n !== s.frame; })[0];
                if (next) { s.frame = next; s.frameSel.value = next; }
            }
            show();
        });
        s.frameSel.addEventListener('change', function () {
            s.frame = s.frameSel.value;
            follow(k, other);
            show();
        });
    });
    same.addEventListener('change', function () { follow('a', 'b'); show(); });
    document.getElementById('swap').addEventListener('click', function () {
        var a = { job: sides.a.job, frame: sides.a.frame };
        set('a', sides.b.job, sides.b.frame);
        set('b', a.job, a.frame);
        show();
    });

    // ---- Layout, divider and zoom --------------------------------------------------
    function layout() {
        var w = imgA.naturalWidth || imgB.naturalWidth, h = imgA.naturalHeight || imgB.naturalHeight;
        if (!w || !h) return;
        var gap = view === 'side' ? 8 : 0, cols = view === 'side' ? 2 : 1;
        var fit = Math.min((stage.clientWidth - 24 - gap) / cols / w, (stage.clientHeight - 24) / h);
        var s = zoom || Math.min(fit, 4);
        canvas.style.width = Math.round(w * s * cols + gap) + 'px';
        canvas.style.height = Math.round(h * s) + 'px';
        var sizeA = imgA.naturalWidth + '×' + imgA.naturalHeight, sizeB = imgB.naturalWidth + '×' + imgB.naturalHeight;
        mismatch = imgA.complete && imgB.complete && sizeA !== sizeB ? '(A is ' + sizeA + ' px, B is ' + sizeB + ' px: B is scaled to fit.)' : '';
        writeNote();
    }
    function setPos(p) {
        pos = Math.min(1, Math.max(0, p));
        imgA.style.clipPath = view === 'slider' ? 'inset(0 ' + ((1 - pos) * 100) + '% 0 0)' : 'none';
        line.style.left = (pos * 100) + '%';
    }
    imgA.addEventListener('load', layout);
    imgB.addEventListener('load', layout);
    window.addEventListener('resize', layout);

    // Fit: drag anywhere to move the divider. Zoomed in: drag the divider itself, and
    // drag anywhere else to move around the picture.
    var drag = null;
    stage.addEventListener('pointerdown', function (e) {
        if (e.button !== 0 || !sides.a.job) return;
        var onLine = !!e.target.closest('.cmp-line');
        if (view === 'slider' && (zoom === 0 || onLine)) {
            drag = { type: 'divider' };
            moveDivider(e);
        } else {
            drag = { type: 'pan', x: e.clientX, y: e.clientY, sl: stage.scrollLeft, st: stage.scrollTop };
            stage.classList.add('panning');
        }
        stage.setPointerCapture(e.pointerId);
        e.preventDefault();
        stage.focus({ preventScroll: true });
    });
    stage.addEventListener('pointermove', function (e) {
        if (!drag) return;
        if (drag.type === 'divider') moveDivider(e);
        else {
            stage.scrollLeft = drag.sl - (e.clientX - drag.x);
            stage.scrollTop = drag.st - (e.clientY - drag.y);
        }
    });
    function stop() { drag = null; stage.classList.remove('panning'); }
    stage.addEventListener('pointerup', stop);
    stage.addEventListener('pointercancel', stop);
    function moveDivider(e) {
        var r = canvas.getBoundingClientRect();
        setPos((e.clientX - r.left) / r.width);
    }
    stage.addEventListener('keydown', function (e) {
        var step = e.shiftKey ? 0.1 : 0.02;
        if (e.key === 'ArrowLeft') setPos(pos - step);
        else if (e.key === 'ArrowRight') setPos(pos + step);
        else if (e.key === 'Home') setPos(0);
        else if (e.key === 'End') setPos(1);
        else return;
        e.preventDefault();
    });

    Array.prototype.forEach.call(document.querySelectorAll('input[name=view]'), function (r) {
        r.addEventListener('change', function () {
            view = r.value;
            stage.classList.toggle('side', view === 'side');
            setPos(pos);
            layout();
        });
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name=zoom]'), function (r) {
        r.addEventListener('change', function () {
            zoom = Number(r.value);
            stage.classList.toggle('zoomed', zoom > 0);
            layout();
        });
    });

    // ---- Start ---------------------------------------------------------------------
    function start() {
        api('/api/jobs').then(function (d) {
            jobs = d.jobs.filter(function (j) { return frames(j).length; });
            var total = jobs.reduce(function (n, j) { return n + frames(j).length; }, 0);
            if (!jobs.length || total < 2) {
                document.querySelector('.cmp-pick').hidden = true;
                canvas.hidden = true;
                empty.hidden = false;
                empty.textContent = jobs.length ? 'There is only one picture so far. Render the file again (Render again on its card) with other settings, then compare the two here.' :
                    'Nothing to compare yet. Finished renders with PNG or JPEG images show up here.';
                note.textContent = '';
                return;
            }
            var q = new URLSearchParams(location.search);
            var a = byId(q.get('a')) || jobs[0];
            // B: what you asked for, else the latest other render of the same file, else the latest other render.
            var b = byId(q.get('b')) ||
                jobs.filter(function (j) { return j !== a && j.name === a.name; })[0] ||
                jobs.filter(function (j) { return j !== a; })[0] || a;
            var fa = q.get('fa'), fb = q.get('fb');
            if (b === a && !fb) fb = frames(a).filter(function (n) { return n !== (fa || frames(a)[0]); })[0];
            set('a', a, fa);
            set('b', b, fb);
            if (!q.get('fb')) follow('a', 'b');
            show();
            setPos(pos);
        }).catch(function (err) {
            empty.hidden = false;
            empty.textContent = 'Could not load the renders: ' + err.message;
        });
    }
    start();
})();
