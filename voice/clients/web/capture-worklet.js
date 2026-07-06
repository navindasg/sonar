// Capture worklet: convert mono Float32 render quanta -> Int16 PCM and post the
// transferable buffer back to the main thread, which forwards it as a WS binary
// frame. Runs in the 16 kHz capture AudioContext so no resampling happens here.
class CaptureProcessor extends AudioWorkletProcessor {
  // process() is called per 128-sample render quantum. `inputs[0][0]` is the
  // first input's first (mono) channel; absent when the mic isn't producing.
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) {
      return true; // keep the node alive even on momentary silence gaps
    }

    // Float32 [-1, 1] -> Int16 [-32768, 32767], clamped to avoid overflow wrap.
    const pcm = new Int16Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }

    // Transfer ownership of the underlying buffer (zero-copy) to the main thread.
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
