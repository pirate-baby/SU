/**
 * AudioWorklet processor that captures raw PCM samples from the microphone
 * and posts them to the main thread for transmission to ElevenLabs STT.
 *
 * Buffers samples to reduce message frequency — posts every ~100ms worth
 * of audio (1600 samples at 16kHz) instead of every 128-sample render quantum.
 */
class PCMProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._buffer = new Float32Array(1600); // ~100ms at 16kHz
        this._offset = 0;
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        const samples = input[0];
        let i = 0;
        while (i < samples.length) {
            const remaining = this._buffer.length - this._offset;
            const toCopy = Math.min(remaining, samples.length - i);
            this._buffer.set(samples.subarray(i, i + toCopy), this._offset);
            this._offset += toCopy;
            i += toCopy;

            if (this._offset >= this._buffer.length) {
                this.port.postMessage(new Float32Array(this._buffer));
                this._offset = 0;
            }
        }
        return true;
    }
}

registerProcessor('pcm-processor', PCMProcessor);
