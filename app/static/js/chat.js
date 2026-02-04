let ws = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY = 2000;

const messagesContainer = document.getElementById('messages');
const messageInput = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const endSessionBtn = document.getElementById('end-session-btn');
const statusIndicator = document.getElementById('status');

const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsUrl = `${wsProtocol}//${window.location.host}/ws/chat/${SESSION_ID}`;

// Track pending tool calls so we can pair results with their call
const pendingToolCalls = new Map();

function connect() {
    setStatus('Connecting...', 'warning');

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
        setStatus('Connected', 'success');
        reconnectAttempts = 0;
        enableInput();
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleMessage(data);
        } catch (error) {
            console.error('Error parsing message:', error);
        }
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
        setStatus('Connection error', 'error');
    };

    ws.onclose = () => {
        console.log('WebSocket closed');
        setStatus('Disconnected', 'error');
        disableInput();

        if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            reconnectAttempts++;
            setTimeout(() => {
                console.log(`Reconnecting... (attempt ${reconnectAttempts})`);
                connect();
            }, RECONNECT_DELAY * reconnectAttempts);
        } else {
            setStatus('Connection lost. Please refresh the page.', 'error');
        }
    };
}

function handleMessage(data) {
    switch (data.type) {
        case 'history':
            data.messages.forEach(msg => {
                appendMessage(msg.role, msg.content, false);
            });
            break;

        case 'user_message':
            appendMessage('user', data.content);
            break;

        case 'assistant_start':
            createAssistantMessage();
            break;

        case 'assistant_chunk':
            appendToAssistantMessage(data.content);
            break;

        case 'tool_use':
            appendToolCall(data);
            break;

        case 'tool_result':
            appendToolResult(data);
            break;

        case 'assistant_end':
            finalizeAssistantMessage();
            break;

        case 'error':
            appendMessage('system', `Error: ${data.content}`, true);
            enableInput();
            break;

        case 'status':
            setStatus(data.content, 'info');
            break;
    }
}

function createAssistantMessage() {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant';
    messageDiv.id = 'current-assistant-message';

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.id = 'current-assistant-content';

    // Add thinking dots animation
    const thinkingDots = document.createElement('div');
    thinkingDots.className = 'thinking-dots';
    thinkingDots.innerHTML = '<span></span><span></span><span></span>';
    contentDiv.appendChild(thinkingDots);

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    scrollToBottom();
}

function removeThinkingDots() {
    const contentDiv = document.getElementById('current-assistant-content');
    if (contentDiv) {
        const dots = contentDiv.querySelector('.thinking-dots');
        if (dots) dots.remove();
    }
}

function appendToAssistantMessage(chunk) {
    const contentDiv = document.getElementById('current-assistant-content');
    if (!contentDiv) return;

    removeThinkingDots();

    // Append text to the last text node, or create one
    let textSpan = contentDiv.querySelector('.text-content:last-of-type');
    if (!textSpan || contentDiv.lastElementChild !== textSpan) {
        textSpan = document.createElement('span');
        textSpan.className = 'text-content';
        contentDiv.appendChild(textSpan);
    }
    textSpan.textContent += chunk;
    scrollToBottom();
}

function formatToolInput(input) {
    if (typeof input === 'string') return input;
    try {
        return JSON.stringify(input, null, 2);
    } catch {
        return String(input);
    }
}

function formatToolContent(content) {
    if (content == null) return '(no output)';
    if (typeof content === 'string') {
        // Try to pretty-print if it looks like JSON
        try {
            const parsed = JSON.parse(content);
            return JSON.stringify(parsed, null, 2);
        } catch {
            return content;
        }
    }
    if (Array.isArray(content)) {
        return content.map(block => {
            if (block.type === 'text') return block.text;
            return JSON.stringify(block, null, 2);
        }).join('\n');
    }
    return JSON.stringify(content, null, 2);
}

function appendToolCall(data) {
    const contentDiv = document.getElementById('current-assistant-content');
    if (!contentDiv) return;

    removeThinkingDots();

    const details = document.createElement('details');
    details.className = 'tool-call';
    details.id = `tool-call-${data.id}`;

    const summary = document.createElement('summary');
    summary.className = 'tool-call-summary';

    const icon = document.createElement('span');
    icon.className = 'tool-icon';
    icon.textContent = '\u2699';

    const label = document.createElement('span');
    label.className = 'tool-name';
    label.textContent = data.name;

    const status = document.createElement('span');
    status.className = 'tool-status running';
    status.textContent = 'running';

    summary.appendChild(icon);
    summary.appendChild(label);
    summary.appendChild(status);
    details.appendChild(summary);

    const body = document.createElement('div');
    body.className = 'tool-call-body';

    const inputSection = document.createElement('div');
    inputSection.className = 'tool-section';
    const inputLabel = document.createElement('div');
    inputLabel.className = 'tool-section-label';
    inputLabel.textContent = 'Input';
    const inputPre = document.createElement('pre');
    inputPre.className = 'tool-data';
    inputPre.textContent = formatToolInput(data.input);
    inputSection.appendChild(inputLabel);
    inputSection.appendChild(inputPre);
    body.appendChild(inputSection);

    const resultSection = document.createElement('div');
    resultSection.className = 'tool-section tool-result-section';
    resultSection.id = `tool-result-section-${data.id}`;
    resultSection.style.display = 'none';
    body.appendChild(resultSection);

    details.appendChild(body);
    contentDiv.appendChild(details);

    pendingToolCalls.set(data.id, details);
    scrollToBottom();
}

