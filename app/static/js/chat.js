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

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    scrollToBottom();
}

function appendToAssistantMessage(chunk) {
    const contentDiv = document.getElementById('current-assistant-content');
    if (contentDiv) {
        contentDiv.textContent += chunk;
        scrollToBottom();
    }
}

function finalizeAssistantMessage() {
    const messageDiv = document.getElementById('current-assistant-message');
    const contentDiv = document.getElementById('current-assistant-content');

    if (messageDiv) messageDiv.id = '';
    if (contentDiv) contentDiv.id = '';

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
