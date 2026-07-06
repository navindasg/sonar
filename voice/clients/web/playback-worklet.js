// Playback worklet: a fixed Float32 ring buffer fed by the main thread from
// incoming PCM frames. process() pulls samples into output, emitting silence on
// underrun so playback never throws. Runs in the 24 kHz playback AudioContext.
//
// Messages from the main thread:
//   Float32Array  -> samples to enqueue (assumed 24 kHz mono)
//   'flush'       -> zero the buffer immediately (barge-in / interrupted)
const RING_CAPACITY = 24000; // ~1 s headroom at 24 kHz; we keep it lightly filled

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ring = new Float32Array(RING_CAPACITY);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.available = 0; // samples currently buffered

    this.port.onmessage = (event) => {
      if (event.data === 'flush') {
        this.flush();
      } else if (event.data instanceof Float32Array) {
        this.enqueue(event.data);
      }
    };
  }

  flush() {
    this.readIndex = 0;
    this.writeIndex = 0;
    this.available = 0;
    this.ring.fill(0);
  }

  enqueue(samples) {
    for (let i = 0; i < samples.length; i++) {
      this.ring[this.writeIndex] = samples[i];
      this.writeIndex = (this.writeIndex + 1) % RING_CAPACITY;
      if (this.available < RING_CAPACITY) {
        this.available++;
      } else {
        // Overflow: advance read to drop the oldest sample (stay real-time).
        this.readIndex = (this.readIndex + 1) % RING_CAPACITY;
      }
    }
  }

  // Fill the (mono) output channel from the ring; pad with silence on underrun.
  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) {
      return true;
    }
    for (let i = 0; i < out.length; i++) {
      if (this.available > 0) {
        out[i] = this.ring[this.readIndex];
        this.readIndex = (this.readIndex + 1) % RING_CAPACITY;
        this.available--;
      } else {
        out[i] = 0; // underrun -> silence
      }
    }
    return true;
  }
}

registerProcessor('playback-processor', PlaybackProcessor);
