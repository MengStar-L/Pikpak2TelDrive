/**
 * Pikpak2TelDrive 前端应用逻辑
 */

// ============================================
// 全局状态
// ============================================
const state = {
    ws: null,
    tasks: {},
    currentPage: 'dashboard',
    currentFilter: 'all',
    reconnectTimer: null,
    reconnectAttempts: 0,
    heartbeatTimer: null,
    pollTimer: null
};

// ============================================
// 工具函数
// ============================================

function formatSpeed(bytesPerSec) {
    if (!bytesPerSec || bytesPerSec === 0) return '0 B/s';
    if (bytesPerSec < 1024) return bytesPerSec + ' B/s';
    if (bytesPerSec < 1048576) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
    if (bytesPerSec < 1073741824) return (bytesPerSec / 1048576).toFixed(1) + ' MB/s';
    return (bytesPerSec / 1073741824).toFixed(1) + ' GB/s';
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = 'toastOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function getStatusText(status) {
    const map = {
        'pending': '等待中',
        'downloading': '下载中',
        'uploading': '上传中',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消',
        'paused': '已暂停'
    };
    return map[status] || status;
}

// ============================================
// API 调用
// ============================================

async function apiCall(url, options = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });
        const data = await resp.json();
        if (!resp.ok) {
            throw new Error(data.detail || data.message || '请求失败');
        }
        return data;
    } catch (e) {
        if (e.name !== 'Error') {
            throw new Error('网络请求失败: ' + e.message);
        }
        throw e;
    }
}

// 全量刷新任务列表（WS 重连 & 兜底轮询共用）
async function fetchAllTasks() {
    try {
        const data = await apiCall('/api/tasks');
        if (data && data.tasks) {
            const serverIds = new Set(data.tasks.map(t => t.task_id));

            // 删除服务端已不存在的任务
            for (const id of Object.keys(state.tasks)) {
                if (!serverIds.has(id)) {
                    delete state.tasks[id];
                    const el = document.getElementById(`task-${id}`);
                    if (el) el.remove();
                }
            }

            // 更新所有任务
            data.tasks.forEach(t => {
                state.tasks[t.task_id] = t;
                renderTaskItem(t);
            });

            updateDashboard();
            checkEmptyState();
        }
    } catch (e) {
        // 静默失败
    }
}

async function addTask(url, filename, telDrivePath) {
    return apiCall('/api/task/add', {
        method: 'POST',
        body: JSON.stringify({ url, filename: filename || null, teldrive_path: telDrivePath || '/' })
    });
}

async function taskAction(taskId, action) {
    if (action === 'delete') {
        return apiCall(`/api/task/${taskId}`, { method: 'DELETE' });
    }
    return apiCall(`/api/task/${taskId}/${action}`, { method: 'POST' });
}

async function loadSettings() {
    return apiCall('/api/settings');
}

async function saveSettings(settings) {
    return apiCall('/api/settings', {
        method: 'PUT',
        body: JSON.stringify(settings)
    });
}

async function testAria2() {
    return apiCall('/api/settings/test/aria2', { method: 'POST' });
}

async function testTelDrive() {
    return apiCall('/api/settings/test/teldrive', { method: 'POST' });
}

// ============================================
// WebSocket
// ============================================

function connectWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    try {
        state.ws = new WebSocket(wsUrl);
    } catch (e) {
        scheduleReconnect();
        return;
    }

    state.ws.onopen = () => {
        state.reconnectAttempts = 0;
        updateWSStatus(true);
        // WS 重连后立即全量刷新，避免错过状态变更
        fetchAllTasks();
    };

    state.ws.onclose = () => {
        updateWSStatus(false);
        scheduleReconnect();
    };

    state.ws.onerror = () => {
        updateWSStatus(false);
    };

    state.ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleWSMessage(msg);
        } catch (e) {
            console.error('WS message parse error:', e);
        }
    };

    // 心跳 - 先清理旧定时器再创建新的
    if (state.heartbeatTimer) {
        clearInterval(state.heartbeatTimer);
    }
    state.heartbeatTimer = setInterval(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send('ping');
        }
    }, 30000);
}

