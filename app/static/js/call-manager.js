/**
 * Call manager: orchestrates the phone-call UI overlay.
 *
 * Manages two screens:
 *   - Ring screen (incoming call from SU): accept / reject
 *   - Active screen (connected call): mute, hang up, state labels, timer
 *
 * Works with VoiceMode for STT/TTS and the main WebSocket for signaling.
 */
class CallManager {
    constructor() {
        this.state = 'idle'; // idle | ringing | active
        this.muted = false;
        this.timerInterval = null;
        this.callStartTime = null;
        this.wakeLock = null;

        // Incoming call context
        this.incomingSessionId = null;
        this.incomingContext = null;

        // Interrupt detection
        this._interruptAnalyser = null;
        this._interruptCheckInterval = null;
        this._interruptThreshold = 0.015; // RMS threshold

        // Ringtone
        this._ringtoneOsc = null;
        this._ringtoneCtx = null;

        // DOM elements (bound in init)
        this.overlay = null;
        this.stateLabel = null;
        this.timerEl = null;
        this.muteBtn = null;
        this.hangupBtn = null;
        this.acceptBtn = null;
        this.rejectBtn = null;
        this.stateDot = null;
    }

    init() {
        this.overlay = document.getElementById('call-overlay');
        if (!this.overlay) return;

        this.stateLabel = this.overlay.querySelector('.call-state-label');
        this.timerEl = this.overlay.querySelector('.call-timer');
        this.muteBtn = document.getElementById('call-mute-btn');
        this.hangupBtn = document.getElementById('call-hangup-btn');
        this.acceptBtn = document.getElementById('call-accept-btn');
        this.rejectBtn = document.getElementById('call-reject-btn');
        this.stateDot = this.overlay.querySelector('.call-state-dot');

        // Button handlers
        if (this.acceptBtn) this.acceptBtn.addEventListener('click', () => this.acceptCall());
        if (this.rejectBtn) this.rejectBtn.addEventListener('click', () => this.rejectCall());
        if (this.hangupBtn) this.hangupBtn.addEventListener('click', () => this.hangUp());
        if (this.muteBtn) this.muteBtn.addEventListener('click', () => this.toggleMute());
    }

    // ---- Incoming call ----

    showIncomingCall(data) {
        if (!this.overlay) return;
        this.incomingSessionId = data.session_id;
        this.incomingContext = data.context;

        this.state = 'ringing';
        this.overlay.className = 'call-overlay visible ringing';
        this._setStateLabel('SU is calling...');
        this._startRingtone();
    }

    acceptCall() {
        // Check for iOS WebView (Telegram, etc.) where getUserMedia is unavailable
        if (this._isRestrictedWebView()) {
            this._stopRingtone();
            this._setStateLabel('Open in Safari to answer');
            // Copy the current URL so they can open it in a real browser
            const url = window.location.href;
            if (navigator.clipboard) {
                navigator.clipboard.writeText(url).catch(() => {});
            }
            window.open(url, '_blank');
            return;
        }

        this._stopRingtone();

        this.state = 'active';
        this.overlay.className = 'call-overlay visible active';
        this.callStartTime = Date.now();
        this._startTimer();
        this._setStateLabel('Connecting...');
        this._requestWakeLock();

        // Start voice mode — this enters the continuous conversation loop
        if (typeof voiceMode !== 'undefined' && voiceMode.enabled) {
            voiceMode._ensurePlaybackContext();
            voiceMode.conversationActive = true;
            voiceMode._updateUI();
            voiceMode.startRecording();
        }

        this._setCallState('listening');
    }

