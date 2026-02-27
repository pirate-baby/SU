/**
 * Voice mode controller for SU Chat.
 *
 * Manages:
 * - Client-side STT via ElevenLabs Scribe v2 Realtime WebSocket
 * - Audio playback of TTS chunks received from the server
 * - Voice mode state machine: idle -> recording -> processing -> playing -> idle
 */
class VoiceMode {
    constructor() {
        this.state = 'idle'; // idle | recording | processing | playing
        this.enabled = false;
        this.conversationActive = false; // persistent voice conversation mode

        // Audio contexts
        this.sttAudioContext = null; // 16kHz for STT capture
        this.playbackContext = null; // default rate for MP3 playback

        // STT
        this.sttSocket = null;
        this.mediaStream = null;
        this.workletNode = null;
        this.sourceNode = null;
        this.partialText = '';
        this.committedText = '';

        // Playback
        this.nextPlayTime = 0;
        this.pendingAudioChunks = 0;
        this.playedAudioChunks = 0;
        this.audioEndReceived = false;

        // UI elements (set during init)
        this.micBtn = null;
        this.voiceStatus = null;
        this.voiceStatusDot = null;
        this.voiceStatusText = null;
    }

    async init() {
        try {
            const resp = await fetch('/api/voice/config');
            const config = await resp.json();
            this.enabled = config.enabled;
        } catch {
            this.enabled = false;
        }

        this.micBtn = document.getElementById('voice-btn');
        this.voiceStatus = document.getElementById('voice-status');
        this.voiceStatusDot = document.getElementById('voice-status-dot');
        this.voiceStatusText = document.getElementById('voice-status-text');

        if (this.enabled && this.micBtn) {
            this.micBtn.style.display = 'flex';
            this.micBtn.addEventListener('click', () => this._onMicClick());
        }
    }

    // -- State transitions --

    async _onMicClick() {
        // Create playback context on first user gesture (required by iOS Safari)
        this._ensurePlaybackContext();

        if (!this.conversationActive) {
            // Enter voice conversation mode
            this.conversationActive = true;
            this._updateUI();
            await this.startRecording();
        } else {
            // Exit voice conversation mode
            this.endConversation();
        }
    }

    endConversation() {
        this.conversationActive = false;
        if (this.state === 'recording') {
            this.stopRecording(false); // discard partial — user is ending conversation
        }
        this._cleanup();
        this.state = 'idle';
        this._updateUI();
        this._showPartialTranscript('');
    }

    async startRecording() {
        this.state = 'recording';
        this.partialText = '';
        this.committedText = '';
        this._updateUI();

        try {
            // Request microphone access
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });

            // Create 16kHz audio context for STT
            this.sttAudioContext = new AudioContext({ sampleRate: 16000 });

            // Get a single-use token for STT
            const tokenResp = await fetch('/api/voice/token/stt');
            if (!tokenResp.ok) throw new Error('Failed to get STT token');
            const { token } = await tokenResp.json();

            // Open STT WebSocket
            const sttUrl = 'wss://api.elevenlabs.io/v1/speech-to-text/realtime'
                + '?model_id=scribe_v2_realtime'
                + `&token=${encodeURIComponent(token)}`
                + '&commit_strategy=vad'
                + '&vad_silence_threshold_secs=1.0'
                + '&audio_format=pcm_16000';

            this.sttSocket = new WebSocket(sttUrl);
            this.sttSocket.onmessage = (event) => this._handleSTTMessage(JSON.parse(event.data));
            this.sttSocket.onerror = () => {
                console.error('STT WebSocket error');
                this.stopRecording(false);
            };
            this.sttSocket.onclose = () => {
                // If we're still recording, it means the connection was lost
                if (this.state === 'recording') {
                    this.stopRecording(false);
                }
            };

            // Wait for STT socket to open before starting audio capture
            await new Promise((resolve, reject) => {
                this.sttSocket.onopen = resolve;
                setTimeout(() => reject(new Error('STT connection timeout')), 5000);
            });

            // Set up AudioWorklet for PCM capture
            await this.sttAudioContext.audioWorklet.addModule('/static/js/pcm-processor.js');
            this.sourceNode = this.sttAudioContext.createMediaStreamSource(this.mediaStream);
            this.workletNode = new AudioWorkletNode(this.sttAudioContext, 'pcm-processor');

            this.workletNode.port.onmessage = (event) => {
                if (this.sttSocket && this.sttSocket.readyState === WebSocket.OPEN) {
                    const float32 = event.data;
                    const int16 = this._float32ToInt16(float32);
                    const base64 = this._arrayBufferToBase64(int16.buffer);
                    this.sttSocket.send(JSON.stringify({
                        message_type: 'input_audio_chunk',
                        audio_base_64: base64,
                    }));
                }
            };

