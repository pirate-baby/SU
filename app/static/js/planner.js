(function () {
    'use strict';

    // ---- State ----
    let tasks = [];
    let events = [];
    let interjections = [];
    let calWeekStart = getMonday(new Date());

    // ---- DOM refs ----
    const taskList = document.getElementById('task-list');
    const interjectionList = document.getElementById('interjection-list');
    const calGrid = document.getElementById('calendar-grid');
    const calTitle = document.getElementById('cal-title');
    const statusFilter = document.getElementById('task-status-filter');
    const priorityFilter = document.getElementById('task-priority-filter');

    // ---- Helpers ----

    function getMonday(d) {
        const dt = new Date(d);
        const day = dt.getDay();
        const diff = dt.getDate() - day + (day === 0 ? -6 : 1);
        dt.setDate(diff);
        dt.setHours(0, 0, 0, 0);
        return dt;
    }

    function addDays(d, n) {
        const dt = new Date(d);
        dt.setDate(dt.getDate() + n);
        return dt;
    }

    function fmtDate(d) {
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    }

    function fmtWeekday(d) {
        return d.toLocaleDateString('en-US', { weekday: 'short' });
    }

    function isSameDay(a, b) {
        return a.getFullYear() === b.getFullYear() &&
            a.getMonth() === b.getMonth() &&
            a.getDate() === b.getDate();
    }

    function isToday(d) {
        return isSameDay(d, new Date());
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    function dueBadge(dueDateStr) {
        if (!dueDateStr) return '';
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const due = new Date(dueDateStr + 'T00:00:00');
        const diff = Math.floor((due - today) / (1000 * 60 * 60 * 24));
        if (diff < 0) return `<span class="due-badge due-overdue">Overdue</span>`;
        if (diff === 0) return `<span class="due-badge due-today">Today</span>`;
        if (diff <= 7) return `<span class="due-badge due-upcoming">${dueDateStr}</span>`;
        return `<span class="due-badge">${dueDateStr}</span>`;
    }

    // ---- API ----

    async function fetchTasks() {
        const params = new URLSearchParams();
        const status = statusFilter.value;
        const priority = priorityFilter.value;
        if (status) params.set('status', status);
        if (priority) params.set('priority', priority);
        params.set('limit', '100');
        const res = await fetch('/api/tasks?' + params);
        tasks = await res.json();
        renderTasks();
        renderCalendar();
    }

    async function fetchEvents() {
        const start = calWeekStart.toISOString();
        const end = addDays(calWeekStart, 7).toISOString();
        const res = await fetch(`/api/events?start_after=${encodeURIComponent(start)}&start_before=${encodeURIComponent(end)}`);
        events = await res.json();
        renderCalendar();
    }

    async function fetchInterjections() {
        const res = await fetch('/api/interjections?status=pending');
        interjections = await res.json();
        renderInterjections();
    }

    async function createTask(data) {
        await fetch('/api/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        fetchTasks();
    }

    async function updateTask(id, data) {
        await fetch(`/api/tasks/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        fetchTasks();
    }

    async function deleteTask(id) {
        await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
        fetchTasks();
    }

    async function toggleTask(id, currentStatus) {
        const newStatus = currentStatus === 'done' ? 'pending' : 'done';
        await updateTask(id, { status: newStatus });
    }

    async function createEvent(data) {
        await fetch('/api/events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        fetchEvents();
    }

    async function updateEvent(id, data) {
        await fetch(`/api/events/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        fetchEvents();
    }

    async function deleteEvent(id) {
        await fetch(`/api/events/${id}`, { method: 'DELETE' });
        fetchEvents();
    }

    async function dismissInterjection(id) {
        await fetch(`/api/interjections/${id}/dismiss`, { method: 'POST' });
        fetchInterjections();
    }

    function chatAboutInterjection(id) {
        window.location.href = `/api/sessions/from-interjection/${id}`;
    }

    // ---- Render: Tasks ----

    function renderTasks() {
        if (!tasks.length) {
            taskList.innerHTML = '<div class="empty-state">No tasks</div>';
            return;
        }
        taskList.innerHTML = tasks.map(t => {
            const isDone = t.status === 'done';
            return `<div class="task-item" data-id="${t.id}">
                <div class="task-checkbox ${isDone ? 'checked' : ''}" data-id="${t.id}" data-status="${t.status}"></div>
                <div class="task-body">
                    <div class="task-title ${isDone ? 'done' : ''}">${escapeHtml(t.title)}</div>
                    <div class="task-meta">
                        <span class="priority-dot priority-${t.priority}"></span>
                        ${t.category ? `<span>${escapeHtml(t.category)}</span>` : ''}
                        ${dueBadge(t.due_date)}
                    </div>
                </div>
            </div>`;
        }).join('');

        // Checkbox click → toggle
        taskList.querySelectorAll('.task-checkbox').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleTask(el.dataset.id, el.dataset.status);
            });
        });

        // Row click → edit modal
        taskList.querySelectorAll('.task-item').forEach(el => {
            el.addEventListener('click', () => openTaskModal(el.dataset.id));
        });
    }

    // ---- Render: Calendar ----

    function renderCalendar() {
        const days = [];
        for (let i = 0; i < 7; i++) {
            days.push(addDays(calWeekStart, i));
        }

        calTitle.textContent = `Week of ${fmtDate(days[0])}`;

        let html = '<div class="cal-corner"></div>';

        // Day headers
        for (const day of days) {
            const todayClass = isToday(day) ? ' today' : '';
            html += `<div class="cal-header${todayClass}">
                <span>${fmtWeekday(day)}</span>
                <span class="day-num">${day.getDate()}</span>
            </div>`;
        }

        // Hour rows
        for (let h = 0; h < 24; h++) {
            const label = h === 0 ? '12 AM' : h < 12 ? `${h} AM` : h === 12 ? '12 PM' : `${h - 12} PM`;
            html += `<div class="cal-time">${label}</div>`;
            for (let d = 0; d < 7; d++) {
                const day = days[d];
                const dateStr = day.toISOString().split('T')[0];
                html += `<div class="cal-cell" data-date="${dateStr}" data-hour="${h}"></div>`;
            }
        }

        calGrid.innerHTML = html;

        // Place events on the grid
        for (const ev of events) {
            const start = new Date(ev.start_time);
            const dayIdx = days.findIndex(d => isSameDay(d, start));
            if (dayIdx === -1) continue;

            const hour = start.getHours();
            const minutes = start.getMinutes();
            const topOffset = (minutes / 60) * 48; // 48px per hour row

            let durationHours = 1; // default
            if (ev.end_time) {
                const end = new Date(ev.end_time);
                durationHours = Math.max(0.5, (end - start) / (1000 * 60 * 60));
            }
            const height = Math.max(20, durationHours * 48);

            // Find the cell
            const cellSelector = `.cal-cell[data-date="${days[dayIdx].toISOString().split('T')[0]}"][data-hour="${hour}"]`;
            const cell = calGrid.querySelector(cellSelector);
            if (!cell) continue;

            const eventEl = document.createElement('div');
            eventEl.className = 'cal-event';
            eventEl.style.top = topOffset + 'px';
            eventEl.style.height = height + 'px';
            eventEl.textContent = ev.title;
            eventEl.dataset.id = ev.id;
            eventEl.addEventListener('click', (e) => {
                e.stopPropagation();
                openEventModal(ev.id);
            });
            cell.appendChild(eventEl);
        }

        // Place tasks with due_date + due_time on the grid
        for (const t of tasks) {
            if (!t.due_date || !t.due_time || t.status === 'done') continue;
            const start = new Date(`${t.due_date}T${t.due_time}`);
            const dayIdx = days.findIndex(d => isSameDay(d, start));
            if (dayIdx === -1) continue;

            const hour = start.getHours();
            const minutes = start.getMinutes();
            const topOffset = (minutes / 60) * 48;

            const cellSelector = `.cal-cell[data-date="${days[dayIdx].toISOString().split('T')[0]}"][data-hour="${hour}"]`;
            const cell = calGrid.querySelector(cellSelector);
            if (!cell) continue;

            const taskEl = document.createElement('div');
            taskEl.className = 'cal-event cal-task';
            taskEl.style.top = topOffset + 'px';
            taskEl.style.height = '20px';
            taskEl.textContent = t.title;
            taskEl.dataset.id = t.id;
            taskEl.addEventListener('click', (e) => {
                e.stopPropagation();
                openTaskModal(t.id);
            });
            cell.appendChild(taskEl);
        }

        // Cell click → new event at that time
        calGrid.querySelectorAll('.cal-cell').forEach(cell => {
            cell.addEventListener('click', () => {
                const date = cell.dataset.date;
                const hour = cell.dataset.hour;
                const pad = (n) => String(n).padStart(2, '0');
                openEventModal(null, `${date}T${pad(hour)}:00`);
            });
        });
    }

    // ---- Render: Interjections ----

    function renderInterjections() {
        if (!interjections.length) {
            interjectionList.innerHTML = '<div class="empty-state">None pending</div>';
            return;
        }
        interjectionList.innerHTML = interjections.map(ij => `
            <div class="interjection-item">
                <span class="urgency-dot urgency-${ij.urgency || 'normal'}"></span>
                <span class="content">${escapeHtml(ij.content)}</span>
                <button class="chat-btn" data-id="${ij.id}" title="Chat about this">&#x1F4AC;</button>
                <button class="dismiss-btn" data-id="${ij.id}" title="Dismiss">&times;</button>
            </div>
        `).join('');

        interjectionList.querySelectorAll('.chat-btn').forEach(btn => {
            btn.addEventListener('click', () => chatAboutInterjection(btn.dataset.id));
        });
        interjectionList.querySelectorAll('.dismiss-btn').forEach(btn => {
            btn.addEventListener('click', () => dismissInterjection(btn.dataset.id));
        });
    }

    // ---- Task Modal ----

    const taskModal = document.getElementById('task-modal');
    const taskForm = document.getElementById('task-form');
    const taskDeleteBtn = document.getElementById('task-delete-btn');

    function openTaskModal(taskId) {
        const task = taskId ? tasks.find(t => t.id === taskId) : null;
        document.getElementById('task-modal-title').textContent = task ? 'Edit Task' : 'New Task';
        document.getElementById('task-id').value = task ? task.id : '';
        document.getElementById('task-title').value = task ? task.title : '';
        document.getElementById('task-description').value = task ? (task.description || '') : '';
        document.getElementById('task-priority').value = task ? task.priority : 3;
        document.getElementById('task-category').value = task ? (task.category || '') : '';
        document.getElementById('task-due-date').value = task ? (task.due_date || '') : '';
        document.getElementById('task-due-time').value = task ? (task.due_time || '') : '';
        taskDeleteBtn.style.display = task ? 'inline-block' : 'none';
        taskModal.style.display = 'flex';
        document.getElementById('task-title').focus();
    }

    taskForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const id = document.getElementById('task-id').value;
        const data = {
            title: document.getElementById('task-title').value,
            description: document.getElementById('task-description').value || null,
            priority: parseInt(document.getElementById('task-priority').value),
            category: document.getElementById('task-category').value || null,
            due_date: document.getElementById('task-due-date').value || null,
            due_time: document.getElementById('task-due-time').value || null,
        };
        if (id) {
            await updateTask(id, data);
        } else {
            await createTask(data);
        }
        taskModal.style.display = 'none';
    });

    taskDeleteBtn.addEventListener('click', async () => {
        const id = document.getElementById('task-id').value;
        if (id && confirm('Delete this task?')) {
            await deleteTask(id);
            taskModal.style.display = 'none';
        }
    });

    document.getElementById('task-cancel-btn').addEventListener('click', () => {
        taskModal.style.display = 'none';
    });
    document.getElementById('task-modal-close').addEventListener('click', () => {
        taskModal.style.display = 'none';
    });
    document.getElementById('add-task-btn').addEventListener('click', () => openTaskModal(null));

    // ---- Event Modal ----

    const eventModal = document.getElementById('event-modal');
    const eventForm = document.getElementById('event-form');
    const eventDeleteBtn = document.getElementById('event-delete-btn');

    function openEventModal(eventId, defaultStart) {
        const ev = eventId ? events.find(e => e.id === eventId) : null;
        document.getElementById('event-modal-title').textContent = ev ? 'Edit Event' : 'New Event';
        document.getElementById('event-id').value = ev ? ev.id : '';
        document.getElementById('event-title').value = ev ? ev.title : '';
        document.getElementById('event-description').value = ev ? (ev.description || '') : '';
        document.getElementById('event-location').value = ev ? (ev.location || '') : '';
        document.getElementById('event-all-day').checked = ev ? !!ev.all_day : false;

        if (ev) {
            document.getElementById('event-start').value = ev.start_time ? ev.start_time.slice(0, 16) : '';
            document.getElementById('event-end').value = ev.end_time ? ev.end_time.slice(0, 16) : '';
        } else {
            document.getElementById('event-start').value = defaultStart || '';
            document.getElementById('event-end').value = '';
        }

        eventDeleteBtn.style.display = ev ? 'inline-block' : 'none';
        eventModal.style.display = 'flex';
        document.getElementById('event-title').focus();
    }

    eventForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const id = document.getElementById('event-id').value;
        const data = {
            title: document.getElementById('event-title').value,
            start_time: document.getElementById('event-start').value,
            end_time: document.getElementById('event-end').value || null,
            description: document.getElementById('event-description').value || null,
            location: document.getElementById('event-location').value || null,
            all_day: document.getElementById('event-all-day').checked,
        };
        if (id) {
            await updateEvent(id, data);
        } else {
            await createEvent(data);
        }
        eventModal.style.display = 'none';
    });

    eventDeleteBtn.addEventListener('click', async () => {
        const id = document.getElementById('event-id').value;
        if (id && confirm('Delete this event?')) {
            await deleteEvent(id);
            eventModal.style.display = 'none';
        }
    });

    document.getElementById('event-cancel-btn').addEventListener('click', () => {
        eventModal.style.display = 'none';
    });
    document.getElementById('event-modal-close').addEventListener('click', () => {
        eventModal.style.display = 'none';
    });
    document.getElementById('add-event-btn').addEventListener('click', () => openEventModal(null));

    // ---- Calendar navigation ----

    document.getElementById('cal-prev').addEventListener('click', () => {
        calWeekStart = addDays(calWeekStart, -7);
        fetchEvents();
    });
    document.getElementById('cal-next').addEventListener('click', () => {
        calWeekStart = addDays(calWeekStart, 7);
        fetchEvents();
    });
    document.getElementById('cal-today').addEventListener('click', () => {
        calWeekStart = getMonday(new Date());
        fetchEvents();
    });

    // ---- Filter listeners ----

    statusFilter.addEventListener('change', fetchTasks);
    priorityFilter.addEventListener('change', fetchTasks);

    // ---- Close modals on overlay click ----

    taskModal.addEventListener('click', (e) => {
        if (e.target === taskModal) taskModal.style.display = 'none';
    });
    eventModal.addEventListener('click', (e) => {
        if (e.target === eventModal) eventModal.style.display = 'none';
    });

    // ---- Polling for live updates ----

    setInterval(() => {
        fetchInterjections();
    }, 30000);

    // ---- Boot ----

    fetchTasks();
    fetchEvents();
    fetchInterjections();
})();
