import { useEffect, useRef } from 'react';
import type { BrainState } from '../../types';

/**
 * Centered audio-reactive orb. Four visual modes driven by Hope's brain
 * state plus live mic input:
 *
 *   - sleeping  → near-dark, faint breath
 *   - idle      → ambient cyan pulse + real-time radial bars from the mic
 *   - speaking  → green-tinted bars, synthesised rhythm (we don't have the
 *                 TTS PCM in the browser, so we drive a smooth sine envelope
 *                 while brainState='speaking')
 *   - thinking  → purple, orbiting particle arc instead of bars
 *
 * The mic stream is opened once on first mount and shared across modes;
 * bar amplitude in idle/speaking modes blends mic level (when present)
 * with the synthesised envelope so the user can see Hope and themselves
 * on the same canvas without modal switching.
 */

interface Props {
  brainState: BrainState;
  listeningPaused: boolean;
  size?: number;
}

const BAR_COUNT = 96;
const FFT_SIZE = 256; // 128 frequency bins, plenty for visual

// Per-state colour palette. Shared between the canvas draw loop (orb
// glow) and the label below the orb so the colour the user sees on
// "THINKING" matches the orb's purple, "SPEAKING" matches green, etc.
const PALETTE = {
  sleeping: { hue: 220, sat: 12, ring: 40 },
  idle: { hue: 192, sat: 70, ring: 55 },
  speaking: { hue: 145, sat: 70, ring: 60 },
  thinking: { hue: 268, sat: 72, ring: 60 },
} as const;

