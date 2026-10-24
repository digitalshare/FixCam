// FixCam on FaceTime: a voice repair helper that looks through the caller's camera and shows
// its work on their screen.
//
// - Caller camera: on lines with caller capture enabled, the Chert bridge publishes the caller's
//   camera as `facetime-remote-video`. We keep the newest frames; look_at_camera sends them to the
//   FixCam server (Python, ../fixcam) for diagnosis with Claude + Keenable.
// - Fallback: the caller can also send a photo/video from the FixCam phone page (/phone).
// - Agent video: we stream the server's rendered "screen" (marked-up frames) as our camera.
import { fileURLToPath } from 'node:url';
import { AutoSubscribe, cli, defineAgent, llm, ServerOptions, voice } from '@livekit/agents';
import {
  LocalVideoTrack, RoomEvent, TrackKind, TrackSource, VideoBufferType, VideoFrame, VideoSource, VideoStream,
} from '@livekit/rtc-node';
import { greetOnBridgeReady } from './greet.mjs';
import { createSession, prewarm } from './pipeline.mjs';

const FIXCAM = (process.env.FIXCAM_URL || 'http://localhost:8000').replace(/\/$/, '');
const FPS = 10;
const CAPTURE_EVERY_MS = 400;   // how often we keep a caller frame
const SEND_FRAMES = 3;          // frames sent per look (covers ~1 s of camera motion)
const STALE_MS = 2500;          // a frame older than this is not "what the camera shows now"

const BASE = `You are FixCam, a friendly AI repair technician on a FaceTime call. Your words are spoken aloud.
You have seen NOTHING except what a CURRENT DIAGNOSIS section below says. Never describe or guess what the
caller's camera, photo or screen shows. Never invent symptoms, appliance models, error codes or part numbers:
only repeat facts the caller said or that appear in CURRENT DIAGNOSIS.
If they would rather describe the problem, call ask_fixcam with exactly what they said, in their own words.
Reply in at most two short spoken sentences. Plain speech only: no lists, markdown or headings.
Give one step at a time and wait for them to say they're done before the next.
Never read URLs or part numbers character by character. If something is dangerous (mains power, gas,
refrigerant), say so first. Be clear that you are an AI.`;

const CAMERA_ON = `
CAMERA: the caller's FaceTime camera is connected. When they point it at something, or ask whether you can
see it, call look_at_camera (pass what they said). It takes a snapshot and an expert analyses it in about
twenty seconds; you are told when results arrive. Ask them to hold the camera steady and close for a moment.
They can also send a photo from the FixCam link on their screen.`;

const CAMERA_OFF = `
CAMERA: no live picture from the caller's camera has arrived yet. If they ask you to look at something, still
call look_at_camera: it checks the camera and tells you what to say. They can also open the FixCam link on
their FaceTime screen and send a photo or short video; you are told when it arrives. If they say they
are sending one, say you'll wait, and do nothing else.`;

function buildInstructions(cameraLive, plan) {
  let text = BASE + (cameraLive ? CAMERA_ON : CAMERA_OFF);
  if (plan) {
    const source = plan.from_photo ? "a snapshot of the caller's camera or photo, plus web research"
      : "the caller's spoken description and web research (you have NOT seen any image)";
    text += `

CURRENT DIAGNOSIS (from ${source}):
${plan.summary}
${plan.safety ? `SAFETY: ${plan.safety}\n` : ''}${plan.needs_professional ? 'This likely needs a professional if the steps do not work.\n' : ''}${plan.parts?.length ? `Parts that may be needed: ${plan.parts.join(', ')}\n` : ''}
FIX STEPS (call show_step(n) as you begin each one; explain each step briefly in your own words, using only what it says):
${plan.steps.map(s => `${s.n}. ${s.instruction}`).join('\n')}`;
  }
  return text;
}

async function api(path, body) {
  const res = await fetch(`${FIXCAM}${path}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res;
}

/** Keeps the newest few frames of the caller's camera. */
class CameraFeed {
  frames = [];     // [{data, width, height, rotation, at}]
  muted = false;
  track = null;

  get live() {
    return !this.muted && this.frames.length > 0 && Date.now() - this.frames.at(-1).at < STALE_MS;
  }

  async consume(track) {
    this.track = track;
    this.frames = [];
    let last = 0;
    for await (const { frame, rotation } of new VideoStream(track)) {
      if (this.track !== track) break;  // replaced by a newer track
      const now = Date.now();
      if (now - last < CAPTURE_EVERY_MS) continue;
      last = now;
      const rgba = frame.convert(VideoBufferType.RGBA);
      this.frames.push({ data: rgba.data, width: rgba.width, height: rgba.height, rotation, at: now });
      if (this.frames.length > SEND_FRAMES) this.frames.shift();
      this.preview(this.frames.at(-1));
    }
  }

  /** Forward about one frame per second to the dashboard's live camera view (fire and forget). */
  preview(f) {
    if (this.previewing || f.at - (this.lastPreview || 0) < 1000) return;
    this.previewing = true;
    this.lastPreview = f.at;
    const qs = new URLSearchParams({ width: String(f.width), height: String(f.height), rotation: String(f.rotation || 0) });
    fetch(`${FIXCAM}/live/camera?${qs}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: Buffer.from(f.data) })
      .catch(() => {})
      .finally(() => { this.previewing = false; });
  }

  /** Newest frames that share the latest frame's size (resolution can change mid-call). */
  snapshot() {
    const last = this.frames.at(-1);
    return this.frames.filter(f => f.width === last.width && f.height === last.height);
  }
}

