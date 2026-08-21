// Smart Plex Queue Dashboard - UI logic only.
// Talks exclusively to the dashboard's own /api/* endpoints; never touches
// queue_listener.py's webhook/recommendation logic directly.

const STATUS_INTERVAL_MS = 3000;
const LOG_INTERVAL_MS = 2000;
const LOG_LINES = 300;

const el = {
    featureDot: document.getElementById('feature-dot'),
    featureValue: document.getElementById('feature-value'),
    listenerDot: document.getElementById('listener-dot'),
    listenerValue: document.getElementById('listener-value'),
    enableToggle: document.getElementById('enable-toggle'),
    restartBtn: document.getElementById('restart-btn'),
    restartIcon: document.getElementById('restart-icon'),
    restartLabel: document.getElementById('restart-label'),
    lastChecked: document.getElementById('last-checked'),
    portPill: document.getElementById('port-pill'),
    logPath: document.getElementById('log-path'),
    logConsole: document.getElementById('log-console'),
    autorefreshToggle: document.getElementById('autorefresh-toggle'),
    autoscrollToggle: document.getElementById('autoscroll-toggle'),
    clearLogBtn: document.getElementById('clear-log-btn'),
    toast: document.getElementById('toast'),
    toastMessage: document.getElementById('toast-message'),
    mediasageHostInput: document.getElementById('mediasage-host-input'),
    logFileInput: document.getElementById('log-file-input'),
    testConnectionBtn: document.getElementById('test-connection-btn'),
    saveConfigBtn: document.getElementById('save-config-btn'),
    configStatus: document.getElementById('config-status'),
    configStatusDot: document.getElementById('config-status-dot'),
    configStatusText: document.getElementById('config-status-text'),
};

let restarting = false;
let logTimer = null;
let statusTimer = null;
let toastTimer = null;