function scheduleReconnect() {
    if (state.reconnectTimer) return;
    state.reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, state.reconnectAttempts), 30000);
    state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        connectWS();
    }, delay);
}

function updateWSStatus(connected) {
    const dot = document.getElementById('ws-status');
    const text = document.getElementById('ws-status-text');
    if (connected) {
        dot.classList.add('connected');
        text.textContent = '已连接';
    } else {
        dot.classList.remove('connected');
        text.textContent = '未连接';
    }
}

function handleWSMessage(msg) {
    switch (msg.type) {
        case 'init':
            // 初始化任务列表
            state.tasks = {};
            if (msg.data && msg.data.tasks) {
                msg.data.tasks.forEach(t => {
                    state.tasks[t.task_id] = t;
                });
            }
            renderTasks();
            updateDashboard();
            break;

        case 'task_update':
            if (msg.data) {
                state.tasks[msg.data.task_id] = msg.data;
                renderTaskItem(msg.data);
                updateDashboard();
            }
            break;

        case 'task_deleted':
            if (msg.data && msg.data.task_id) {
                delete state.tasks[msg.data.task_id];
                const el = document.getElementById(`task-${msg.data.task_id}`);
                if (el) el.remove();
                updateDashboard();
                checkEmptyState();
            }
            break;

        case 'global_stat':
            if (msg.data) {
                const speed = formatSpeed(msg.data.download_speed || 0);
                document.getElementById('stat-speed').textContent = speed;
            }
            break;

        case 'pong':
            break;
    }
}

// ============================================
// 渲染
// ============================================

function renderTasks() {
    const list = document.getElementById('task-list');
    const recent = document.getElementById('recent-tasks');
    const tasks = Object.values(state.tasks);

    if (tasks.length === 0) {
        list.innerHTML = `<div class="empty-state">
            <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
            </svg>
            <p>暂无任务</p>
        </div>`;
        recent.innerHTML = list.innerHTML;
        return;
    }

    list.innerHTML = '';
    tasks.forEach(task => {
        const el = createTaskElement(task, 'task');
        if (shouldShowTask(task)) {
            list.appendChild(el);
        }
    });

    // 最近任务（最多 5 条）
    recent.innerHTML = '';
    tasks.slice(0, 5).forEach(task => {
        recent.appendChild(createTaskElement(task, 'recent-task'));
    });

    checkEmptyState();
}

function shouldShowTask(task) {
    if (state.currentFilter === 'all') return true;
    return task.status === state.currentFilter;
}

function createTaskElement(task, prefix = 'task') {
    const div = document.createElement('div');
    div.className = 'task-item';
    div.id = `${prefix}-${task.task_id}`;
    div.innerHTML = buildTaskHTML(task);
    bindTaskActions(div, task.task_id);
    return div;
}