export default defineAgent({
  prewarm,
  entry: async ctx => {
    // Subscribe to video too, and register handlers before the caller's tracks arrive.
    const camera = new CameraFeed();
    let onCameraChange = () => {};
    ctx.room.on(RoomEvent.TrackSubscribed, (track, pub, participant) => {
      if (track.kind !== TrackKind.KIND_VIDEO || !participant.identity.startsWith('chert-bridge-')) return;
      console.log(`[fixcam] caller camera connected (${pub.name || 'video'})`);
      fetch(`${FIXCAM}/live/camera?status=connected`, { method: 'POST' }).catch(() => {});
      camera.muted = false;
      camera.consume(track).catch(e => console.log(`[fixcam] camera stream ended: ${e}`));
      setTimeout(() => onCameraChange(), 1500);  // wait for the first frames
    });
    ctx.room.on(RoomEvent.TrackMuted, (pub, participant) => {
      if (pub.kind === TrackKind.KIND_VIDEO && participant.identity?.startsWith('chert-bridge-')) {
        camera.muted = true;
        fetch(`${FIXCAM}/live/camera?status=muted`, { method: 'POST' }).catch(() => {});
        onCameraChange();
      }
    });
    ctx.room.on(RoomEvent.TrackUnmuted, (pub, participant) => {
      if (pub.kind === TrackKind.KIND_VIDEO && participant.identity?.startsWith('chert-bridge-')) { camera.muted = false; setTimeout(() => onCameraChange(), 1500); }
    });
    await ctx.connect(undefined, AutoSubscribe.SUBSCRIBE_ALL);
    const caller = await ctx.waitForParticipant();
    await api('/live/start', { callId: ctx.room.name }).catch(e => console.log(`[fixcam] server unreachable: ${e}`));

    // --- Agent video: stream the FixCam server's screen as this agent's camera.
    let frameBuf = null, width = 960, height = 540, screenVersion = -1;
    const refreshScreen = async () => {
      try {
        const res = await api('/live/screen');
        width = Number(res.headers.get('x-width'));
        height = Number(res.headers.get('x-height'));
        screenVersion = Number(res.headers.get('x-version'));
        frameBuf = new Uint8Array(await res.arrayBuffer());
      } catch (e) { console.log(`[fixcam] screen fetch failed: ${e}`); }
    };
    await refreshScreen();
    const source = new VideoSource(width, height);
    const track = LocalVideoTrack.createVideoTrack('fixcam-screen', source);
    const pushFrame = () => frameBuf && source.captureFrame(new VideoFrame(frameBuf, width, height, VideoBufferType.RGBA));
    pushFrame();
    await ctx.room.localParticipant.publishTrack(track, { source: TrackSource.SOURCE_CAMERA });
    const videoTimer = setInterval(pushFrame, 1000 / FPS);

    // --- Voice agent with tools backed by the FixCam server.
    let plan = null, cameraLive = false;
    const tools = [
      llm.tool({
        name: 'look_at_camera',
        description: "Take a snapshot of what the caller's FaceTime camera shows right now and have the expert diagnose it. ALWAYS use this (never ask_fixcam) when they ask you to look at, see or check something, say 'this', or point their camera. If the camera is unavailable it tells you what to say instead.",
        parameters: { type: 'object', properties: { what_they_said: { type: 'string', description: "What the caller said about the problem, in their own words (empty if nothing)" } }, required: ['what_they_said'] },
        execute: async ({ what_they_said }) => {
          if (!camera.live) {
            console.log(`[fixcam] look_at_camera: no live frames (track=${camera.track ? 'yes' : 'no'}, muted=${camera.muted}, frames=${camera.frames.length})`);
            return "No live camera picture right now. Ask them to make sure their camera is on and pointed at the problem, or to send a photo from the FixCam link on their screen.";
          }
          const frames = camera.snapshot();
          const { width: w, height: h, rotation } = frames.at(-1);
          const body = Buffer.concat(frames.map(f => Buffer.from(f.data)));
          const qs = new URLSearchParams({ note: what_they_said || '', count: String(frames.length), width: String(w), height: String(h), rotation: String(rotation || 0) });
          const res = await fetch(`${FIXCAM}/live/frames?${qs}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body });
          const r = await res.json();
          console.log(`[fixcam] look_at_camera sent ${frames.length} frame(s) ${w}x${h} rot=${rotation}: ${JSON.stringify(r)}`);
          if (r.ok) return 'Snapshot taken; the analysis takes about twenty seconds. Tell them to hold on while you take a look, without guessing what it shows.';
          return r.error === 'busy' ? 'Still analysing the previous snapshot; ask them to hold on a moment.' : `Snapshot failed: ${r.error}`;
        },
      }),
      llm.tool({
        name: 'show_step',
        description: "Show the caller the given fix step on their screen. Call it whenever you start explaining a step.",
        parameters: { type: 'object', properties: { number: { type: 'integer', description: 'Step number, starting at 1' } }, required: ['number'] },
        execute: async ({ number }) => {
          const r = await (await api('/live/show', { step: number })).json();
          await refreshScreen();
          return r.ok ? `Now showing step ${number}: ${r.instruction}` : r.error;
        },
      }),
      llm.tool({
        name: 'ask_fixcam',
        description: "Ask the FixCam expert system (web research + repair knowledge) about a problem described in WORDS only. Never use it when the caller asks you to look at, see or check something, or points their camera: use look_at_camera for that. Never use it when they say they are sending a photo or video.",
        parameters: { type: 'object', properties: { question: { type: 'string', description: "What the caller actually said, in their own words. Never add details they did not say." } }, required: ['question'] },
        execute: async ({ question }) => {
          const r = await (await api('/live/ask', { question })).json();
          if (!r.async) return r.answer;
          return r.started
            ? 'Research started; results arrive in about 20 seconds. Tell the caller you are looking it up.'
            : 'Already working on a diagnosis; tell the caller it will be ready in a moment.';
        },
      }),
    ];
    const agent = new voice.Agent({ instructions: buildInstructions(false, null), tools });
    const updateInstructions = () => agent.updateInstructions(buildInstructions(cameraLive, plan));
    onCameraChange = () => {
      if (camera.live === cameraLive) return;
      cameraLive = camera.live;
      console.log(`[fixcam] caller camera ${cameraLive ? 'live' : 'unavailable'}`);
      updateInstructions();
    };
    const cameraCheck = setInterval(() => onCameraChange(), 2000);

    const session = createSession(ctx);
    await session.start({
      room: ctx.room,
      agent,
      record: false,
      inputOptions: {
        participantIdentity: caller.identity,
        audioEnabled: true,
        videoEnabled: false,  // frames go to look_at_camera, not to the voice LLM
        textEnabled: false,
        closeOnDisconnect: true,
        deleteRoomOnClose: false,
      },
      outputOptions: { audioEnabled: true, transcriptionEnabled: false },
    });

    // Speak only after FaceTime is ready: talking earlier can drop the call.
    let greeted = false;
    const stopGreeting = greetOnBridgeReady(ctx.room, () => {
      greeted = true;
      onCameraChange();
      session.generateReply({
        instructions: camera.live
          ? "Greet the caller in one short sentence: you're FixCam, an AI repair helper; ask them to point their camera at what's broken and tell you what's going on."
          : "Greet the caller in one short sentence: you're FixCam, an AI repair helper; ask them to snap a photo of what's broken using the FixCam link on screen, or just tell you what's wrong.",
      });
    });

    // --- Poll the server for diagnosis progress and screen changes.
    let after = 0;
    const poll = setInterval(async () => {
      try {
        const { events, screenVersion: v } = await (await api(`/live/events?after=${after}`)).json();
        if (v !== screenVersion) await refreshScreen();
        if (!greeted || !events.length) return;  // hold events until the greeting
        after = events.at(-1).seq;
        // Only the newest result matters; a burst can hold several (e.g. photos sent before the call).
        const result = events.filter(e => e.type === 'result').at(-1);
        const latest = result ?? events.at(-1);
        console.log(`[fixcam] events ${events.map(e => e.type).join(',')} -> handling ${latest.type}`);
        if (latest.type === 'processing' && latest.source === 'phone') {
          session.generateReply({ instructions: `The caller just sent a photo or video${latest.note ? ` with the note "${latest.note}"` : ''}. In one short sentence, say you got it and you're taking a look; it takes about twenty seconds.` });
        } else if (latest.type === 'result' && latest.steps.length) {
          plan = latest;
          await updateInstructions();
          session.generateReply({ instructions: 'The diagnosis just arrived and step 1 is on their screen. In two short sentences: say what is wrong in plain words, then explain step 1. Then ask them to tell you when it is done.' });
        } else if (latest.type === 'result') {
          session.generateReply({ instructions: `FixCam replied: "${latest.summary}". Relay that to the caller briefly.` });
        } else if (latest.type === 'error') {
          session.generateReply({ instructions: 'Something went wrong analysing that. Apologise briefly and ask them to try again.' });
        }
      } catch (e) { /* server restarting; keep polling */ }
    }, 1000);

    ctx.addShutdownCallback(async () => {
      await api('/live/end', {}).catch(() => {});  // keeps the diagnosis; dashboard shows "idle"
      clearInterval(poll);
      clearInterval(videoTimer);
      clearInterval(cameraCheck);
      stopGreeting();
      camera.track = null;
      await session.close();
      await track.close();
      await source.close();
    });
  },
});

cli.runApp(new ServerOptions({
  agent: fileURLToPath(import.meta.url),
  agentName: process.env.AGENT_NAME || 'student-agent',
}));