export function WaveformOrb({ brainState, listeningPaused, size = 320 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const rafRef = useRef<number | null>(null);
  // Mutable state read inside the animation loop without re-rendering.
  const stateRef = useRef({ brainState, listeningPaused });
  stateRef.current = { brainState, listeningPaused };

  // Open mic exactly once. We don't tear it down on unmount in dev because
  // hot-reload would otherwise re-prompt for permission every save.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
          },
        });
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        const Ctx =
          window.AudioContext ||
          (window as unknown as { webkitAudioContext: typeof AudioContext })
            .webkitAudioContext;
        const ctx = new Ctx();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = FFT_SIZE;
        analyser.smoothingTimeConstant = 0.7;
        src.connect(analyser);
        audioCtxRef.current = ctx;
        analyserRef.current = analyser;
      } catch (err) {
        // Permission denied or no mic — orb still runs in synthesised mode.
        // eslint-disable-next-line no-console
        console.warn('[WaveformOrb] mic unavailable:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Animation loop. Single rAF, branches on the latest brainState/paused.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    const baseRadius = size * 0.28;

    const freqBuf = new Uint8Array(FFT_SIZE / 2);
    let t0 = performance.now();

    const draw = (now: number) => {
      const elapsed = (now - t0) / 1000;
      const { brainState: bs, listeningPaused: paused } = stateRef.current;
      const tone = PALETTE[bs] ?? PALETTE.idle;

      ctx.clearRect(0, 0, size, size);

      // Read live mic level. RMS over the frequency buffer is a cheap proxy
      // for "loudness" — good enough for visual scaling.
      let micLevel = 0;
      const analyser = analyserRef.current;
      if (analyser && !paused) {
        analyser.getByteFrequencyData(freqBuf);
        let sum = 0;
        for (let i = 0; i < freqBuf.length; i++) sum += freqBuf[i];
        micLevel = sum / freqBuf.length / 255; // 0..1
      }

      // ---- Background glow halo --------------------------------------
      const haloPulse = 0.5 + Math.sin(elapsed * 1.6) * 0.1;
      const halo = ctx.createRadialGradient(cx, cy, baseRadius * 0.4, cx, cy, baseRadius * 2.1);
      halo.addColorStop(0, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring}%, ${0.35 * haloPulse})`);
      halo.addColorStop(1, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring}%, 0)`);
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, size, size);

      // ---- Inner core -------------------------------------------------
      const coreBreath = 0.92 + Math.sin(elapsed * 1.8) * 0.05;
      const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, baseRadius * 0.85 * coreBreath);
      core.addColorStop(0, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring + 18}%, 0.72)`);
      core.addColorStop(0.6, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring}%, 0.32)`);
      core.addColorStop(1, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring - 12}%, 0)`);
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, baseRadius * 0.85 * coreBreath, 0, Math.PI * 2);
      ctx.fill();

      if (bs === 'thinking') {
        // Orbiting arc: 3 staggered arcs sweeping around the core.
        for (let i = 0; i < 3; i++) {
          const phase = elapsed * (0.9 + i * 0.4) + (i * Math.PI * 2) / 3;
          const start = phase;
          const end = phase + Math.PI * 0.55;
          const r = baseRadius * (1.05 + i * 0.18);
          ctx.beginPath();
          ctx.lineWidth = 2.5;
          ctx.strokeStyle = `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring + 10 - i * 8}%, ${0.85 - i * 0.22})`;
          ctx.arc(cx, cy, r, start, end);
          ctx.stroke();
          // particle head
          const px = cx + Math.cos(end) * r;
          const py = cy + Math.sin(end) * r;
          ctx.beginPath();
          ctx.fillStyle = `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring + 25}%, ${0.95 - i * 0.25})`;
          ctx.arc(px, py, 3.2 - i * 0.6, 0, Math.PI * 2);
          ctx.fill();
        }
      } else {
        // Radial bars driven by mic + (when speaking) a synthesised envelope.
        // We mirror around 12 o'clock so the wave reads as bilateral.
        const speakEnvelope =
          bs === 'speaking'
            ? 0.45 + 0.35 * Math.abs(Math.sin(elapsed * 5.5)) + 0.18 * Math.sin(elapsed * 11)
            : 0;
        const idleFloor = bs === 'idle' ? 0.06 + Math.sin(elapsed * 2.4) * 0.03 : 0.04;
        for (let i = 0; i < BAR_COUNT; i++) {
          const angle = (i / BAR_COUNT) * Math.PI * 2 - Math.PI / 2;
          // Map bar index to a frequency bin, weighted toward lower
          // frequencies where speech energy lives.
          const bin = Math.floor((i / BAR_COUNT) * (freqBuf.length * 0.55));
          const micBar = freqBuf[bin] / 255;
          let amp = idleFloor + micBar * 0.85 + speakEnvelope * (0.6 + Math.sin(i * 0.5 + elapsed * 4) * 0.4);
          // Smooth bar envelope around the ring so abrupt frequency
          // spikes don't look spiky — apply a tiny moving average.
          if (i > 0) {
            const prevBin = Math.floor(((i - 1) / BAR_COUNT) * (freqBuf.length * 0.55));
            amp = amp * 0.7 + (idleFloor + (freqBuf[prevBin] / 255) * 0.85) * 0.3;
          }
          amp = Math.min(1.2, Math.max(0.04, amp));

          const inner = baseRadius * 0.95;
          const outer = inner + amp * size * 0.16;
          const x1 = cx + Math.cos(angle) * inner;
          const y1 = cy + Math.sin(angle) * inner;
          const x2 = cx + Math.cos(angle) * outer;
          const y2 = cy + Math.sin(angle) * outer;
          // Gradient on each bar: bright at the inner edge, fades out.
          const grad = ctx.createLinearGradient(x1, y1, x2, y2);
          grad.addColorStop(0, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring + 22}%, 0.95)`);
          grad.addColorStop(1, `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring + 5}%, 0)`);
          ctx.strokeStyle = grad;
          ctx.lineWidth = 2.2;
          ctx.lineCap = 'round';
          ctx.beginPath();
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
          ctx.stroke();
        }
      }

      // ---- Hairline rim (defines the orb edge) -----------------------
      ctx.beginPath();
      ctx.lineWidth = 1;
      ctx.strokeStyle = `hsla(${tone.hue}, ${tone.sat}%, ${tone.ring + 10}%, 0.4)`;
      ctx.arc(cx, cy, baseRadius * 0.95, 0, Math.PI * 2);
      ctx.stroke();

      // Mic-mute indicator: thin red ring when listening is paused.
      if (paused) {
        ctx.beginPath();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = 'hsla(0, 70%, 55%, 0.7)';
        ctx.arc(cx, cy, baseRadius * 1.05, 0, Math.PI * 2);
        ctx.stroke();
      }

      rafRef.current = requestAnimationFrame(draw);
    };
    rafRef.current = requestAnimationFrame(draw);

    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [size]);

  // Human-readable state text shown beneath the orb. Idle stays
  // intentionally bare so a quiet workspace is, well, quiet — but every
  // active state gets a clear, sentence-cased label so the user can tell
  // at a glance what Hope is doing during the silent window between her
  // ack and her reply.
  const stateLabel: Record<BrainState, string> = {
    sleeping: 'Sleeping',
    idle: '',
    thinking: 'Thinking…',
    speaking: 'Speaking',
  };
  const labelText = listeningPaused ? 'Muted' : stateLabel[brainState] ?? '';
  const labelTone = PALETTE[brainState] ?? PALETTE.idle;

  return (
    <div
      className="relative flex flex-col items-center justify-center"
      style={{ width: size, height: size + 36 }}
      aria-label={`Hope ${brainState}`}
    >
      <canvas
        ref={canvasRef}
        style={{
          width: size,
          height: size,
          filter: 'blur(0.3px)',
        }}
      />
      {labelText && (
        <div
          className="mt-2 text-sm font-medium tracking-[0.18em] uppercase"
          style={{
            color: `hsl(${labelTone.hue} ${labelTone.sat}% ${labelTone.ring}%)`,
            textShadow: '0 0 12px rgba(0,0,0,0.45)',
            opacity: 0.95,
          }}
        >
          {labelText}
        </div>
      )}
    </div>
  );
}