function buildTaskHTML(task) {
    const filename = task.filename || task.url.split('/').pop() || '未知文件';
    const status = task.status || 'pending';
    const dlProgress = task.download_progress || 0;
    const ulProgress = task.upload_progress || 0;

    let progressHTML = '';
    if (status === 'downloading' || status === 'paused') {
        progressHTML = `
            <div class="task-progress-section">
                <div class="progress-labels">
                    <span>下载进度</span>
                    <span>${dlProgress.toFixed(1)}%</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill download ${status === 'downloading' ? 'active' : ''}" style="width: ${dlProgress}%"></div>
                </div>
            </div>`;
    } else if (status === 'uploading') {
        progressHTML = `
            <div class="task-progress-section">
                <div class="progress-labels">
                    <span>下载进度</span>
                    <span>100%</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill download" style="width: 100%"></div>
                </div>
            </div>
            <div class="task-progress-section">
                <div class="progress-labels">
                    <span>上传进度</span>
                    <span>${ulProgress.toFixed(1)}%</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill upload active" style="width: ${ulProgress}%"></div>
                </div>
            </div>`;
    } else if (status === 'completed') {
        progressHTML = `
            <div class="task-progress-section">
                <div class="progress-bar">
                    <div class="progress-fill complete" style="width: 100%"></div>
                </div>
            </div>`;
    }

    // 操作按钮
    let actionsHTML = '';
    if (status === 'downloading') {
        actionsHTML = `
            <button class="btn btn-sm btn-ghost" data-action="pause" title="暂停">⏸</button>
            <button class="btn btn-sm btn-danger" data-action="cancel" title="取消">✕</button>`;
    } else if (status === 'paused') {
        actionsHTML = `
            <button class="btn btn-sm btn-ghost" data-action="resume" title="恢复">▶</button>
            <button class="btn btn-sm btn-danger" data-action="cancel" title="取消">✕</button>`;
    } else if (status === 'failed') {
        actionsHTML = `
            <button class="btn btn-sm btn-outline" data-action="retry" title="重试">↻</button>
            <button class="btn btn-sm btn-danger" data-action="delete" title="删除">🗑</button>`;
    } else if (status === 'completed' || status === 'cancelled') {
        actionsHTML = `
            <button class="btn btn-sm btn-danger" data-action="delete" title="删除">🗑</button>`;
    } else if (status === 'uploading') {
        actionsHTML = `
            <button class="btn btn-sm btn-danger" data-action="cancel" title="取消">✕</button>`;
    }

    const metaItems = [];
    if (task.file_size) metaItems.push(`<span class="task-meta-item">📦 ${task.file_size}</span>`);
    if (task.download_speed && status === 'downloading') metaItems.push(`<span class="task-meta-item">⬇ ${task.download_speed}</span>`);
    if (task.error) metaItems.push(`<span class="task-meta-item" style="color:var(--error)">⚠ ${task.error}</span>`);

    return `
        <div class="task-item-header">
            <span class="task-filename">${escapeHTML(filename)}</span>
            <span class="task-status ${status}">${getStatusText(status)}</span>
        </div>
        ${progressHTML}
        <div class="task-meta">
            ${metaItems.join('')}
            <div class="task-actions">
                ${actionsHTML}
            </div>
        </div>`;
}

function renderTaskItem(task) {
    // 更新任务列表页
    const existing = document.getElementById(`task-${task.task_id}`);
    if (existing) {
        existing.innerHTML = buildTaskHTML(task);
        bindTaskActions(existing, task.task_id);
        existing.style.display = shouldShowTask(task) ? '' : 'none';
    } else {
        // 新任务
        const list = document.getElementById('task-list');
        const empty = list.querySelector('.empty-state');
        if (empty) empty.remove();

        const el = createTaskElement(task, 'task');
        el.style.display = shouldShowTask(task) ? '' : 'none';
        list.prepend(el);
    }

    // 更新仪表盘最近任务
    updateRecentTasks();
}

function updateRecentTasks() {
    const recent = document.getElementById('recent-tasks');
    const tasks = Object.values(state.tasks).slice(0, 5);
    if (tasks.length === 0) {
        recent.innerHTML = `<div class="empty-state">
            <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
            </svg>
            <p>暂无任务</p>
        </div>`;
        return;
    }
    recent.innerHTML = '';
    tasks.forEach(task => {
        recent.appendChild(createTaskElement(task, 'recent-task'));
    });
}

function checkEmptyState() {
    const list = document.getElementById('task-list');
    const items = list.querySelectorAll('.task-item');
    const visibleItems = Array.from(items).filter(i => i.style.display !== 'none');
    if (visibleItems.length === 0 && !list.querySelector('.empty-state')) {
        list.innerHTML = `<div class="empty-state">
            <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
            </svg>
            <p>暂无任务</p>
        </div>`;
    }
}