    rejectCall() {
        this._stopRingtone();
        this._hide();

        // Notify server
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'call_action',
                action: 'reject',
                session_id: this.incomingSessionId,
            }));
        }
    }

    // ---- User-initiated call ----

    startCall() {
        if (!this.overlay) return;

        this.state = 'active';
        this.overlay.className = 'call-overlay visible active';

        // Only reset timer if not already running (e.g. from early "Connecting..." phase)
        if (!this.timerInterval) {
            this.callStartTime = Date.now();
            this._startTimer();
        }
        this._requestWakeLock();

        // Voice mode should already be starting via the mic button handler
        this._setCallState('listening');
    }

    // ---- Hang up ----

    hangUp() {
        // Stop voice mode
        if (typeof voiceMode !== 'undefined' && voiceMode.conversationActive) {
            voiceMode.endConversation();
        }

        // Notify server of hangup
        if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'call_action',
                action: 'hangup',
                session_id: typeof SESSION_ID !== 'undefined' ? SESSION_ID : null,
            }));
        }

        this._stopInterruptDetection();
        this._stopRingtone();
        this._hide();

        // On the dedicated call page, end the session and redirect home
        if (typeof AUTO_CALL_MODE !== 'undefined' && AUTO_CALL_MODE) {
            fetch(`/api/sessions/${SESSION_ID}/end`, { method: 'POST' })
                .then(() => { window.location.href = '/'; })
                .catch(() => { window.location.href = '/'; });
        }
    }

    // Called when server sends call_ended
    onCallEndedBySU() {
        if (typeof voiceMode !== 'undefined' && voiceMode.conversationActive) {
            voiceMode.endConversation();
        }
        this._stopInterruptDetection();
        this._hide();
    }

    // ---- Mute ----

    toggleMute() {
        this.muted = !this.muted;
        if (this.muteBtn) {
            this.muteBtn.classList.toggle('active', this.muted);
        }

        // Mute/unmute the mic stream
        if (typeof voiceMode !== 'undefined' && voiceMode.mediaStream) {
            voiceMode.mediaStream.getAudioTracks().forEach(t => {
                t.enabled = !this.muted;
            });
        }
    }

    // ---- Call state updates (from voice mode) ----

    _setCallState(state) {
        if (this.state !== 'active') return;

        const labels = {
            listening: 'Listening...',
            thinking: 'SU is thinking...',
            speaking: 'SU is speaking...',
            connected: 'Connected',
        };

        this._setStateLabel(labels[state] || 'Connected');

        if (this.stateDot) {
            this.stateDot.className = 'call-state-dot';
            if (state === 'listening') this.stateDot.classList.add('listening');
            if (state === 'thinking') this.stateDot.classList.add('thinking');
            if (state === 'speaking') this.stateDot.classList.add('speaking');
        }

        // Start interrupt detection when SU is speaking
        if (state === 'speaking') {
            this._startInterruptDetection();
        } else {
            this._stopInterruptDetection();
        }
    }

    // ---- Interrupt detection ----

    _startInterruptDetection() {
        if (this._interruptCheckInterval) return;
        if (typeof voiceMode === 'undefined' || !voiceMode.mediaStream) return;

        try {
            // Use the playback context or create one for analysis
            const ctx = voiceMode.playbackContext || new AudioContext();
            const source = ctx.createMediaStreamSource(voiceMode.mediaStream);
            this._interruptAnalyser = ctx.createAnalyser();
            this._interruptAnalyser.fftSize = 512;
            source.connect(this._interruptAnalyser);

            const dataArray = new Float32Array(this._interruptAnalyser.fftSize);

            this._interruptCheckInterval = setInterval(() => {
                if (!this._interruptAnalyser) return;
                this._interruptAnalyser.getFloatTimeDomainData(dataArray);

                // Calculate RMS
                let sum = 0;
                for (let i = 0; i < dataArray.length; i++) {
                    sum += dataArray[i] * dataArray[i];
                }
                const rms = Math.sqrt(sum / dataArray.length);

                if (rms > this._interruptThreshold) {
                    this._onInterrupt();
                }
            }, 80);
        } catch (err) {
            console.warn('Interrupt detection setup failed:', err);
        }
    }

    _stopInterruptDetection() {
        if (this._interruptCheckInterval) {
            clearInterval(this._interruptCheckInterval);
            this._interruptCheckInterval = null;
        }
        this._interruptAnalyser = null;
    }

    _onInterrupt() {
        console.log('Interrupt detected — cancelling playback');
        this._stopInterruptDetection();

        if (typeof voiceMode !== 'undefined') {
            voiceMode.cancelPlayback();
            // Start listening again immediately
            if (voiceMode.conversationActive) {
                voiceMode.state = 'idle';
                voiceMode.startRecording();
            }
        }

        this._setCallState('listening');
    }

    // ---- Ringtone ----

    _startRingtone() {
        try {
            this._ringtoneCtx = new AudioContext();
            this._playRingPattern();
        } catch {
            // No audio — silent ring
        }
    }

    _playRingPattern() {
        if (!this._ringtoneCtx || this.state !== 'ringing') return;

        const ctx = this._ringtoneCtx;
        const now = ctx.currentTime;

        // Two-tone ring pattern (like a phone)
        for (let i = 0; i < 2; i++) {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.value = i === 0 ? 440 : 480;
            gain.gain.value = 0.08;
            osc.connect(gain);
            gain.connect(ctx.destination);

            osc.start(now);
            gain.gain.setValueAtTime(0.08, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + 0.8);
            osc.stop(now + 0.8);
        }

        // Repeat after pause
        this._ringtoneTimeout = setTimeout(() => this._playRingPattern(), 2000);
    }

    _stopRingtone() {
        if (this._ringtoneTimeout) {
            clearTimeout(this._ringtoneTimeout);
            this._ringtoneTimeout = null;
        }
        if (this._ringtoneCtx) {
            this._ringtoneCtx.close().catch(() => {});
            this._ringtoneCtx = null;
        }
    }

    // ---- Wake Lock ----

    async _requestWakeLock() {
        try {
            if ('wakeLock' in navigator) {
                this.wakeLock = await navigator.wakeLock.request('screen');
            }
        } catch {
            // Wake lock not available
        }
    }

    _releaseWakeLock() {
        if (this.wakeLock) {
            this.wakeLock.release().catch(() => {});
            this.wakeLock = null;
        }
    }

    // ---- Timer ----

    _startTimer() {
        this._updateTimer();
        this.timerInterval = setInterval(() => this._updateTimer(), 1000);
    }

    _updateTimer() {
        if (!this.timerEl || !this.callStartTime) return;
        const elapsed = Math.floor((Date.now() - this.callStartTime) / 1000);
        const min = Math.floor(elapsed / 60);
        const sec = elapsed % 60;
        this.timerEl.textContent = `${min}:${sec.toString().padStart(2, '0')}`;
    }

    // ---- Internal ----

    _setStateLabel(text) {
        if (this.stateLabel) this.stateLabel.textContent = text;
    }

    _isRestrictedWebView() {
        const ua = navigator.userAgent || '';
        // Telegram WebView on iOS
        if (/TelegramWebview/i.test(ua)) return true;
        // Generic iOS in-app browser (no Safari)
        if (/iPhone|iPad/.test(ua) && !/Safari/.test(ua)) return true;
        // Check if getUserMedia is available
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return true;
        return false;
    }

    _hide() {
        this.state = 'idle';
        this._stopRingtone();
        this._releaseWakeLock();

        if (this.timerInterval) {
            clearInterval(this.timerInterval);
            this.timerInterval = null;
        }

        this.callStartTime = null;
        this.muted = false;
        this.incomingSessionId = null;
        this.incomingContext = null;

        if (this.muteBtn) this.muteBtn.classList.remove('active');
        if (this.overlay) this.overlay.className = 'call-overlay';
    }
}

// Global instance — initialized from chat.js
const callManager = new CallManager();