            this.sourceNode.connect(this.workletNode);
            // Connect to destination to keep processing alive
            this.workletNode.connect(this.sttAudioContext.destination);

        } catch (err) {
            console.error('Failed to start recording:', err);
            this._cleanup();
            this.state = 'idle';
            this._updateUI();
        }
    }

    stopRecording(manual) {
        // Stop the microphone and worklet
        if (this.sourceNode) {
            this.sourceNode.disconnect();
            this.sourceNode = null;
        }
        if (this.workletNode) {
            this.workletNode.disconnect();
            this.workletNode = null;
        }
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(t => t.stop());
            this.mediaStream = null;
        }
        if (this.sttAudioContext) {
            this.sttAudioContext.close().catch(() => {});
            this.sttAudioContext = null;
        }

        // If manually stopped (user tapped mic again), send whatever we have
        if (manual) {
            if (this.sttSocket) {
                this.sttSocket.close();
                this.sttSocket = null;
            }
            const text = (this.committedText || this.partialText).trim();
            if (text && window._voiceSendMessage) {
                this.state = 'processing';
                this._updateUI();
                this._showPartialTranscript('');
                window._voiceSendMessage(text);
            } else {
                this.state = 'idle';
                this._updateUI();
                this._showPartialTranscript('');
            }
        }
    }

    _handleSTTMessage(data) {
        const msgType = data.message_type;

        if (msgType === 'partial_transcript') {
            this.partialText = data.text || '';
            this._showPartialTranscript(this.partialText);
        } else if (msgType === 'committed_transcript' || msgType === 'committed_transcript_with_timestamps') {
            // VAD detected silence and committed the transcript.
            // Accumulate text and immediately finish — one utterance per recording.
            const text = data.text || '';
            if (text.trim()) {
                this.committedText += (this.committedText ? ' ' : '') + text.trim();
            }
            this._showPartialTranscript(this.committedText);
            this._finishSTT();
        } else if (msgType === 'session_ended' || msgType === 'end_of_stream') {
            this._finishSTT();
        } else if (msgType && msgType.endsWith('_error')) {
            console.error('STT error:', data);
            this._cleanup();
            this.state = 'idle';
            this._updateUI();
        }
    }

    _finishSTT() {
        // Guard against multiple calls
        if (this.state !== 'recording') return;

        // Stop mic and close STT
        this.stopRecording(false);
        if (this.sttSocket) {
            this.sttSocket.close();
            this.sttSocket = null;
        }

        const text = (this.committedText || this.partialText).trim();
        if (text && window._voiceSendMessage) {
            this.state = 'processing';
            this._updateUI();
            this._showPartialTranscript('');
            window._voiceSendMessage(text);
        } else {
            this.state = 'idle';
            this._updateUI();
            this._showPartialTranscript('');
        }
    }

    // -- TTS Audio Playback --

    handleAudioChunk(base64Audio) {
        if (this.state !== 'playing') {
            this.state = 'playing';
            this._ensurePlaybackContext();
            // Small initial buffer (200ms) to let first chunk decode before playing
            this.nextPlayTime = this.playbackContext.currentTime + 0.2;
            this.pendingAudioChunks = 0;
            this.playedAudioChunks = 0;
            this.audioEndReceived = false;
            this._updateUI();
        }

        this.pendingAudioChunks++;

        // Decode base64 MP3 to ArrayBuffer
        const binary = atob(base64Audio);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }

        this.playbackContext.decodeAudioData(
            bytes.buffer.slice(0), // slice to get a transferable copy
            (buffer) => {
                const source = this.playbackContext.createBufferSource();
                source.buffer = buffer;
                source.connect(this.playbackContext.destination);

                // Schedule seamlessly — small overlap tolerance to prevent gaps
                const startTime = Math.max(this.playbackContext.currentTime, this.nextPlayTime);
                source.start(startTime);
                // Subtract a tiny overlap (5ms) to prevent audible gaps between chunks
                this.nextPlayTime = startTime + buffer.duration - 0.005;

                source.onended = () => {
                    this.playedAudioChunks++;
                    this._checkPlaybackComplete();
                };
            },
            (err) => {
                // Decode error — skip this chunk
                console.warn('Audio decode error, skipping chunk:', err);
                this.playedAudioChunks++;
                this._checkPlaybackComplete();
            }
        );
    }

    handleFillerEnd() {
        // Filler audio finished — let any queued chunks play out but don't
        // trigger the conversation loop or state transition. The real response
        // audio will arrive after assistant_start and have its own audio_end.
        // Nothing to do — chunks will play out naturally and the real
        // audio_end will be the one that matters.
    }

    handleAudioEnd() {
        this.audioEndReceived = true;
        this._checkPlaybackComplete();
    }

    _checkPlaybackComplete() {
        if (this.audioEndReceived && this.playedAudioChunks >= this.pendingAudioChunks) {
            // All audio has been played
            // Small buffer to let the last chunk finish
            setTimeout(() => {
                if (this.conversationActive) {
                    // Continuous conversation — start listening again
                    this.state = 'idle';
                    this.startRecording();
                } else {
                    this.state = 'idle';
                    this._updateUI();
                }
            }, 200);
        }
    }

    _ensurePlaybackContext() {
        if (!this.playbackContext || this.playbackContext.state === 'closed') {
            this.playbackContext = new AudioContext();
        }
        if (this.playbackContext.state === 'suspended') {
            this.playbackContext.resume();
        }
    }

    // -- UI --

    _updateUI() {
        if (!this.micBtn) return;

        // Reset classes
        this.micBtn.classList.remove('recording', 'processing', 'playing', 'voice-active');

        if (this.conversationActive) {
            // Mic is always clickable in conversation mode (to end it)
            this.micBtn.classList.add('voice-active');
            this.micBtn.disabled = false;

            switch (this.state) {
                case 'idle':
                    this._setVoiceStatus('Voice on');
                    break;
                case 'recording':
                    this.micBtn.classList.add('recording');
                    this._setVoiceStatus('Listening...');
                    break;
                case 'processing':
                    this.micBtn.classList.add('processing');
                    this._setVoiceStatus('Thinking...');
                    break;
                case 'playing':
                    this.micBtn.classList.add('playing');
                    this._setVoiceStatus('Speaking...');
                    break;
            }
        } else {
            switch (this.state) {
                case 'idle':
                    this.micBtn.disabled = false;
                    this._setVoiceStatus('');
                    break;
                case 'recording':
                    this.micBtn.classList.add('recording');
                    this.micBtn.disabled = false;
                    this._setVoiceStatus('Listening...');
                    break;
                case 'processing':
                    this.micBtn.classList.add('processing');
                    this.micBtn.disabled = true;
                    this._setVoiceStatus('Thinking...');
                    break;
                case 'playing':
                    this.micBtn.classList.add('playing');
                    this.micBtn.disabled = true;
                    this._setVoiceStatus('Speaking...');
                    break;
            }
        }
    }

    _setVoiceStatus(text) {
        if (!this.voiceStatus) return;
        if (text) {
            this.voiceStatus.style.display = 'flex';
            if (this.voiceStatusText) this.voiceStatusText.textContent = text;
            if (this.voiceStatusDot) {
                this.voiceStatusDot.className = 'voice-status-dot';
                if (this.state === 'recording') this.voiceStatusDot.classList.add('recording');
                if (this.state === 'playing') this.voiceStatusDot.classList.add('playing');
            }
        } else {
            this.voiceStatus.style.display = 'none';
        }
    }

    _showPartialTranscript(text) {
        let el = document.getElementById('partial-transcript');
        if (!el) {
            el = document.createElement('div');
            el.id = 'partial-transcript';
            el.className = 'partial-transcript';
            const inputContainer = document.querySelector('.input-container');
            if (inputContainer) {
                inputContainer.parentNode.insertBefore(el, inputContainer);
            }
        }
        if (text) {
            el.textContent = text;
            el.style.display = 'block';
        } else {
            el.style.display = 'none';
            el.textContent = '';
        }
    }

    // -- Cleanup --

    _cleanup() {
        if (this.sourceNode) { this.sourceNode.disconnect(); this.sourceNode = null; }
        if (this.workletNode) { this.workletNode.disconnect(); this.workletNode = null; }
        if (this.mediaStream) { this.mediaStream.getTracks().forEach(t => t.stop()); this.mediaStream = null; }
        if (this.sttAudioContext) { this.sttAudioContext.close().catch(() => {}); this.sttAudioContext = null; }
        if (this.sttSocket) { this.sttSocket.close(); this.sttSocket = null; }
    }

    // -- Utility --

    _float32ToInt16(float32) {
        const int16 = new Int16Array(float32.length);
        for (let i = 0; i < float32.length; i++) {
            const s = Math.max(-1, Math.min(1, float32[i]));
            int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        return int16;
    }

    _arrayBufferToBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = '';
        for (let i = 0; i < bytes.length; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return btoa(binary);
    }
}

// Global instance — initialized from chat.js
const voiceMode = new VoiceMode();