function bindTaskActions(el, taskId) {
    el.querySelectorAll('[data-action]').forEach(btn => {
        btn.onclick = async (e) => {
            e.stopPropagation();
            const action = btn.dataset.action;
            try {
                await taskAction(taskId, action);
                showToast(`操作成功`, 'success');

                // 操作成功后立即刷新该任务状态
                if (action === 'delete') {
                    delete state.tasks[taskId];
                    const taskEl = document.getElementById(`task-${taskId}`);
                    if (taskEl) taskEl.remove();
                    const recentEl = document.getElementById(`recent-task-${taskId}`);
                    if (recentEl) recentEl.remove();
                    updateRecentTasks();
                    updateDashboard();
                    checkEmptyState();
                } else {
                    try {
                        const resp = await apiCall(`/api/task/${taskId}`);
                        if (resp && resp.data) {
                            state.tasks[taskId] = resp.data;
                            renderTaskItem(resp.data);
                            updateDashboard();
                        }
                    } catch (_) { /* WS 或轮询会兜底 */ }
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        };
    });
}

function updateDashboard() {
    const tasks = Object.values(state.tasks);
    const downloading = tasks.filter(t => t.status === 'downloading').length;
    const uploading = tasks.filter(t => t.status === 'uploading').length;
    const completed = tasks.filter(t => t.status === 'completed').length;

    document.getElementById('stat-downloading').textContent = downloading;
    document.getElementById('stat-uploading').textContent = uploading;
    document.getElementById('stat-completed').textContent = completed;
}

function escapeHTML(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ============================================
// 页面导航
// ============================================

function switchPage(pageName) {
    state.currentPage = pageName;

    // 更新导航
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.page === pageName);
    });

    // 更新页面
    document.querySelectorAll('.page').forEach(page => {
        page.classList.toggle('active', page.id === `page-${pageName}`);
    });

    // 加载设置
    if (pageName === 'settings') {
        loadAndFillSettings();
    }
}

// ============================================
// 设置
// ============================================

async function loadAndFillSettings() {
    try {
        const settings = await loadSettings();
        // aria2
        document.getElementById('aria2-rpc-url').value = settings.aria2?.rpc_url || '';
        document.getElementById('aria2-rpc-port').value = settings.aria2?.rpc_port || 6800;
        document.getElementById('aria2-rpc-secret').value = settings.aria2?.rpc_secret || '';
        document.getElementById('aria2-max-concurrent').value = settings.aria2?.max_concurrent || 3;
        document.getElementById('aria2-download-dir').value = settings.aria2?.download_dir || './downloads';
        // TelDrive
        document.getElementById('td-api-host').value = settings.teldrive?.api_host || '';
        document.getElementById('td-access-token').value = settings.teldrive?.access_token || '';
        document.getElementById('td-channel-id').value = settings.teldrive?.channel_id || 0;
        document.getElementById('td-chunk-size').value = settings.teldrive?.chunk_size || '500M';
        document.getElementById('td-upload-concurrency').value = settings.teldrive?.upload_concurrency || 4;
        document.getElementById('td-upload-dir').value = settings.teldrive?.upload_dir || '';
        document.getElementById('td-target-path').value = settings.teldrive?.target_path || '/';
        // General
        document.getElementById('gen-max-retries').value = settings.general?.max_retries || 3;
        document.getElementById('gen-auto-delete').checked = settings.general?.auto_delete !== false;
    } catch (e) {
        showToast('加载设置失败: ' + e.message, 'error');
    }
}

function collectSettings() {
    return {
        aria2: {
            rpc_url: document.getElementById('aria2-rpc-url').value,
            rpc_port: parseInt(document.getElementById('aria2-rpc-port').value) || 6800,
            rpc_secret: document.getElementById('aria2-rpc-secret').value,
            max_concurrent: parseInt(document.getElementById('aria2-max-concurrent').value) || 3,
            download_dir: document.getElementById('aria2-download-dir').value || './downloads'
        },
        teldrive: {
            api_host: document.getElementById('td-api-host').value,
            access_token: document.getElementById('td-access-token').value,
            channel_id: parseInt(document.getElementById('td-channel-id').value) || 0,
            chunk_size: document.getElementById('td-chunk-size').value,
            upload_concurrency: parseInt(document.getElementById('td-upload-concurrency').value) || 4,
            upload_dir: document.getElementById('td-upload-dir').value,
            target_path: document.getElementById('td-target-path').value || '/'
        },
        general: {
            max_retries: parseInt(document.getElementById('gen-max-retries').value) || 3,
            auto_delete: document.getElementById('gen-auto-delete').checked
        }
    };
}

