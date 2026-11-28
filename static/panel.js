    // Sessions are stored in an HttpOnly cookie; never keep the password client-side.
    localStorage.removeItem('agentcontrol_auth_' + location.host);
    const list = document.getElementById('list');
    const msg = document.getElementById('msg');
    const ICONS = {
      x: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>',
      check: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m20 6-11 11-5-5"/></svg>',
      alert: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
      dot: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/></svg>',
    };
    let projectSearch = '';
    let projectFilter = 'all';
    let projectSort = 'activity';
    let connOnline = true;

    function toast(message, type = 'info', timeout = 4200) {
      let wrap = document.querySelector('.toast-wrap');
      if (!wrap) {
        wrap = document.createElement('div');
        wrap.className = 'toast-wrap';
        wrap.setAttribute('role', 'status');
        wrap.setAttribute('aria-live', 'polite');
        document.body.appendChild(wrap);
      }
      const item = document.createElement('div');
      item.className = 'toast ' + type;
      item.textContent = message;
      wrap.appendChild(item);
      setTimeout(() => item.remove(), timeout);
    }

    function setConnection(online) {
      connOnline = online;
      const el = document.getElementById('live-indicator');
      if (!el) return;
      el.classList.toggle('offline', !online);
      el.innerHTML = `${ICONS.dot}<span>${online ? 'live' : 'offline'}</span>`;
    }

    function applyTheme(name) {
      const theme = ['dark', 'light', 'nord'].includes(name) ? name : 'dark';
      document.documentElement.dataset.theme = theme;
      try { localStorage.setItem('agentcontrol_theme', theme); } catch (_) {}
      const select = document.getElementById('theme-select');
      if (select) select.value = theme;
    }

    function bootTheme() {
      let saved = 'dark';
      try { saved = localStorage.getItem('agentcontrol_theme') || saved; } catch (_) {}
      if (saved === 'dark' && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
        saved = 'light';
      }
      applyTheme(saved);
      document.getElementById('theme-select')?.addEventListener('change', (e) => applyTheme(e.target.value));
      document.querySelectorAll('.modal-close-x').forEach((btn) => { btn.innerHTML = ICONS.x; });
      setConnection(true);
    }
    let busy = false;
    let setupNeedsPassword = false;
    let setupNeedsApiKey = false;
    let refreshTimer = null;
    let history = [];
    let cpuCores = 1;
    let netPeak = 1024;
    let modalOpen = false;
    let expandedDocker = new Set();
    let lastFoldersJson = '';
    let ctxProjectName = null;
    let longPressTimer = null;
    let longPressRow = null;

    async function apiFetch(url, opts = {}) {
      opts.headers = { ...(opts.headers || {}) };
      opts.credentials = opts.credentials || 'same-origin';
      const res = await fetch(url, opts);
      if (res.status === 401) { location.replace('/'); throw new Error('unauthorized'); }
      if (res.status === 403) {
        const data = await res.json().catch(() => ({}));
        if (data.needs_password) { showSetup(); throw new Error('setup'); }
        throw new Error('forbidden');
      }
      return res;
    }

    function showSetup() {
      document.getElementById('setup-gate').classList.remove('hidden');
      document.getElementById('app').classList.add('hidden');
    }


    function showApp() {
      document.getElementById('setup-gate').classList.add('hidden');
      document.getElementById('app').classList.remove('hidden');
      if (!refreshTimer) refreshTimer = setInterval(refreshAll, 2000);
      loadSettingsPanel();
      refreshAll();
      checkAndApplyUpdate(false);
      const url = new URL(location.href);
      const project = url.searchParams.get('opencode');
      if (project) {
        url.searchParams.delete('opencode');
        window.history.replaceState(null, '', url);
        openOpencodeProject(project, true);
      }
    }

    function updatePageTitle(sys) {
      const host = window.location.hostname || '';
      const hn = sys?.hostname || document.querySelector('meta[name=ac-hostname]')?.content || '';
      const ip = sys?.ip || document.querySelector('meta[name=ac-ip]')?.content || '';
      const workers = sys?.panel?.running_workers ?? 0;
      const parts = [host, hn];
      if (ip) parts.push(ip);
      parts.push(workers + 'w');
      document.title = parts.join(' · ') + ' | AgentControl';
    }

    async function checkAndApplyUpdate(apply = false) {
      const banner = document.getElementById('update-banner');
      const btn = document.getElementById('panel-update-btn');
      try {
        const res = await apiFetch('/api/update/check');
        const data = await res.json();
        const localV = data.local?.version || '?';
        const remoteV = data.remote?.version || '';
        if (btn) {
          btn.textContent = data.update_available ? `Update v${localV}→${remoteV}` : `Panel v${localV}`;
        }
        if (!data.update_available) {
          if (apply) {
            banner.classList.remove('hidden');
            banner.textContent = `Panel is up to date (v${localV}).`;
            setTimeout(() => banner.classList.add('hidden'), 4000);
          }
          return;
        }
        banner.classList.remove('hidden');
        if (!apply) {
          banner.innerHTML = `Panel v${localV} → v${remoteV} available. <button class="btn btn-sm btn-primary" type="button" data-action="checkAndApplyUpdate" data-arg="1">Update now</button>`;
          return;
        }
        if (btn) { btn.disabled = true; btn.classList.add('loading'); btn.textContent = 'Updating…'; }
        banner.textContent = remoteV
          ? `Downloading v${remoteV} from GitHub and updating…`
          : 'Checking GitHub for updates…';
        await apiFetch('/api/update/apply', { method: 'POST' });
        const start = Date.now();
        while (Date.now() - start < 180000) {
          await new Promise(r => setTimeout(r, 3000));
          try {
            const health = await fetch('/health');
            if (health.ok) {
              const h = await health.json();
              if (h.version === remoteV) {
                banner.textContent = `Updated to v${remoteV} — reloading…`;
                location.reload();
                return;
              }
            }
          } catch (_) {}
          try {
            const st = await apiFetch('/api/update/status');
            const status = await st.json();
            const lastLine = (status.log_tail || '').trim().split('\n').pop();
            if (lastLine) banner.textContent = `Updating… ${lastLine}`;
            if (!status.in_progress && status.local?.version === remoteV) {
              location.reload();
              return;
            }
          } catch (_) {}
        }
        banner.textContent = 'Update is taking longer than expected — check /var/lib/agentcontrol/update.log';
      } catch (e) {
        if (e.message !== 'unauthorized' && e.message !== 'setup') {
          banner.classList.remove('hidden');
          banner.textContent = 'Update check failed: ' + (e.message || 'unknown');
        }
      } finally {
        if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
      }
    }

    async function submitSetup() {
      const errEl = document.getElementById('setup-error');
      errEl.textContent = '';
      try {
        const key = document.getElementById('setup-api-key').value.trim();
        const res = await apiFetch('/api/setup/api-key', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key, setup_token: new URLSearchParams(location.search).get('setup') || '' }),
        });
        const data = await res.json();
        if (!res.ok) { errEl.textContent = data.error || 'Failed'; return; }
        showApp();
      } catch (e) {
        if (e.message !== 'unauthorized') errEl.textContent = 'Setup failed';
      }
    }


    async function boot() {
      const st = await (await apiFetch('/api/setup/status')).json();
      if (st.needs_api_key) { showSetup(); return; }
      showApp();
    }

    function healthLevel(pct, loadMode) {
      if (loadMode) {
        if (pct >= 100) return 'bad';
        if (pct >= 70) return 'warn';
        return 'ok';
      }
      if (pct >= 90) return 'bad';
      if (pct >= 75) return 'warn';
      return 'ok';
    }

    function statusIcon(level) {
      return level === 'bad' ? ICONS.x : level === 'warn' ? ICONS.alert : ICONS.check;
    }

    function applyStatus(metricId, statusId, level) {
      const card = document.getElementById(metricId);
      const st = document.getElementById(statusId);
      if (card) card.className = 'metric ' + level;
      if (st) { st.className = 'status ' + level; st.innerHTML = statusIcon(level); }
    }

    function pctClass(p) { return p >= 90 ? 'bad' : p >= 75 ? 'warn' : ''; }

    function drawGrid(ctx, w, h, maxY, percentMode) {
      ctx.strokeStyle = 'rgba(139,147,167,.15)';
      ctx.lineWidth = 1;
      if (percentMode) {
        [0.5, 1].forEach(frac => {
          const y = h - 2 - frac * (h - 4);
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(w, y);
          ctx.stroke();
        });
      }
    }

    function plotLine(ctx, values, w, h, max, color, dashed) {
      if (!values.length) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      if (dashed) ctx.setLineDash([3, 2]); else ctx.setLineDash([]);
      ctx.beginPath();
      values.forEach((v, i) => {
        const x = (i / Math.max(values.length - 1, 1)) * (w - 2) + 1;
        const y = h - 2 - (Math.min(v, max) / max) * (h - 4);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }

    function drawSparkline(canvasId, series, opts = {}) {
      const c = document.getElementById(canvasId);
      if (!c) return;
      const ctx = c.getContext('2d');
      const w = c.width, h = c.height;
      ctx.clearRect(0, 0, w, h);
      const max = opts.max ?? 100;
      drawGrid(ctx, w, h, max, opts.percent !== false);
      const list = Array.isArray(series[0]) ? series : [series];
      const colors = opts.colors || ['#5b8cff'];
      const dashed = opts.dashed || [];
      list.forEach((values, idx) => plotLine(ctx, values, w, h, max, colors[idx] || colors[0], dashed[idx]));
    }

    function drawChart(canvasId, series, opts = {}) {
      const c = document.getElementById(canvasId);
      if (!c) return;
      const ctx = c.getContext('2d');
      const w = c.width, h = c.height;
      ctx.clearRect(0, 0, w, h);
      const max = opts.max ?? 100;
      drawGrid(ctx, w, h, max, opts.percent !== false);
      const list = Array.isArray(series[0]) ? series : [series];
      const colors = opts.colors || ['#5b8cff'];
      const dashed = opts.dashed || [];
      list.forEach((values, idx) => {
        if (!values.length) return;
        const color = colors[idx] || colors[0];
        if (!dashed[idx]) {
          ctx.fillStyle = color + '22';
          ctx.beginPath();
          values.forEach((v, i) => {
            const x = (i / Math.max(values.length - 1, 1)) * (w - 4) + 2;
            const y = h - 4 - (Math.min(v, max) / max) * (h - 8);
            i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
          });
          ctx.lineTo(w - 2, h - 2);
          ctx.lineTo(2, h - 2);
          ctx.closePath();
          ctx.fill();
        }
        plotLine(ctx, values, w, h, max, color, dashed[idx]);
      });
    }

    function netMax(values) {
      const peak = values.length ? Math.max(...values) : 0;
      return Math.max(peak * 1.2, 1024);
    }

    function renderShareBars(cpuShare, memShare) {
      return `<div class="share-bars">
        <div class="share-row"><span>CPU</span><div class="bar"><i class="${pctClass(cpuShare)}" style="width:${Math.min(cpuShare,100)}%"></i></div><span class="pct">${cpuShare.toFixed(1)}%</span></div>
        <div class="share-row"><span>RAM</span><div class="bar"><i class="${pctClass(memShare)}" style="width:${Math.min(memShare,100)}%;background:var(--green)"></i></div><span class="pct">${memShare.toFixed(1)}%</span></div>
      </div>`;
    }

    function renderDockerGroups(docker) {
      if (!docker.available) return '<div class="full" style="color:var(--muted)">Docker not installed</div>';
      const groups = docker.groups || [];
      if (!groups.length) return '<div class="full" style="color:var(--muted)">No running containers</div>';
      const totals = docker.totals || {};
      const head = `<div class="full docker-totals">Docker total: <strong>${(totals.cpu_percent||0).toFixed(1)}% CPU</strong> · <strong>${totals.mem_human||'0 B'} RAM</strong> — shares below are % of all Docker usage</div>`;
      const body = groups.map(g => {
        const open = expandedDocker.has(g.id) ? ' open' : '';
        const chev = expandedDocker.has(g.id) ? '▾' : '▸';
        const kind = g.type === 'compose' ? `compose · ${g.container_count} containers` : 'standalone';
        const items = (g.containers || []).map(c => `
          <div class="docker-item">
            <div class="docker-item-name">${esc(c.service || c.name)}</div>
            ${renderShareBars(c.cpu_share || 0, c.mem_share || 0)}
            <div style="font-size:.62rem;color:var(--muted);margin-top:4px">${c.cpu_percent.toFixed(1)}% host CPU · ${c.mem_usage}</div>
          </div>`).join('');
        return `<div class="docker-group${open}" data-docker-group="${esc(g.id)}">
          <div class="docker-group-head" data-docker-toggle="${esc(g.id)}">
            <div>
              <div class="docker-group-title">${esc(g.label)} <span class="docker-group-chevron">${chev}</span></div>
              <div class="docker-group-sub">${kind}</div>
              ${renderShareBars(g.cpu_share || 0, g.mem_share || 0)}
            </div>
            <div class="docker-group-pct"><strong>${g.cpu_share.toFixed(1)}%</strong> CPU<br><strong>${g.mem_share.toFixed(1)}%</strong> RAM</div>
          </div>
          <div class="docker-group-body">${items}</div>
        </div>`;
      }).join('');
      return `<div class="full docker-list">${head}${body}</div>`;
    }

    function toggleDockerGroup(id) {
      if (expandedDocker.has(id)) expandedDocker.delete(id); else expandedDocker.add(id);
      document.querySelectorAll('.docker-group').forEach(el => {
        const gid = el.dataset.dockerGroup;
        const open = expandedDocker.has(gid);
        el.classList.toggle('open', open);
        const chev = el.querySelector('.docker-group-chevron');
        if (chev) chev.textContent = open ? '▾' : '▸';
      });
    }

    function renderSystem(s) {
      cpuCores = s.cpu.cores;
      const loadPct = s.cpu.load_percent;
      const cpuLvl = healthLevel(Math.max(s.cpu.percent, loadPct), true);
      const ramLvl = healthLevel(s.memory.percent, false);
      const diskLvl = healthLevel(s.disk_root.percent, false);
      const swapPct = s.memory.swap_percent;
      const swapLvl = swapPct != null ? healthLevel(swapPct, false) : 'ok';

      applyStatus('metric-cpu', 'st-cpu', cpuLvl);
      applyStatus('metric-ram', 'st-ram', ramLvl);
      applyStatus('metric-disk', 'st-disk', diskLvl);
      applyStatus('metric-swap', 'st-swap', swapLvl);

      document.getElementById('m-cpu-line').innerHTML =
        `${s.cpu.percent}% <span class="dim">· ${s.cpu.cores}c · load ${loadPct}%</span>`;
      document.getElementById('m-ram-line').innerHTML =
        `${s.memory.percent}% <span class="dim">· ${s.memory.used_human}/${s.memory.total_human}</span>`;
      document.getElementById('m-disk-line').innerHTML =
        `${s.disk_root.percent}% <span class="dim">· ${s.disk_root.used_human}/${s.disk_root.total_human}</span>`;
      document.getElementById('m-swap-line').innerHTML = swapPct != null
        ? `${swapPct}% <span class="dim">· ${s.memory.swap_used_human || '—'} used</span>`
        : `<span class="dim">none</span>`;
      document.getElementById('m-net-line').innerHTML =
        `↓${s.network.rx_rate_human} <span class="dim">↑${s.network.tx_rate_human}</span>`;

      const cpuHist = history.map(p => p.cpu);
      const loadHist = history.map(p => p.load_pct ?? 0);
      const ramHist = history.map(p => p.ram);
      const diskHist = history.map(p => p.disk);
      const swapHist = history.map(p => p.swap ?? 0);
      const netHist = history.map(p => p.net ?? 0);
      netPeak = netMax(netHist);

      drawSparkline('spark-cpu', [cpuHist.slice(-30), loadHist.slice(-30)], {
        max: 100, percent: true, colors: ['#5b8cff', '#f0a060'], dashed: [false, true],
      });
      drawSparkline('spark-ram', ramHist.slice(-30), { max: 100, percent: true, colors: ['#3dd68c'] });
      drawSparkline('spark-disk', diskHist.slice(-30), { max: 100, percent: true, colors: ['#e8c468'] });
      drawSparkline('spark-swap', swapHist.slice(-30), { max: 100, percent: true, colors: ['#c678dd'] });
      drawSparkline('spark-net', netHist.slice(-30), { max: netPeak, percent: false, colors: ['#9b8cff'] });

      drawChart('chart-cpu', [cpuHist, loadHist], {
        max: 100, percent: true, colors: ['#5b8cff', '#f0a060'], dashed: [false, true],
      });
      drawChart('chart-ram', ramHist, { max: 100, percent: true, colors: ['#3dd68c'] });
      drawChart('chart-disk', diskHist, { max: 100, percent: true, colors: ['#e8c468'] });
      drawChart('chart-swap', swapHist, { max: 100, percent: true, colors: ['#c678dd'] });
      drawChart('chart-net', netHist, { max: netPeak, percent: false, colors: ['#9b8cff'] });

      const cpuPill = document.getElementById('toolbar-cpu-pill');
      if (cpuPill) cpuPill.textContent = `CPU ${s.cpu.percent}% · RAM ${s.memory.percent}%`;

      const dockerSummary = s.docker.available
        ? `${s.docker.running} running / ${s.docker.total} total · v${s.docker.version || '?'}`
        : 'Docker not installed';

      document.getElementById('server-detail-grid').innerHTML = `
        <div class="full"><strong>${s.hostname}</strong> · ${s.ip}</div>
        <div class="full">${s.os} · kernel ${s.kernel}</div>
        <div><span>Uptime:</span> ${s.uptime_human}</div>
        <div><span>Load:</span> ${s.cpu.load_1} / ${s.cpu.load_5} / ${s.cpu.load_15} (${loadPct}% of ${s.cpu.cores} cores)</div>
        <div class="full"><span>Network:</span> ↓ ${s.network.rx_rate_human} · ↑ ${s.network.tx_rate_human}</div>
        <div class="full"><span>Disk /:</span> ${s.disk_root.used_human} / ${s.disk_root.total_human} (${s.disk_root.free_human} free)</div>
        ${s.disk_scan_root ? `<div class="full"><span>Disk ${s.panel.scan_root}:</span> ${s.disk_scan_root.used_human} / ${s.disk_scan_root.total_human}</div>` : ''}
        <div><span>Workers:</span> ${s.panel.running_workers} active</div>
        <div><span>Swap:</span> ${swapPct != null ? swapPct + '% · ' + (s.memory.swap_used_human || '—') : 'none'}</div>
        <div><span>Agent CLI:</span> ${s.agent.version || 'not installed'}</div>
        <div class="full section-title" style="margin-top:8px">Docker · ${dockerSummary}</div>
        ${renderDockerGroups(s.docker)}
        <div class="full" style="color:var(--muted);font-size:.72rem;margin-top:6px">History: ${history.length} samples (~${Math.round(history.length * 2 / 60)} min) · refreshes every 2s</div>`;
    }

    function openMetricsModal() {
      const range = document.getElementById('chart-range');
      if (range) range.value = chartRange;
      document.getElementById('metrics-modal').classList.remove('hidden');
      document.getElementById('server-details').classList.add('open');
    }
    function closeMetricsModal() { document.getElementById('metrics-modal').classList.add('hidden'); }
    function openSettingsModal() {
      document.getElementById('settings-modal').classList.remove('hidden');
      loadSettingsPanel();
    }
    function closeSettingsModal() { document.getElementById('settings-modal').classList.add('hidden'); }
    function openProxyModal() {
      document.getElementById('proxy-modal').classList.remove('hidden');
      refreshPoolStatus();
    }
    function closeProxyModal() { document.getElementById('proxy-modal').classList.add('hidden'); }

    function renderPoolStatus(data) {
      const enabled = data.enabled;
      const pill = document.getElementById('toolbar-proxy-pill');
      if (pill) {
        if (!enabled) pill.textContent = 'Proxy: direct';
        else if (data.listening) pill.textContent = 'Proxy: ' + (data.active_proxy?.name || 'active');
        else pill.textContent = 'Proxy: down';
      }
      document.getElementById('pool-enabled').checked = !!enabled;
      const subs = data.subscriptions || [];
      const subList = document.getElementById('pool-sub-list');
      if (subList) {
        subList.innerHTML = subs.length ? subs.map(s => {
          const err = s.last_error ? `<span style="color:var(--red)">${esc(s.last_error)}</span>` : '';
          const count = s.last_link_count != null ? `${s.last_link_count} links` : '—';
          const fetched = s.last_fetch_at ? new Date(s.last_fetch_at).toLocaleString() : 'never';
          return `<div class="pool-item"><span><strong>${esc(s.name || s.id)}</strong><br><span style="color:var(--muted)">${esc(s.url_preview || '')}</span></span><span>${count}<br>${fetched}${err ? '<br>' + err : ''}</span></div>`;
        }).join('') : '<p class="settings-note">No subscriptions — add a URL above</p>';
      }
      const list = document.getElementById('pool-proxy-list');
      const proxies = data.proxies || [];
      list.innerHTML = proxies.length ? proxies.map(p => {
        const chk = p.last_check || {};
        const cls = chk.ok ? 'ok' : (chk.at ? 'bad' : '');
        const lat = chk.latency_ms != null ? chk.latency_ms + 'ms' : '—';
        const active = data.active_proxy_id === p.id ? ' ★' : '';
        return `<div class="pool-item ${cls}"><span><strong>${esc(p.name || p.id)}</strong>${active}<br><span style="color:var(--muted)">${esc(p.share_preview || '')}</span></span><span>${chk.ok ? 'OK ' + lat : esc(chk.error || 'untested')}</span></div>`;
      }).join('') : '<p class="settings-note">No proxies — add links or subscriptions</p>';
      const pills = [];
      if (enabled) pills.push(pill(data.listening ? 'Listening' : 'Not listening', data.listening));
      if (data.last_check_at) pills.push(pill('Last check ' + new Date(data.last_check_at).toLocaleTimeString(), true));
      document.getElementById('pool-pills').innerHTML = pills.join('');
      const sel = data.last_selection;
      document.getElementById('pool-status-text').textContent = sel
        ? `Active: ${sel.name || sel.proxy_id || '—'} · ${sel.reason || ''}`
        : (data.recent_logs && data.recent_logs[0] ? data.recent_logs[0].message : '—');
    }

    async function refreshPoolStatus(live) {
      try {
        const res = await apiFetch('/api/proxy/pool' + (live ? '?live=1' : ''));
        const data = await res.json();
        renderPoolStatus(data);
      } catch (e) {}
    }

    async function onPoolEnabledChange() {
      const enabled = document.getElementById('pool-enabled').checked;
      const res = await apiFetch('/api/proxy/pool', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const data = await res.json();
      if (!res.ok) document.getElementById('pool-status-text').textContent = data.error || 'Failed';
      await refreshPoolStatus(true);
    }

    async function addPoolSubscription() {
      const url = document.getElementById('pool-sub-url').value.trim();
      if (!url) return;
      document.getElementById('pool-status-text').textContent = 'Fetching subscription…';
      try {
        const res = await apiFetch('/api/proxy/pool/subscription', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url }),
        });
        const data = await res.json();
        if (!res.ok) {
          document.getElementById('pool-status-text').textContent = data.error || 'Subscription failed';
          return;
        }
        document.getElementById('pool-sub-url').value = '';
        document.getElementById('pool-status-text').textContent = `Added — ${data.subscription?.last_link_count ?? 0} links`;
        await refreshPoolStatus(true);
      } catch (e) {
        document.getElementById('pool-status-text').textContent = e.message || 'Subscription failed';
      }
    }

    async function addPoolLinks() {
      const lines = document.getElementById('pool-links-input').value.split('\n').map(s => s.trim()).filter(Boolean);
      for (const share_url of lines) {
        await apiFetch('/api/proxy/pool/link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ share_url }),
        });
      }
      document.getElementById('pool-links-input').value = '';
      await refreshPoolStatus(true);
    }

    async function testPoolProxies() {
      document.getElementById('pool-status-text').textContent = 'Testing all proxies…';
      const res = await apiFetch('/api/proxy/pool/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      const logEl = document.getElementById('pool-log');
      logEl.classList.remove('hidden');
      logEl.textContent = JSON.stringify(data.report || data, null, 2);
      renderPoolStatus(data.status || data);
    }

    async function ensurePoolProxy() {
      const res = await apiFetch('/api/proxy/pool/ensure?force=1', { method: 'POST' });
      const data = await res.json();
      renderPoolStatus(data.status || data);
      document.getElementById('pool-status-text').textContent = data.ok ? 'Proxy ready' : (data.error || 'Failed');
    }

    async function refreshPoolSubscriptions() {
      await apiFetch('/api/proxy/pool/refresh', { method: 'POST' });
      await refreshPoolStatus(true);
    }

    function toggleServerDetails() {
      const el = document.getElementById('server-details');
      el.classList.toggle('open');
    }

    function esc(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
    }

    function openCursorUrl(url) {
      if (!url) return;
      window.location.href = url;
    }

    function renderProjectActions(folder) {
      const parts = [];
      if (folder.running && folder.agent_url) {
        const encUrl = encodeURIComponent(folder.agent_url);
        parts.push(`<button type="button" class="btn btn-primary btn-sm" data-action="openCursorUrl" data-url="${esc(folder.agent_url)}">Cursor</button>`);
      }
      const dis = busy ? ' disabled' : '';
      parts.push(`<button type="button" class="btn btn-ghost btn-sm" data-action="details">Details</button>`);
      parts.push(`<button type="button" class="btn btn-ghost btn-sm" data-action="logs">Logs</button>`);
      parts.push(`<button type="button" class="btn btn-ghost btn-sm" data-action="opencode"${dis}>OpenCode</button>`);
      parts.push(`<button type="button" class="btn btn-primary btn-sm" data-action="start"${dis}>Start</button>`);
      parts.push(`<button type="button" class="btn btn-danger btn-sm" data-action="stop"${dis}>Stop</button>`);
      parts.push(`<button type="button" class="btn btn-sm restart" data-action="restart"${dis}>Restart</button>`);
      return `<span class="btn-group">${parts.join('')}</span>`;
    }

    let opencodeOpening = false;
    let ocHideTimer = null;

    function setOpencodeProgress(title, text, opts = {}) {
      const box = document.getElementById('oc-progress');
      if (!box) return;
      if (ocHideTimer) { clearTimeout(ocHideTimer); ocHideTimer = null; }
      box.classList.remove('hidden', 'done', 'error');
      if (opts.error) box.classList.add('error');
      if (opts.done) box.classList.add('done');
      document.getElementById('oc-progress-title').textContent = title;
      document.getElementById('oc-progress-text').textContent = text || '';
      const actions = document.getElementById('oc-progress-actions');
      actions.replaceChildren();
      if (opts.action) {
        const a = document.createElement('a');
        a.className = 'btn btn-primary btn-sm';
        a.href = opts.action.href;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = opts.action.label;
        actions.appendChild(a);
      }
      if (!opts.busy && !opts.persist) {
        ocHideTimer = setTimeout(() => box.classList.add('hidden'), 6000);
      }
    }

    function hideOpencodeStatus() {
      if (ocHideTimer) { clearTimeout(ocHideTimer); ocHideTimer = null; }
      document.getElementById('oc-progress').classList.add('hidden');
    }

    async function openOpencodeProject(name, sameTab = false) {
      if (busy || opencodeOpening) return;
      opencodeOpening = true;
      const projectUrl = '/api/opencode/open/' + encodeURIComponent(name);
      setOpencodeProgress('OpenCode', 'Starting OpenCode…', { busy: true });
      try {
        let res = await apiFetch(projectUrl, { method: 'POST' });
        let data = await res.json();
        if (data.status === 'install_required') {
          const installUrl = '/api/opencode/install';
          let installation = await (await apiFetch(installUrl)).json();
          if (installation.status !== 'installing' && installation.status !== 'ready') {
            const target = data.version ? `OpenCode ${data.version}` : 'OpenCode';
            if (!confirm(`Install ${target} and build its web interface on this server? This can take several minutes. Existing chats and settings are kept.`)) {
              hideOpencodeStatus();
              return;
            }
            const start = await apiFetch(installUrl, { method: 'POST' });
            installation = await start.json();
            if (!start.ok) throw new Error(installation.error || 'Could not start installation.');
          }
          const started = Date.now();
          let shown = -1;
          while (installation.status === 'installing') {
            const secs = Math.round((Date.now() - started) / 1000);
            if (secs !== shown && secs % 2 === 0) {
              shown = secs;
              setOpencodeProgress('Installing OpenCode', `Downloading and building on this server… ${secs}s elapsed. This can take several minutes.`, { busy: true });
            }
            await new Promise(resolve => setTimeout(resolve, 2500));
            installation = await (await apiFetch(installUrl)).json();
          }
          if (installation.status !== 'ready') {
            throw new Error(installation.error || 'Installation stopped. Click OpenCode to try again.');
          }
          setOpencodeProgress('OpenCode', 'Installation complete. Starting OpenCode…', { busy: true });
          res = await apiFetch(projectUrl, { method: 'POST' });
          data = await res.json();
          sameTab = true;
        }
        if (!res.ok) throw new Error(data.error || 'OpenCode failed');
        if (sameTab) {
          setOpencodeProgress('OpenCode ready', 'Opening the project…', { busy: true, persist: true });
          location.assign(data.opencode_path);
          return;
        }
        setOpencodeProgress('OpenCode ready', 'Opened in a new tab. Use the link below if it was blocked.', {
          busy: false,
          persist: true,
          action: { label: 'Open project', href: data.opencode_path },
        });
        window.open(data.opencode_path, '_blank', 'noopener');
        lastFoldersJson = '';
        await refreshAll();
      } catch (e) {
        if (e.message !== 'unauthorized') {
          setOpencodeProgress('OpenCode failed', e.message || 'OpenCode failed', {
            busy: false,
            error: true,
            persist: true,
          });
        }
      } finally {
        opencodeOpening = false;
      }
    }

    function filteredFolders(folders) {
      const q = projectSearch.trim().toLowerCase();
      let out = folders.filter((f) => {
        if (q && !(`${f.name} ${f.path}`.toLowerCase().includes(q))) return false;
        if (projectFilter === 'running') return Boolean(f.running);
        if (projectFilter === 'idle') return !f.running;
        if (projectFilter === 'starting') return Boolean(f.running && !f.ready);
        return true;
      });
      const activity = (f) => Number(f.touched_at || f.mtime || 0);
      if (projectSort === 'name') out.sort((a, b) => a.name.localeCompare(b.name));
      else if (projectSort === 'status') out.sort((a, b) => Number(b.running) - Number(a.running) || activity(b) - activity(a));
      else out.sort((a, b) => activity(b) - activity(a));
      return out;
    }

    function renderFolders(folders) {
      const all = Array.isArray(folders) ? folders : [];
      const visible = filteredFolders(all);
      if (!all.length) {
        list.innerHTML = '<div class="empty-state">No project folders found under the scan root.</div>';
        lastFoldersJson = '[]';
        return;
      }
      if (!visible.length) {
        list.innerHTML = '<div class="empty-state">No projects match the current search or filter.</div>';
        return;
      }
      const json = JSON.stringify(visible.map(f => ({ n: f.name, r: f.running, rd: f.ready, t: f.touched_relative })));
      if (json === lastFoldersJson && list.querySelector('.projects-table')) return;
      lastFoldersJson = json;
      const rows = visible.map(f => {
        const running = f.running ? ' running' : '';
        const badge = `<span class="badge ${f.running ? 'run' : ''}">${f.running ? 'on' : 'off'}</span>`;
        const statusHint = f.running && f.ready ? ' · connected' : (f.running ? ' · starting…' : '');
        const gitBadge = f.git_has_remote ? '' : '<span class="badge warn" title="No git remote — Cursor may show this machine as disabled">no git remote</span>';
        return `<tr class="project-row${running}" data-name="${esc(f.name)}" data-path="${esc(f.path)}">
          <td><strong>${esc(f.name)}</strong> ${gitBadge}<div class="project-path">${esc(f.path)}</div></td>
          <td>${esc(f.touched_relative || f.mtime_relative || '—')}</td>
          <td>${badge}<span class="sub">${statusHint}</span></td>
          <td>${renderProjectActions(f)}</td>
        </tr>`;
      }).join('');
      list.innerHTML = `<div class="projects-section"><table class="projects-table"><thead><tr><th>Project</th><th>Last activity</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }

    function bindProjectFilters() {
      const search = document.getElementById('project-search');
      const filter = document.getElementById('project-filter');
      const sort = document.getElementById('project-sort');
      let timer = null;
      search?.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          projectSearch = search.value;
          lastFoldersJson = '';
          refreshAll();
        }, 150);
      });
      filter?.addEventListener('change', () => { projectFilter = filter.value; lastFoldersJson = ''; refreshAll(); });
      sort?.addEventListener('change', () => { projectSort = sort.value; lastFoldersJson = ''; refreshAll(); });
      document.getElementById('chart-range')?.addEventListener('change', (e) => { chartRange = e.target.value; refreshAll(); });
    }

    let pendingAgentUrl = null;

    function showStartLog(text) {
      const el = document.getElementById('start-modal-log');
      if (!text) {
        el.classList.add('hidden');
        el.textContent = '';
        return;
      }
      el.classList.remove('hidden');
      el.textContent = text;
      el.scrollTop = el.scrollHeight;
    }

    function formatLogErrors(errors) {
      return (errors || []).join('\n');
    }

    function openCursorFromModal() {
      if (!pendingAgentUrl) return;
      window.location.href = pendingAgentUrl;
    }

    function setStartStep(step, state) {
      document.querySelectorAll('#start-steps li').forEach(li => {
        li.classList.remove('active', 'done', 'error');
        const key = li.dataset.step;
        const order = ['request', 'launch', 'connect', 'redirect'];
        const si = order.indexOf(step);
        const ki = order.indexOf(key);
        if (state === 'error' && key === step) li.classList.add('error');
        else if (ki < si) li.classList.add('done');
        else if (ki === si) li.classList.add(state === 'done' ? 'done' : 'active');
      });
    }

    function openStartModal(name) {
      modalOpen = true;
      pendingAgentUrl = null;
      document.getElementById('start-modal-title').textContent = 'Starting ' + name;
      document.getElementById('start-modal-msg').textContent = '';
      document.getElementById('start-modal-close').classList.add('hidden');
      document.getElementById('start-modal-open').classList.add('hidden');
      showStartLog('');
      document.getElementById('start-modal').classList.remove('hidden');
      setStartStep('request', 'active');
    }

    function closeStartModal() {
      document.getElementById('start-modal').classList.add('hidden');
      document.getElementById('start-modal-open').classList.add('hidden');
      pendingAgentUrl = null;
      modalOpen = false;
      busy = false;
      lastFoldersJson = '';
      refreshAll();
    }

    function openCtxModal(name) {
      ctxProjectName = name;
      modalOpen = true;
      document.getElementById('ctx-modal-title').textContent = name;
      document.getElementById('ctx-modal-hint').textContent = 'Creates .agentcontrol-ignore in the project folder. Delete that file to restore.';
      document.getElementById('ctx-modal').classList.remove('hidden');
    }

    function closeCtxModal() {
      document.getElementById('ctx-modal').classList.add('hidden');
      modalOpen = false;
      ctxProjectName = null;
    }

    function removeProjectRow(name) {
      const row = [...list.querySelectorAll('.project-row')].find(r => r.dataset.name === name);
      if (row) {
        row.classList.add('removing');
        setTimeout(() => {
          row.remove();
          if (!list.querySelector('.project-row')) {
            list.innerHTML = '<p class="loading">No folders found</p>';
          }
        }, 150);
      }
      lastFoldersJson = '';
    }

    function pill(label, ok, warn) {
      const cls = ok ? 'ok' : (warn ? 'warn' : 'bad');
      const dot = ok ? '●' : (warn ? '◐' : '○');
      return `<span class="xray-pill ${cls}">${dot} ${esc(label)}</span>`;
    }

    async function loadSettingsPanel() {
      try {
        const [settingsRes, modelsRes] = await Promise.all([
          apiFetch('/api/settings'),
          apiFetch('/api/models'),
        ]);
        const settings = await settingsRes.json();
        const select = document.getElementById('settings-model');
        const models = (await modelsRes.json()).models || [];
        const current = select.value;
        select.innerHTML = '<option value="">— Cursor default —</option>' + models.map(m =>
          `<option value="${esc(m.id)}">${esc(m.label)} (${esc(m.id)})</option>`
        ).join('');
        select.value = settings.default_model || current || '';
        document.getElementById('settings-model-status').textContent = settings.default_model
          ? 'Default model: ' + settings.default_model
          : 'No default model — Cursor will use its own default.';
        const statusEl = document.getElementById('settings-api-status');
        if (statusEl) {
          statusEl.textContent = settings.api_key_set
            ? 'API key is configured on this server.'
            : 'No API key saved yet — workers cannot start.';
          statusEl.className = 'api-key-hint' + (settings.api_key_set ? ' ok' : '');
        }
        const bridge = settings.cursor_bridge || {};
        document.getElementById('bridge-enabled').checked = !!bridge.enabled;
        document.getElementById('bridge-edge-ip').value = bridge.edge_ip || '';
        document.getElementById('bridge-port').value = bridge.port || 33443;
        document.getElementById('bridge-tls-host').value = bridge.tls_host || 'api2.cursor.sh';
        document.getElementById('bridge-endpoint').value = bridge.endpoint || '';
        const bStatus = document.getElementById('bridge-settings-status');
        if (bridge.enabled && bridge.endpoint) {
          bStatus.textContent = 'Active endpoint: ' + bridge.endpoint + (bridge.hosts_configured ? ' · /etc/hosts OK' : ' · hosts pending');
          bStatus.className = 'api-key-hint ok';
        } else {
          bStatus.textContent = 'Direct api2 (no edge bridge)';
          bStatus.className = 'api-key-hint';
        }
        const totp = settings.totp || {};
        const totpEl = document.getElementById('totp-settings-status');
        if (totpEl) {
          totpEl.textContent = totp.enabled
            ? 'Authenticator enabled — required at sign-in.'
            : (totp.pending
              ? 'Setup started — click Set up authenticator to show the QR again.'
              : (totp.configured ? 'Secret saved but not enabled yet.' : 'Authenticator off.'));
          totpEl.className = 'api-key-hint' + (totp.enabled ? ' ok' : '');
        }
        const oc = settings.opencode || {};
        const ocSecrets = oc.secrets || {};
        const ocEl = document.getElementById('opencode-settings-status');
        if (ocEl) {
          const bits = [
            oc.installed
              ? `CLI ${oc.version || 'installed'}${oc.pinned_version && oc.version !== oc.pinned_version ? ` (target ${oc.pinned_version})` : ''}`
              : 'CLI missing',
            oc.install_required ? 'install required' : '',
            oc.healthy ? 'serve healthy' : 'serve down',
            ocSecrets.opencode_go_set ? 'Go key set' : 'Go key —',
            ocSecrets.anthropic_set ? 'Anthropic set' : '',
            ocSecrets.openai_set ? 'OpenAI set' : '',
          ].filter(Boolean).join(' · ');
          ocEl.textContent = bits;
          ocEl.className = 'api-key-hint' + (oc.healthy ? ' ok' : '');
        }
        if (ocSecrets.default_model) {
          document.getElementById('oc-default-model').placeholder = 'Current: ' + ocSecrets.default_model;
        }
        if (ocSecrets.remote_public_url) {
          document.getElementById('oc-remote-url').value = ocSecrets.remote_public_url;
        }
        await refreshSessionsStatus();
        await refreshAgentStatus();
        await checkOpencodeUpdate();
      } catch (e) {}
    }

    async function saveOpencodeSettings() {
      const msgEl = document.getElementById('opencode-save-msg');
      msgEl.textContent = 'Saving…';
      const body = {
        anthropic_api_key: document.getElementById('oc-anthropic-key').value.trim(),
        openai_api_key: document.getElementById('oc-openai-key').value.trim(),
        opencode_go_key: document.getElementById('oc-go-key').value.trim(),
        default_model: document.getElementById('oc-default-model').value.trim(),
        remote_public_url: document.getElementById('oc-remote-url').value.trim(),
        restart_serve: true,
      };
      try {
        const res = await apiFetch('/api/settings/opencode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        msgEl.textContent = res.ok ? 'Saved — restarting shared serve if needed' : (data.error || 'Failed');
        msgEl.style.color = res.ok ? 'var(--green)' : 'var(--red)';
        if (res.ok) {
          document.getElementById('oc-anthropic-key').value = '';
          document.getElementById('oc-openai-key').value = '';
          document.getElementById('oc-go-key').value = '';
        }
        await loadSettingsPanel();
      } catch (e) { msgEl.textContent = 'Failed'; }
    }

    async function refreshOpencodeRemote() {
      const msgEl = document.getElementById('opencode-save-msg');
      try {
        const res = await apiFetch('/api/settings/opencode/remote/refresh', { method: 'POST' });
        const data = await res.json();
        msgEl.textContent = res.ok
          ? (data.remote_public_url ? 'Remote URL: ' + data.remote_public_url : 'No remote URL returned — use /oc/ on this panel')
          : (data.error || 'Failed');
      } catch (e) { msgEl.textContent = 'Failed'; }
    }

    function showTotpSetup(data) {
      const setup = document.getElementById('totp-setup');
      const img = document.getElementById('totp-qr');
      const msg = document.getElementById('totp-setup-msg');
      setup.style.display = 'flex';
      msg.textContent = '';
      document.getElementById('totp-secret-preview').value = data.secret || '';
      if (data.qr_data_uri) {
        img.src = data.qr_data_uri;
        img.style.display = 'block';
      } else {
        img.style.display = 'none';
      }
      const el = document.getElementById('totp-settings-status');
      el.textContent = data.qr_data_uri
        ? 'Scan the QR code, then enter the 6-digit code from your app.'
        : 'QR rendering unavailable — add the secret manually to your app, then enter the 6-digit code.';
      el.className = 'api-key-hint';
      document.getElementById('totp-enable-code').focus();
    }

    async function startTotpSetup(fresh) {
      const el = document.getElementById('totp-settings-status');
      const res = await apiFetch(fresh ? '/api/settings/totp/rotate' : '/api/settings/totp/begin', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        el.textContent = data.error || 'Could not create a secret';
        el.className = 'api-key-hint';
        return;
      }
      showTotpSetup(data);
    }

    // Kept for older markup or bookmarks.
    async function rotateTotpSecret() { return startTotpSetup(true); }

    async function enableTotpFromSettings() {
      const codeEl = document.getElementById('totp-enable-code');
      const code = codeEl.value.trim();
      const msg = document.getElementById('totp-setup-msg');
      if (!/^\d{6}$/.test(code)) {
        msg.textContent = 'Enter the 6-digit code from your authenticator app.';
        return;
      }
      msg.textContent = '';
      const res = await apiFetch('/api/settings/totp/enable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        msg.textContent = data.error || 'Enable failed';
        return;
      }
      if (data.reauthentication_required) { location.replace('/'); return; }
      document.getElementById('totp-setup').style.display = 'none';
      codeEl.value = '';
      document.getElementById('totp-settings-status').textContent = 'Authenticator enabled — required at sign-in.';
      document.getElementById('totp-settings-status').className = 'api-key-hint ok';
    }

    async function disableTotp() {
      const res = await apiFetch('/api/settings/totp/disable', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (data.reauthentication_required) { location.replace('/'); return; }
      await loadSettingsPanel();
    }

    async function revokeAllSessions() {
      if (!confirm('Sign out every browser session on this server? You will need the password (and 2FA) again.')) return;
      const res = await apiFetch('/api/auth/sessions/revoke-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { alert(data.error || 'Failed'); return; }
      location.replace('/');
    }

    async function signOutCurrent() {
      await apiFetch('/api/auth/logout', { method: 'POST' }).catch(() => {});
      location.replace('/');
    }

    async function saveBridgeSettings() {
      const status = document.getElementById('bridge-settings-status');
      const payload = {
        enabled: document.getElementById('bridge-enabled').checked,
        edge_ip: document.getElementById('bridge-edge-ip').value.trim(),
        port: parseInt(document.getElementById('bridge-port').value, 10) || 33443,
        tls_host: document.getElementById('bridge-tls-host').value.trim() || 'api2.cursor.sh',
        endpoint: document.getElementById('bridge-endpoint').value.trim(),
      };
      try {
        const res = await apiFetch('/api/settings/bridge', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) { status.textContent = data.error || 'Save failed'; status.className = 'api-key-hint'; return; }
        status.textContent = 'Saved — endpoint: ' + (data.endpoint || data.cursor_bridge?.endpoint || '—');
        status.className = 'api-key-hint ok';
        await loadSettingsPanel();
      } catch (e) {
        status.textContent = 'Save failed';
      }
    }

    async function testBridgeConnection() {
      const status = document.getElementById('bridge-settings-status');
      const logEl = document.getElementById('bridge-test-log');
      status.textContent = 'Testing edge bridge…';
      logEl.classList.remove('hidden');
      logEl.textContent = '';
      try {
        const res = await apiFetch('/api/settings/bridge/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const data = await res.json();
        logEl.textContent = JSON.stringify(data, null, 2);
        if (data.ok) {
          status.textContent = 'OK — HTTP ' + data.http_status + (data.worker_auth_ok ? ' · worker auth OK' : '');
          status.className = 'api-key-hint ok';
        } else {
          status.textContent = data.error || 'Test failed';
          status.className = 'api-key-hint';
        }
      } catch (e) {
        status.textContent = 'Test failed';
      }
    }

    async function saveDefaultModel() {
      const model = document.getElementById('settings-model').value;
      const status = document.getElementById('settings-model-status');
      try {
        const res = await apiFetch('/api/settings/model', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const data = await res.json();
        if (!res.ok) { status.textContent = data.error || 'Failed'; return; }
        status.textContent = model ? 'Default model: ' + model : 'Cleared — using Cursor default.';
        status.className = 'api-key-hint ok';
      } catch (e) {
        status.textContent = 'Failed to save model';
      }
    }

    async function hideProject(name) {
      closeCtxModal();
      removeProjectRow(name);
      try {
        const res = await apiFetch('/api/hide/' + encodeURIComponent(name), { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
          msg.textContent = data.error || 'Hide failed';
          msg.className = 'error';
          lastFoldersJson = '';
          const foldRes = await apiFetch('/api/folders');
          renderFolders((await foldRes.json()).folders);
          return;
        }
        msg.textContent = 'Hidden — delete .agentcontrol-ignore to restore';
        msg.className = '';
      } catch (e) {
        msg.textContent = 'Hide failed';
        msg.className = 'error';
        lastFoldersJson = '';
        try {
          const foldRes = await apiFetch('/api/folders');
          renderFolders((await foldRes.json()).folders);
        } catch (_) {}
      }
    }

    function bindProjectList() {
      if (list.dataset.bound) return;
      list.dataset.bound = '1';

      list.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        const row = btn.closest('.project-row');
        const name = row?.dataset.name;
        if (!name || busy) return;
        if (btn.dataset.action === 'start') startAgent(name);
        if (btn.dataset.action === 'stop') stopAgent(name);
        if (btn.dataset.action === 'restart') restartAgent(name);
        if (btn.dataset.action === 'opencode') openOpencodeProject(name);
        if (btn.dataset.action === 'logs') openLogsModal(name);
        if (btn.dataset.action === 'details') openDetailsModal(name);
      });

      list.addEventListener('contextmenu', (e) => {
        const row = e.target.closest('.project-row');
        if (!row) return;
        e.preventDefault();
        openCtxModal(row.dataset.name);
      });

      const clearPress = () => {
        if (longPressTimer) clearTimeout(longPressTimer);
        longPressTimer = null;
        if (longPressRow) longPressRow.classList.remove('pressing');
        longPressRow = null;
      };

      list.addEventListener('touchstart', (e) => {
        const row = e.target.closest('.project-row');
        if (!row || e.target.closest('[data-action]') || e.target.closest('.btn')) return;
        clearPress();
        longPressRow = row;
        row.classList.add('pressing');
        longPressTimer = setTimeout(() => {
          if (navigator.vibrate) navigator.vibrate(20);
          openCtxModal(row.dataset.name);
          clearPress();
        }, 550);
      }, { passive: true });

      list.addEventListener('touchend', clearPress);
      list.addEventListener('touchmove', clearPress);
      list.addEventListener('touchcancel', clearPress);
    }

    document.getElementById('ctx-hide-btn').addEventListener('click', () => {
      if (ctxProjectName) hideProject(ctxProjectName);
    });

    document.getElementById('server-detail-grid').addEventListener('click', (e) => {
      const head = e.target.closest('[data-docker-toggle]');
      if (!head) return;
      e.stopPropagation();
      toggleDockerGroup(head.dataset.dockerToggle);
    });

    async function refreshAll() {
      if (document.getElementById('app').classList.contains('hidden')) return;
      const skipFolders = busy || modalOpen;
      try {
        const reqs = [apiFetch('/api/system'), apiFetch('/api/system/history?range=' + encodeURIComponent(chartRange))];
        if (!skipFolders) reqs.push(apiFetch('/api/folders'));
        const results = await Promise.all(reqs);
        const sys = await results[0].json();
        const hist = await results[1].json();
        history = hist.points || [];
        renderSystem(sys);
        updatePageTitle(sys);
        if (!skipFolders && results[2]) {
          renderFolders((await results[2].json()).folders);
        }
        refreshPoolStatus();
        setConnection(true);
        msg.textContent = '';
      } catch (e) {
        if (e.message === 'unauthorized') return;
        if (e.message === 'setup') return;
        setConnection(false);
        if (!list.querySelector('.projects-table')) {
          list.innerHTML = '<div class="error-state">Could not load projects. Check the server and retry.<br><button class="btn btn-ghost btn-sm" type="button" data-action="refreshAll" style="margin-top:10px">Retry</button></div>';
        }
        if (!modalOpen) {
          msg.textContent = 'Refresh failed: ' + (e.message || 'unknown');
          msg.className = 'error';
        }
      }
    }

    async function saveApiKey() {
      const key = document.getElementById('settings-api-key').value.trim();
      const msgEl = document.getElementById('settings-msg');
      if (!key) { msgEl.textContent = 'Enter an API key'; return; }
      try {
        const res = await apiFetch('/api/setup/api-key', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key }),
        });
        const data = await res.json();
        msgEl.textContent = res.ok ? 'API key saved and verified' : (data.error || 'Failed');
        msgEl.style.color = res.ok ? 'var(--green)' : 'var(--red)';
        if (!res.ok && data.detail) {
          msgEl.textContent += ' — see worker log for details';
        }
        if (res.ok) document.getElementById('settings-api-key').value = '';
        refreshAll();
      } catch (e) { msgEl.textContent = 'Failed'; }
    }

    async function waitReady(name, timeoutMs = 60000) {
      const start = Date.now();
      const msgEl = document.getElementById('start-modal-msg');
      while (Date.now() - start < timeoutMs) {
        const res = await apiFetch('/api/ready/' + encodeURIComponent(name));
        const data = await res.json();
        if (!data.running) throw new Error('stopped');
        if (data.log_tail) showStartLog(data.log_tail);
        if (data.has_errors && !data.ready) {
          const errText = formatLogErrors(data.log_errors) || 'Worker reported errors in log';
          throw new Error(errText);
        }
        if (data.ready) return data;
        if (data.readyz && data.readyz.connected) return data;
        msgEl.textContent = 'Waiting for Cursor connection…';
        await new Promise(r => setTimeout(r, 1500));
      }
      throw new Error('timeout');
    }

    async function startAgent(name) {
      if (busy) return;
      busy = true;
      try { await apiFetch('/api/projects/touch/' + encodeURIComponent(name), { method: 'POST' }); } catch (_) {}
      openStartModal(name);
      const msgEl = document.getElementById('start-modal-msg');
      try {
        setStartStep('request', 'active');
        const res = await apiFetch('/api/start/' + encodeURIComponent(name), { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
          setStartStep('request', 'error');
          msgEl.textContent = data.error || 'Start failed';
          document.getElementById('start-modal-close').classList.remove('hidden');
          return;
        }
        setStartStep('request', 'done');
        setStartStep('launch', 'done');
        setStartStep('connect', 'active');
        msgEl.textContent = 'Waiting for Cursor connection…';

        const ready = await waitReady(name);
        setStartStep('connect', 'done');
        setStartStep('redirect', 'active');
        pendingAgentUrl = ready.agent_url || data.agent_url;
        msgEl.textContent = 'Worker connected — opening Cursor…';
        setStartStep('redirect', 'done');
        document.getElementById('start-modal-close').classList.remove('hidden');
        if (pendingAgentUrl) {
          setTimeout(() => { window.location.href = pendingAgentUrl; }, 600);
        }
      } catch (e) {
        if (e.message === 'stopped') {
          setStartStep('connect', 'error');
          msgEl.textContent = 'Worker stopped unexpectedly.';
        } else if (e.message === 'timeout') {
          setStartStep('connect', 'error');
          msgEl.textContent = 'Timed out — check worker logs below. Fix the issue before opening Cursor.';
        } else if (e.message !== 'unauthorized') {
          setStartStep('connect', 'error');
          const lines = (e.message || '').split('\n').filter(Boolean);
          if (lines.length > 1) {
            msgEl.textContent = 'Worker error — fix before opening Cursor:';
            showStartLog(lines.join('\n'));
          } else {
            msgEl.textContent = 'Connection failed: ' + (e.message || 'unknown');
          }
        }
        document.getElementById('start-modal-close').classList.remove('hidden');
      } finally {
        if (!document.getElementById('start-modal').classList.contains('hidden')) {
          // keep busy until redirect or close
        } else {
          busy = false;
        }
      }
    }

    async function stopAgent(name) {
      if (busy) return;
      busy = true;
      msg.textContent = 'Stopping…';
      try {
        const res = await apiFetch('/api/stop/' + encodeURIComponent(name), { method: 'POST' });
        if (res.ok) {
          msg.textContent = 'Stopped';
          lastFoldersJson = '';
        }
        await refreshAll();
      } catch (e) {} finally {
        busy = false;
      }
    }

    async function restartAgent(name) {
      if (busy) return;
      busy = true;
      try { await apiFetch('/api/projects/touch/' + encodeURIComponent(name), { method: 'POST' }); } catch (_) {}
      openStartModal(name);
      document.getElementById('start-modal-title').textContent = 'Restarting ' + name;
      const msgEl = document.getElementById('start-modal-msg');
      try {
        setStartStep('request', 'active');
        const res = await apiFetch('/api/restart/' + encodeURIComponent(name), { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
          setStartStep('request', 'error');
          msgEl.textContent = data.error || 'Restart failed';
          document.getElementById('start-modal-close').classList.remove('hidden');
          return;
        }
        setStartStep('request', 'done');
        setStartStep('launch', 'done');
        setStartStep('connect', 'active');
        msgEl.textContent = 'Waiting for Cursor connection…';
        const ready = await waitReady(name);
        setStartStep('connect', 'done');
        setStartStep('redirect', 'active');
        pendingAgentUrl = ready.agent_url || data.agent_url;
        msgEl.textContent = 'Worker connected — opening Cursor…';
        setStartStep('redirect', 'done');
        document.getElementById('start-modal-close').classList.remove('hidden');
        if (pendingAgentUrl) {
          setTimeout(() => { window.location.href = pendingAgentUrl; }, 600);
        }
      } catch (e) {
        setStartStep('connect', 'error');
        msgEl.textContent = e.message || 'Restart failed';
        document.getElementById('start-modal-close').classList.remove('hidden');
      } finally {
        if (document.getElementById('start-modal').classList.contains('hidden')) busy = false;
      }
    }

    function bindModalBackdrops() {
      const closers = {
        'metrics-modal': closeMetricsModal,
        'settings-modal': closeSettingsModal,
        'proxy-modal': closeProxyModal,
        'start-modal': closeStartModal,
        'ctx-modal': closeCtxModal,
        'logs-modal': closeLogsModal,
        'details-modal': closeDetailsModal,
        'audit-modal': closeAuditModal,
        'tokens-modal': closeTokensModal,
        'notifications-modal': closeNotificationsModal,
        'hidden-modal': closeHiddenModal,
        'terminal-modal': closeTerminal,
      };
      document.querySelectorAll('.modal-overlay[data-modal-closable]').forEach((overlay) => {
        overlay.addEventListener('click', (e) => {
          if (e.target !== overlay) return;
          if (overlay.dataset.modalGuardBusy === '1' && busy) return;
          closers[overlay.id]?.();
        });
      });
    }

    // ---- logs viewer -------------------------------------------------
    let logsTimer = null;
    let logsProject = '';

    async function openLogsModal(name) {
      logsProject = name;
      modalOpen = true;
      document.getElementById('logs-title').textContent = 'Logs · ' + name;
      const search = document.getElementById('logs-search');
      search.value = '';
      if (!search.dataset.bound) {
        search.dataset.bound = '1';
        let timer = null;
        search.addEventListener('input', () => {
          clearTimeout(timer);
          timer = setTimeout(refreshLogs, 200);
        });
      }
      document.getElementById('logs-download').href = '/api/logs/' + encodeURIComponent(name) + '/download';
      document.getElementById('logs-modal').classList.remove('hidden');
      await refreshLogs();
      clearInterval(logsTimer);
      logsTimer = setInterval(refreshLogs, 3000);
    }

    async function refreshLogs() {
      if (!logsProject || document.getElementById('logs-modal').classList.contains('hidden')) return;
      const query = document.getElementById('logs-search').value.trim();
      const view = document.getElementById('logs-view');
      try {
        const res = await apiFetch(`/api/logs/${encodeURIComponent(logsProject)}?tail=800&q=${encodeURIComponent(query)}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'log read failed');
        const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
        view.textContent = (data.lines || []).join('\n') || (data.exists ? 'No matching lines.' : 'Log file not created yet.');
        if (atBottom) view.scrollTop = view.scrollHeight;
        document.getElementById('logs-meta').textContent =
          `${(data.size / 1024).toFixed(1)} KB · ${data.mtime ? new Date(data.mtime * 1000).toLocaleString() : '—'} · auto-refresh 3s`;
      } catch (e) {
        view.textContent = 'Could not read the log: ' + (e.message || 'unknown');
      }
    }

    function closeLogsModal() {
      clearInterval(logsTimer);
      logsTimer = null;
      logsProject = '';
      document.getElementById('logs-modal').classList.add('hidden');
      modalOpen = false;
      refreshAll();
    }

    // ---- project details --------------------------------------------
    let detailsProject = '';

    function fmtDuration(seconds) {
      if (seconds == null) return '—';
      const s = Math.max(0, Math.floor(seconds));
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
      return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
    }

    async function openDetailsModal(name) {
      detailsProject = name;
      modalOpen = true;
      document.getElementById('details-title').textContent = 'Project · ' + name;
      document.getElementById('details-body').textContent = 'Loading…';
      document.getElementById('details-modal').classList.remove('hidden');
      await refreshDetails();
    }

    async function refreshDetails() {
      if (!detailsProject) return;
      const body = document.getElementById('details-body');
      try {
        const res = await apiFetch('/api/projects/' + encodeURIComponent(detailsProject) + '/details');
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || 'failed');
        const git = d.git || {};
        const item = (label, value) => `<div class="details-item"><b>${esc(String(value ?? '—'))}</b><span>${esc(label)}</span></div>`;
        body.innerHTML = `
          <div class="details-grid">
            ${item('Status', d.running ? (d.ready ? 'running · connected' : 'running · starting') : 'idle')}
            ${item('Uptime', fmtDuration(d.uptime_seconds))}
            ${item('PID', d.pid || '—')}
            ${item('Worker ID', d.worker_id || '—')}
            ${item('Log size', d.log_size != null ? (d.log_size / 1024).toFixed(1) + ' KB' : '—')}
            ${item('Last modified', d.mtime ? new Date(d.mtime * 1000).toLocaleString() : '—')}
          </div>
          <div class="details-grid">
            ${item('Git branch', git.branch || '—')}
            ${item('HEAD', git.head || '—')}
            ${item('Dirty files', git.is_repo ? git.dirty_files : '—')}
            ${item('Ahead / behind', git.is_repo ? `${git.ahead ?? 0} / ${git.behind ?? 0}` : '—')}
            ${item('Last commit', git.last_commit || '—')}
            ${item('Remote', git.remote_url || 'none')}
          </div>
          <div class="settings-row">
            <a class="btn btn-primary btn-sm" href="${esc(d.agent_url)}" target="_blank" rel="noopener">Open in Cursor</a>
            <button class="btn btn-ghost btn-sm" type="button" data-action="detailsLogs">Logs</button>
            <button class="btn btn-ghost btn-sm" type="button" data-action="detailsOpencode">OpenCode</button>
          </div>`;
      } catch (e) {
        body.innerHTML = `<div class="error-state">${esc(e.message || 'Could not load details')}</div>`;
      }
    }

    function closeDetailsModal() {
      detailsProject = '';
      document.getElementById('details-modal').classList.add('hidden');
      modalOpen = false;
      refreshAll();
    }

    // ---- sessions ----------------------------------------------------
    async function refreshSessionsStatus() {
      try {
        const res = await apiFetch('/api/auth/sessions');
        const data = await res.json();
        const el = document.getElementById('sessions-status');
        if (el) el.textContent = `${data.count || 0} active session(s) · ${data.ttl_days || 0} day(s)`;
        const listEl = document.getElementById('sessions-list');
        if (listEl) {
          const rows = (data.sessions || []).map((s) => `
            <div class="session-row">
              <div>
                <div>${s.current ? '<strong>This device</strong>' : esc((s.ip || 'unknown') + ' · ' + (s.ua || 'unknown client').slice(0, 60))}</div>
                <div class="meta">last seen ${new Date(s.last_seen * 1000).toLocaleString()} · expires ${new Date(s.expires_at * 1000).toLocaleDateString()}</div>
              </div>
              ${s.current ? '' : `<button class="btn btn-ghost btn-sm" type="button" data-action="revokeSession" data-arg="${esc(s.id)}">Revoke</button>`}
            </div>`).join('');
          listEl.innerHTML = rows || '<div class="empty-state">No active sessions.</div>';
        }
      } catch (_) {}
    }

    async function revokeSession(id) {
      if (!id) return;
      const res = await apiFetch('/api/auth/sessions/' + encodeURIComponent(id) + '/revoke', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      toast(res.ok ? 'Session revoked' : (data.error || 'Revoke failed'), res.ok ? 'success' : 'error');
      refreshSessionsStatus();
    }

    // ---- notifications ----------------------------------------------
    const EVENT_LABELS = {
      worker_down: 'Worker stopped',
      worker_error: 'Worker errors',
      disk: 'Disk usage',
      cert: 'TLS certificate',
      update: 'Panel update',
      opencode: 'OpenCode',
      login_fail: 'Login lockout',
      backup: 'Backups',
      recovery: 'Recovery notices',
    };

    async function openNotificationsModal() {
      const res = await apiFetch('/api/notifications');
      const cfg = (await res.json()).config || {};
      document.getElementById('notify-enabled').checked = cfg.enabled !== false;
      document.getElementById('notify-severity').value = cfg.min_severity || 'warning';
      document.getElementById('notify-throttle').value = cfg.throttle_seconds || 900;
      document.getElementById('notify-events').innerHTML = Object.entries(EVENT_LABELS).map(([key, label]) => `
        <label class="settings-check"><input type="checkbox" data-event="${key}" ${cfg.events?.[key] !== false ? 'checked' : ''}> ${label}</label>`).join('');
      const ch = cfg.channels || {};
      const set = (id, value) => { const el = document.getElementById(id); if (el) el.value = value ?? ''; };
      document.getElementById('notify-telegram-enabled').checked = Boolean(ch.telegram?.enabled);
      set('notify-telegram-chat', ch.telegram?.chat_id);
      document.getElementById('notify-webhook-enabled').checked = Boolean(ch.webhook?.enabled);
      set('notify-webhook-url', ch.webhook?.url);
      document.getElementById('notify-email-enabled').checked = Boolean(ch.email?.enabled);
      set('notify-email-host', ch.email?.smtp_host);
      set('notify-email-port', ch.email?.smtp_port || 587);
      set('notify-email-user', ch.email?.username);
      set('notify-email-from', ch.email?.from);
      set('notify-email-to', Array.isArray(ch.email?.to) ? ch.email.to.join(', ') : (ch.email?.to || ''));
      document.getElementById('notify-ntfy-enabled').checked = Boolean(ch.ntfy?.enabled);
      set('notify-ntfy-url', ch.ntfy?.url || 'https://ntfy.sh');
      set('notify-ntfy-topic', ch.ntfy?.topic);
      document.getElementById('notify-msg').textContent = '';
      document.getElementById('notifications-modal').classList.remove('hidden');
    }

    function closeNotificationsModal() { document.getElementById('notifications-modal').classList.add('hidden'); }

    function notificationPayload() {
      const events = {};
      document.querySelectorAll('#notify-events [data-event]').forEach((el) => { events[el.dataset.event] = el.checked; });
      const val = (id) => document.getElementById(id)?.value?.trim() || '';
      return {
        enabled: document.getElementById('notify-enabled').checked,
        min_severity: document.getElementById('notify-severity').value,
        throttle_seconds: Number(document.getElementById('notify-throttle').value) || 900,
        events,
        channels: {
          telegram: { enabled: document.getElementById('notify-telegram-enabled').checked, bot_token: document.getElementById('notify-telegram-token').value, chat_id: val('notify-telegram-chat') },
          webhook: { enabled: document.getElementById('notify-webhook-enabled').checked, url: val('notify-webhook-url'), secret: document.getElementById('notify-webhook-secret').value },
          email: {
            enabled: document.getElementById('notify-email-enabled').checked,
            smtp_host: val('notify-email-host'),
            smtp_port: Number(val('notify-email-port')) || 587,
            username: val('notify-email-user'),
            password: document.getElementById('notify-email-pass').value,
            from: val('notify-email-from'),
            to: val('notify-email-to').split(',').map((x) => x.trim()).filter(Boolean),
          },
          ntfy: { enabled: document.getElementById('notify-ntfy-enabled').checked, url: val('notify-ntfy-url') || 'https://ntfy.sh', topic: val('notify-ntfy-topic') },
        },
      };
    }

    async function saveNotifications() {
      const msgEl = document.getElementById('notify-msg');
      const res = await apiFetch('/api/notifications', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(notificationPayload()),
      });
      const data = await res.json().catch(() => ({}));
      msgEl.textContent = res.ok ? 'Saved.' : (data.error || 'Save failed');
      msgEl.style.color = res.ok ? 'var(--green)' : 'var(--red)';
      if (res.ok) toast('Notification settings saved', 'success');
    }

    async function testNotifyChannel(channel) {
      const msgEl = document.getElementById('notify-msg');
      await saveNotifications();
      if (msgEl.textContent !== 'Saved.') return;
      const res = await apiFetch('/api/notifications/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel }),
      });
      const data = await res.json().catch(() => ({}));
      msgEl.textContent = data.ok ? `Test sent via ${channel}.` : (data.error || 'Test failed');
      msgEl.style.color = data.ok ? 'var(--green)' : 'var(--red)';
    }

    // ---- audit -------------------------------------------------------
    async function openAuditModal() {
      document.getElementById('audit-modal').classList.remove('hidden');
      await refreshAudit();
    }
    function closeAuditModal() { document.getElementById('audit-modal').classList.add('hidden'); }
    async function refreshAudit() {
      const el = document.getElementById('audit-list');
      el.textContent = 'Loading…';
      try {
        const res = await apiFetch('/api/audit?limit=200');
        const data = await res.json();
        const rows = (data.events || []).map((e) => `
          <div class="audit-row">
            <span class="muted">${new Date(e.ts * 1000).toLocaleString()}</span>
            <span>${esc(e.actor)} <span class="muted">(${esc(e.role)})</span></span>
            <span><strong>${esc(e.action)}</strong> ${e.target ? '· ' + esc(e.target) : ''} <span class="muted">${esc(e.ip || '')}</span></span>
          </div>`).join('');
        el.innerHTML = rows || '<div class="empty-state">No audit events yet.</div>';
      } catch (e) {
        el.innerHTML = `<div class="error-state">${esc(e.message || 'Could not load audit')}</div>`;
      }
    }

    // ---- tokens ------------------------------------------------------
    async function openTokensModal() {
      document.getElementById('tokens-modal').classList.remove('hidden');
      document.getElementById('token-new').classList.add('hidden');
      await refreshTokens();
    }
    function closeTokensModal() { document.getElementById('tokens-modal').classList.add('hidden'); document.getElementById('token-new').textContent = ''; }
    async function refreshTokens() {
      const el = document.getElementById('tokens-list');
      try {
        const res = await apiFetch('/api/tokens');
        const data = await res.json();
        const rows = (data.tokens || []).map((t) => `
          <div class="audit-row">
            <span class="muted">${new Date(t.created_at * 1000).toLocaleDateString()}</span>
            <span>${esc(t.name)} <span class="muted">${esc(t.role)}</span></span>
            <span>${t.revoked_at ? '<span class="badge bad">revoked</span>' : `<button class="btn btn-ghost btn-sm" type="button" data-action="revokeToken" data-arg="${esc(t.id)}">Revoke</button>`}</span>
          </div>`).join('');
        el.innerHTML = rows || '<div class="empty-state">No API tokens.</div>';
      } catch (e) {
        el.innerHTML = `<div class="error-state">${esc(e.message || 'Could not load tokens')}</div>`;
      }
    }
    async function createToken() {
      const name = document.getElementById('token-name').value.trim();
      const role = document.getElementById('token-role').value;
      if (!name) { toast('Token name is required', 'error'); return; }
      const res = await apiFetch('/api/tokens', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, role }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { toast(data.error || 'Create failed', 'error'); return; }
      const el = document.getElementById('token-new');
      el.textContent = `Copy now (shown once): ${data.token}`;
      el.classList.remove('hidden');
      document.getElementById('token-name').value = '';
      await refreshTokens();
    }
    async function revokeToken(id) {
      if (!confirm('Revoke this token? Clients using it will stop working.')) return;
      const res = await apiFetch('/api/tokens/' + encodeURIComponent(id) + '/revoke', { method: 'POST' });
      toast(res.ok ? 'Token revoked' : 'Revoke failed', res.ok ? 'success' : 'error');
      await refreshTokens();
    }

    // ---- hidden projects --------------------------------------------
    async function openHiddenModal() {
      document.getElementById('hidden-modal').classList.remove('hidden');
      const el = document.getElementById('hidden-list');
      try {
        const res = await apiFetch('/api/hidden');
        const data = await res.json();
        el.innerHTML = (data.hidden || []).map((h) => `
          <div class="session-row"><span>${esc(h.name)}</span>
          <button class="btn btn-ghost btn-sm" type="button" data-action="unhideProject" data-arg="${esc(h.name)}">Restore</button></div>`).join('')
          || '<div class="empty-state">No hidden projects.</div>';
      } catch (e) {
        el.innerHTML = `<div class="error-state">${esc(e.message || 'Could not load hidden projects')}</div>`;
      }
    }
    function closeHiddenModal() { document.getElementById('hidden-modal').classList.add('hidden'); }
    async function unhideProject(name) {
      const res = await apiFetch('/api/unhide/' + encodeURIComponent(name), { method: 'POST' });
      toast(res.ok ? 'Project restored' : 'Restore failed', res.ok ? 'success' : 'error');
      if (res.ok) { await openHiddenModal(); lastFoldersJson = ''; refreshAll(); }
    }

    // ---- terminal ----------------------------------------------------
    let terminalSocket = null;
    let terminalBuffer = '';

    function openTerminal() {
      if (!confirm('Open a shell as the panel service user on this host?')) return;
      const view = document.getElementById('terminal-view');
      view.textContent = 'Connecting…\n';
      view.focus();
      document.getElementById('terminal-modal').classList.remove('hidden');
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      terminalSocket = new WebSocket(`${scheme}://${location.host}/api/terminal/ws`);
      terminalSocket.binaryType = 'arraybuffer';
      terminalSocket.onopen = () => { view.textContent = ''; };
      terminalSocket.onmessage = (event) => {
        const text = typeof event.data === 'string' ? event.data : new TextDecoder().decode(event.data);
        terminalBuffer += text;
        if (terminalBuffer.length > 200000) terminalBuffer = terminalBuffer.slice(-150000);
        view.textContent = terminalBuffer;
        view.scrollTop = view.scrollHeight;
      };
      terminalSocket.onclose = () => {
        terminalBuffer += '\n[disconnected]\n';
        view.textContent = terminalBuffer;
        terminalSocket = null;
      };
      view.onkeydown = (event) => {
        if (!terminalSocket || terminalSocket.readyState !== WebSocket.OPEN) return;
        event.preventDefault();
        const map = { Enter: '\r', Backspace: '\x7f', Tab: '\t', Escape: '\x1b', ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D' };
        if (map[event.key]) { terminalSocket.send(map[event.key]); return; }
        if (event.ctrlKey && event.key.length === 1) {
          const code = event.key.toUpperCase().charCodeAt(0) - 64;
          terminalSocket.send(String.fromCharCode(code));
          return;
        }
        if (event.key.length === 1) terminalSocket.send(event.key);
      };
    }
    function closeTerminal() {
      if (terminalSocket) { try { terminalSocket.close(); } catch (_) {} terminalSocket = null; }
      document.getElementById('terminal-modal').classList.add('hidden');
    }

    // ---- OpenCode / agent updates ------------------------------------
    async function refreshAgentStatus() {
      const el = document.getElementById('agent-version-status');
      try {
        const res = await apiFetch('/api/agent/status');
        const data = await res.json();
        el.textContent = data.installed ? `agent ${data.version || '?'}` : 'agent CLI not installed';
        el.className = 'api-key-hint' + (data.installed ? ' ok' : '');
        const logEl = document.getElementById('agent-update-log');
        if (data.log_tail) { logEl.textContent = data.log_tail; logEl.classList.remove('hidden'); }
      } catch (_) {}
    }

    async function updateAgent() {
      if (!confirm('Download and install the latest Cursor agent CLI on this server?')) return;
      const res = await apiFetch('/api/agent/update', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      toast(res.ok ? 'Agent update started' : (data.error || 'Update failed'), res.ok ? 'success' : 'error');
      if (res.ok) setTimeout(refreshAgentStatus, 6000);
    }

    async function checkOpencodeUpdate() {
      const el = document.getElementById('opencode-version-status');
      el.textContent = 'Checking…';
      try {
        const res = await apiFetch('/api/opencode/versions?refresh=1');
        const data = await res.json();
        el.textContent = `installed ${data.installed || '—'} · web ${data.web || '—'} · pinned ${data.pinned} · latest ${data.latest || '?'}` +
          (data.patch_available ? '' : ' · no patch for latest');
        el.className = 'api-key-hint' + (data.update_available ? '' : ' ok');
      } catch (e) { el.textContent = 'Check failed: ' + e.message; }
    }

    async function updateOpencode() {
      const version = prompt('OpenCode version to install (blank = latest):', '');
      if (version === null) return;
      const res = await apiFetch('/api/opencode/update', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version }),
      });
      const data = await res.json().catch(() => ({}));
      toast(res.ok ? 'OpenCode update started (see opencode-update.log)' : (data.error || 'Update failed'), res.ok ? 'success' : 'error');
    }

    async function restartOpencode() {
      const res = await apiFetch('/api/opencode/restart', { method: 'POST' });
      toast(res.ok ? 'OpenCode serve restarted' : 'Restart failed', res.ok ? 'success' : 'error');
    }

    // ---- chart range -------------------------------------------------
    let chartRange = '1h';
    function setChartRange(range) {
      chartRange = range;
      refreshAll();
    }

    const ACTIONS = {
      addPoolLinks,
      addPoolSubscription,
      checkAndApplyUpdate: (on) => checkAndApplyUpdate(Boolean(on)),
      checkOpencodeUpdate,
      closeAuditModal,
      closeCtxModal,
      closeDetailsModal,
      closeHiddenModal,
      closeLogsModal,
      closeMetricsModal,
      closeNotificationsModal,
      closeProxyModal,
      closeSettingsModal,
      closeStartModal,
      closeTerminal,
      closeTokensModal,
      createToken,
      detailsLogs: () => { const name = detailsProject; closeDetailsModal(); openLogsModal(name); },
      detailsOpencode: () => { const name = detailsProject; closeDetailsModal(); openOpencodeProject(name); },
      disableTotp,
      enableTotpFromSettings,
      ensurePoolProxy,
      hideOpencodeStatus,
      openAuditModal,
      openCursorFromModal,
      openCursorUrl: (el) => openCursorUrl(el.dataset.url),
      openDetailsModal: () => { if (detailsProject) refreshDetails(); },
      openHiddenModal,
      openLogsModal: () => refreshLogs(),
      openMetricsModal,
      openNotificationsModal,
      openProxyModal,
      openSettingsModal,
      openTerminal,
      openTokensModal,
      refreshAll,
      refreshAgentStatus,
      refreshAudit,
      refreshDetails,
      refreshLogs,
      refreshOpencodeRemote,
      refreshPoolSubscriptions,
      refreshTokens,
      restartOpencode,
      revokeAllSessions,
      revokeSession,
      revokeToken,
      saveApiKey,
      saveBridgeSettings,
      saveDefaultModel,
      saveNotifications,
      saveOpencodeSettings,
      signOutCurrent,
      startTotpSetup: (on) => startTotpSetup(Boolean(on)),
      submitSetup,
      testBridgeConnection,
      testNotifyChannel,
      testPoolProxies,
      toggleServerDetails,
      unhideProject,
      updateAgent,
      updateOpencode,
    };

    document.addEventListener('click', (e) => {
      const el = e.target.closest('[data-action]');
      if (!el) return;
      if (['start', 'stop', 'restart', 'opencode'].includes(el.dataset.action)) return;
      const action = el.dataset.action;
      const fn = ACTIONS[action];
      if (!fn) return;
      e.preventDefault();
      if (action === 'openCursorUrl') {
        fn(el);
        return;
      }
      const arg = el.dataset.arg;
      if (arg === '1') fn(true);
      else if (arg === '0') fn(false);
      else if (arg !== undefined) fn(arg);
      else fn();
    });

    bootTheme();
    bindProjectFilters();
    bindProjectList();
    bindModalBackdrops();
    boot();