function appendToolResult(data) {
    const details = pendingToolCalls.get(data.tool_use_id);

    if (details) {
        // Update status badge
        const status = details.querySelector('.tool-status');
        if (status) {
            status.classList.remove('running');
            if (data.is_error) {
                status.classList.add('error');
                status.textContent = 'error';
            } else {
                status.classList.add('done');
                status.textContent = 'done';
            }
        }

        // Fill in result section
        const resultSection = details.querySelector('.tool-result-section');
        if (resultSection) {
            resultSection.style.display = '';
            const resultLabel = document.createElement('div');
            resultLabel.className = 'tool-section-label';
            resultLabel.textContent = 'Result';
            const resultPre = document.createElement('pre');
            resultPre.className = 'tool-data';
            if (data.is_error) resultPre.classList.add('tool-data-error');
            resultPre.textContent = formatToolContent(data.content);
            resultSection.appendChild(resultLabel);
            resultSection.appendChild(resultPre);
        }

        pendingToolCalls.delete(data.tool_use_id);
    } else {
        // Orphan result — render inline
        const contentDiv = document.getElementById('current-assistant-content');
        if (!contentDiv) return;

        const details = document.createElement('details');
        details.className = 'tool-call';

        const summary = document.createElement('summary');
        summary.className = 'tool-call-summary';
        const icon = document.createElement('span');
        icon.className = 'tool-icon';
        icon.textContent = '\u2699';
        const label = document.createElement('span');
        label.className = 'tool-name';
        label.textContent = `Result (${data.tool_use_id.slice(0, 8)})`;
        const statusEl = document.createElement('span');
        statusEl.className = data.is_error ? 'tool-status error' : 'tool-status done';
        statusEl.textContent = data.is_error ? 'error' : 'done';
        summary.appendChild(icon);
        summary.appendChild(label);
        summary.appendChild(statusEl);
        details.appendChild(summary);

        const body = document.createElement('div');
        body.className = 'tool-call-body';
        const resultPre = document.createElement('pre');
        resultPre.className = 'tool-data';
        if (data.is_error) resultPre.classList.add('tool-data-error');
        resultPre.textContent = formatToolContent(data.content);
        body.appendChild(resultPre);
        details.appendChild(body);

        contentDiv.appendChild(details);
    }
    scrollToBottom();
}

function finalizeAssistantMessage() {
    const messageDiv = document.getElementById('current-assistant-message');
    const contentDiv = document.getElementById('current-assistant-content');

    if (messageDiv) messageDiv.id = '';
    if (contentDiv) contentDiv.id = '';

    // Mark any still-pending tool calls as done
    pendingToolCalls.forEach((details) => {
        const status = details.querySelector('.tool-status.running');
        if (status) {
            status.classList.remove('running');
            status.classList.add('done');
            status.textContent = 'done';
        }
    });
    pendingToolCalls.clear();

    enableInput();
}

function appendMessage(role, content, isError = false) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;

    if (isError) {
        messageDiv.classList.add('error');
    }

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = content;

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    scrollToBottom();
}

function sendMessage() {
    const message = messageInput.value.trim();

    if (!message || !ws || ws.readyState !== WebSocket.OPEN) {
        return;
    }

    ws.send(JSON.stringify({
        type: 'user_message',
        content: message,
        session_id: SESSION_ID
    }));

    messageInput.value = '';
    disableInput();
}

function enableInput() {
    messageInput.disabled = false;
    sendBtn.disabled = false;
    messageInput.focus();
}

function disableInput() {
    messageInput.disabled = true;
    sendBtn.disabled = true;
}

function setStatus(message, type) {
    statusIndicator.textContent = message;
    statusIndicator.className = `status-indicator ${type}`;

    if (type === 'success' || type === 'info') {
        setTimeout(() => {
            statusIndicator.textContent = '';
            statusIndicator.className = 'status-indicator';
        }, 3000);
    }
}

function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

async function endSession() {
    if (!confirm('Are you sure you want to end this session?')) {
        return;
    }

    try {
        const response = await fetch(`/api/sessions/${SESSION_ID}/end`, {
            method: 'POST'
        });

        if (response.ok) {
            ws.close();
            window.location.href = '/';
        } else {
            alert('Failed to end session');
        }
    } catch (error) {
        console.error('Error ending session:', error);
        alert('Error ending session');
    }
}

sendBtn.addEventListener('click', sendMessage);
endSessionBtn.addEventListener('click', endSession);

messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

connect();