function showToast(message, kind) {
    el.toastMessage.textContent = message;
    el.toast.className = `toast ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        el.toast.classList.add('hidden');
    }, 3500);
}

async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
        throw new Error(`Request to ${path} failed (${res.status})`);
    }
    return res.json();
}

function setDot(node, state) {
    node.className = 'status-dot ' + state;
}

async function refreshStatus() {
    try {
        const data = await api('/api/status');

        el.portPill.textContent = `port ${data.port}`;
        el.logPath.textContent = data.log_file || '';
        el.lastChecked.textContent = `Last checked: ${data.checked_at}`;

        if (data.enabled) {
            setDot(el.featureDot, 'on');
            el.featureValue.textContent = 'Enabled';
            el.featureValue.className = 'status-value success-text';
        } else {
            setDot(el.featureDot, 'off');
            el.featureValue.textContent = 'Disabled';
            el.featureValue.className = 'status-value muted-text';
        }

        if (!restarting) {
            el.enableToggle.checked = data.enabled;
            el.enableToggle.disabled = false;
        }

        if (data.running) {
            setDot(el.listenerDot, restarting ? 'on pulse' : 'on');
            el.listenerValue.textContent = 'Running';
            el.listenerValue.className = 'status-value success-text';
        } else {
            setDot(el.listenerDot, restarting ? 'off pulse' : 'bad');
            el.listenerValue.textContent = 'Not running';
            el.listenerValue.className = restarting ? 'status-value muted-text' : 'status-value error-text';
        }
    } catch (err) {
        el.lastChecked.textContent = 'Last checked: connection error';
    }
}

function classifyLine(line) {
    if (/(^|\s)(❌|error|failed|traceback)/i.test(line)) return 'log-error';
    if (/(✅|succeeded|success)/i.test(line)) return 'log-success';
    if (/(⚠️|warn)/i.test(line)) return 'log-warn';
    if (/DEBUG/.test(line)) return 'log-debug';
    return 'log-info';
}

function escapeHtml(str) {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

async function refreshLogs() {
    if (!el.autorefreshToggle.checked) return;
    try {
        const data = await api(`/api/logs?lines=${LOG_LINES}`);
        const lines = data.lines || [];

        if (lines.length === 0) {
            el.logConsole.innerHTML = '<div class="log-empty">No log output yet.</div>';
            return;
        }

        const wasAtBottom = isScrolledToBottom(el.logConsole);

        el.logConsole.innerHTML = lines
            .map((line) => `<div class="log-line ${classifyLine(line)}">${escapeHtml(line)}</div>`)
            .join('');

        if (el.autoscrollToggle.checked && wasAtBottom) {
            scrollToBottom(el.logConsole);
        }
    } catch (err) {
        // Silently skip a failed poll; next interval will retry.
    }
}

function isScrolledToBottom(node) {
    return node.scrollHeight - node.scrollTop - node.clientHeight < 40;
}

function scrollToBottom(node) {
    node.scrollTop = node.scrollHeight;
}

async function handleToggle() {
    const wantEnabled = el.enableToggle.checked;
    el.enableToggle.disabled = true;
    try {
        const endpoint = wantEnabled ? '/api/enable' : '/api/disable';
        await api(endpoint, { method: 'POST' });
        showToast(wantEnabled ? 'Smart Queue enabled' : 'Smart Queue disabled', 'success');
    } catch (err) {
        showToast('Could not update feature toggle', 'error');
        el.enableToggle.checked = !wantEnabled;
    } finally {
        el.enableToggle.disabled = false;
        refreshStatus();
    }
}

async function handleRestart() {
    if (restarting) return;
    restarting = true;
    el.restartBtn.disabled = true;
    el.restartIcon.classList.add('spin');
    el.restartLabel.textContent = 'Restarting…';

    try {
        const data = await api('/api/restart', { method: 'POST' });
        showToast(data.message, data.ok ? 'success' : 'error');
    } catch (err) {
        showToast('Restart request failed', 'error');
    } finally {
        restarting = false;
        el.restartBtn.disabled = false;
        el.restartIcon.classList.remove('spin');
        el.restartLabel.textContent = 'Restart Listener';
        refreshStatus();
    }
}

function setConfigStatus(kind, text) {
    el.configStatus.classList.remove('hidden');
    el.configStatusDot.className = 'status-dot ' + kind;
    el.configStatusText.textContent = text;
}

async function loadConfig() {
    try {
        const data = await api('/api/config');
        el.mediasageHostInput.value = data.mediasage_host;
        el.mediasageHostInput.placeholder = data.mediasage_host_default;
        el.logFileInput.value = data.log_file_is_custom ? data.log_file : '';
        el.logFileInput.placeholder = data.log_file_default;
    } catch (err) {
        setConfigStatus('bad', 'Could not load current listener settings.');
    }
}

async function handleSaveConfig() {
    const host = el.mediasageHostInput.value.trim();
    const logFile = el.logFileInput.value.trim();
    if (!host) {
        setConfigStatus('bad', 'Enter a MediaSage host URL first.');
        return;
    }
    el.saveConfigBtn.disabled = true;
    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mediasage_host: host, log_file: logFile }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
            throw new Error(data.error || 'Save failed');
        }
        setConfigStatus('off', data.message);
        showToast('Listener settings saved', 'success');
    } catch (err) {
        setConfigStatus('bad', err.message);
        showToast('Could not save listener settings', 'error');
    } finally {
        el.saveConfigBtn.disabled = false;
    }
}

async function handleTestConnection() {
    const host = el.mediasageHostInput.value.trim();
    el.testConnectionBtn.disabled = true;
    setConfigStatus('pulse', 'Testing connection…');
    try {
        const res = await fetch('/api/config/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mediasage_host: host }),
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || `Request failed (${res.status})`);
        }
        if (data.ok) {
            const d = data.detail || {};
            const bits = [];
            if ('plex_connected' in d) bits.push(`Plex: ${d.plex_connected ? 'connected' : 'not connected'}`);
            if ('llm_configured' in d) bits.push(`LLM: ${d.llm_configured ? 'configured' : 'not configured'}`);
            setConfigStatus('on', `Reachable${bits.length ? ' — ' + bits.join(', ') : ''}`);
        } else {
            const reason = (data.detail && data.detail.error) || `HTTP ${data.status_code || 'error'}`;
            setConfigStatus('bad', `Unreachable — ${reason}`);
        }
    } catch (err) {
        setConfigStatus('bad', `Unreachable — ${err.message}`);
    } finally {
        el.testConnectionBtn.disabled = false;
    }
}

el.enableToggle.addEventListener('change', handleToggle);
el.saveConfigBtn.addEventListener('click', handleSaveConfig);
el.testConnectionBtn.addEventListener('click', handleTestConnection);
el.restartBtn.addEventListener('click', handleRestart);
el.clearLogBtn.addEventListener('click', () => {
    el.logConsole.innerHTML = '<div class="log-empty">View cleared. Waiting for new lines&hellip;</div>';
});
el.autorefreshToggle.addEventListener('change', () => {
    if (el.autorefreshToggle.checked) refreshLogs();
});

refreshStatus();
refreshLogs();
loadConfig();
statusTimer = setInterval(refreshStatus, STATUS_INTERVAL_MS);
logTimer = setInterval(refreshLogs, LOG_INTERVAL_MS);