// ============================================
// 弹窗
// ============================================

function openModal() {
    document.getElementById('modal-overlay').classList.add('active');
    document.getElementById('task-url').focus();
}

function closeModal() {
    document.getElementById('modal-overlay').classList.remove('active');
    document.getElementById('task-url').value = '';
    document.getElementById('task-filename').value = '';
    document.getElementById('task-path').value = '/';
}

// ============================================
// 事件绑定
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    // 导航
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            switchPage(item.dataset.page);
        });
    });

    // 过滤器
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            state.currentFilter = btn.dataset.filter;
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderTasks();
        });
    });

    // 添加任务按钮
    document.getElementById('btn-add-task').addEventListener('click', openModal);
    document.getElementById('btn-add-task-dash').addEventListener('click', openModal);

    // 弹窗
    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.getElementById('modal-cancel').addEventListener('click', closeModal);
    document.getElementById('modal-overlay').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeModal();
    });

    document.getElementById('modal-submit').addEventListener('click', async () => {
        const url = document.getElementById('task-url').value.trim();
        if (!url) {
            showToast('请输入下载链接', 'error');
            return;
        }
        const filename = document.getElementById('task-filename').value.trim();
        const path = document.getElementById('task-path').value.trim() || '/';

        try {
            await addTask(url, filename, path);
            showToast('任务添加成功', 'success');
            closeModal();
        } catch (e) {
            showToast('添加失败: ' + e.message, 'error');
        }
    });

    // 保存设置
    document.getElementById('btn-save-settings').addEventListener('click', async () => {
        try {
            const settings = collectSettings();
            await saveSettings(settings);
            showToast('设置已保存', 'success');
        } catch (e) {
            showToast('保存失败: ' + e.message, 'error');
        }
    });

    // 测试 aria2
    document.getElementById('btn-test-aria2').addEventListener('click', async () => {
        const resultEl = document.getElementById('aria2-test-result');
        resultEl.className = 'test-result';
        resultEl.style.display = 'none';

        // 先保存当前输入值
        try {
            const settings = collectSettings();
            await saveSettings(settings);
        } catch (e) { /* 忽略 */ }

        try {
            const result = await testAria2();
            resultEl.className = `test-result ${result.success ? 'success' : 'error'}`;
            resultEl.textContent = result.message + (result.version ? ` (v${result.version})` : '');
            resultEl.style.display = 'block';
        } catch (e) {
            resultEl.className = 'test-result error';
            resultEl.textContent = '测试失败: ' + e.message;
            resultEl.style.display = 'block';
        }
    });

    // 测试 TelDrive
    document.getElementById('btn-test-teldrive').addEventListener('click', async () => {
        const resultEl = document.getElementById('teldrive-test-result');
        resultEl.className = 'test-result';
        resultEl.style.display = 'none';

        try {
            const settings = collectSettings();
            await saveSettings(settings);
        } catch (e) { /* 忽略 */ }

        try {
            const result = await testTelDrive();
            resultEl.className = `test-result ${result.success ? 'success' : 'error'}`;
            resultEl.textContent = result.message;
            resultEl.style.display = 'block';
        } catch (e) {
            resultEl.className = 'test-result error';
            resultEl.textContent = '测试失败: ' + e.message;
            resultEl.style.display = 'block';
        }
    });

    // 键盘快捷键
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });

    // 连接 WebSocket
    connectWS();

    // 兜底轮询：每 3 秒通过 REST API 全量刷新，防止 WS 漏消息
    state.pollTimer = setInterval(fetchAllTasks, 3000);
});
