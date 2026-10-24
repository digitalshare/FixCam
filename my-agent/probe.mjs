// Probe: does the caller's FaceTime camera reach the LiveKit room?
// Same as agent.mjs, but subscribes to every track, logs what the Chert bridge publishes,
// and saves a caller video frame every 3 s to ../data/facetime/<room>/ as .ppm images.
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { AutoSubscribe, cli, defineAgent, ServerOptions, voice } from '@livekit/agents';
import { RoomEvent, TrackKind, VideoBufferType, VideoStream } from '@livekit/rtc-node';
import { greetOnBridgeReady } from './greet.mjs';
import { createSession, prewarm } from './pipeline.mjs';

const instructions = `You are FixCam, a friendly AI repair helper on a FaceTime call.
This is a connection test. Keep replies to one short sentence. Be clear that you are an AI.`;
const greeting = 'Greet the caller in one short sentence: say you are FixCam and ask them to point the camera at what is broken.';

const OUT = fileURLToPath(new URL('../data/facetime/', import.meta.url));

async function saveFrames(track, dir) {
  mkdirSync(dir, { recursive: true });
  let last = 0, n = 0;
  for await (const { frame } of new VideoStream(track)) {
    const now = Date.now();
    if (now - last < 3000) continue;
    last = now;
    const rgb = frame.convert(VideoBufferType.RGB24);
    const header = Buffer.from(`P6\n${rgb.width} ${rgb.height}\n255\n`);
    writeFileSync(`${dir}/frame_${String(n++).padStart(3, '0')}.ppm`, Buffer.concat([header, Buffer.from(rgb.data)]));
    console.log(`[probe] saved caller frame ${n} (${rgb.width}x${rgb.height})`);
  }
}

export default defineAgent({
  prewarm,
  entry: async ctx => {
    await ctx.connect(undefined, AutoSubscribe.SUBSCRIBE_ALL);
    console.log(`[probe] job metadata: ${ctx.job.metadata || '(none)'}`);
    ctx.room.on(RoomEvent.TrackPublished, (pub, p) =>
      console.log(`[probe] ${p.identity} published ${pub.kind === TrackKind.KIND_VIDEO ? 'VIDEO' : 'audio'} (${pub.source})`));
    ctx.room.on(RoomEvent.TrackSubscribed, (track, pub, p) => {
      console.log(`[probe] subscribed ${p.identity} ${track.kind === TrackKind.KIND_VIDEO ? 'VIDEO' : 'audio'}`);
      if (track.kind === TrackKind.KIND_VIDEO) {
        saveFrames(track, `${OUT}${ctx.room.name}`).catch(e => console.log(`[probe] video stream ended: ${e}`));
      }
    });

    const caller = await ctx.waitForParticipant();
    const tracks = [...caller.trackPublications.values()].map(t => (t.kind === TrackKind.KIND_VIDEO ? 'VIDEO' : 'audio'));
    console.log(`[probe] caller ${caller.identity} joined with tracks: ${tracks.join(', ') || '(none yet)'}`);

    const session = createSession(ctx);
    ctx.addShutdownCallback(() => session.close());
    await session.start({
      room: ctx.room,
      agent: new voice.Agent({ instructions }),
      record: false,
      inputOptions: {
        participantIdentity: caller.identity,
        audioEnabled: true,
        videoEnabled: false,
        textEnabled: false,
        closeOnDisconnect: true,
        deleteRoomOnClose: false,
      },
      outputOptions: { audioEnabled: true, transcriptionEnabled: false },
    });
    const stopGreeting = greetOnBridgeReady(ctx.room, () => session.generateReply({ instructions: greeting }));
    ctx.addShutdownCallback(async () => stopGreeting());
  },
});

cli.runApp(new ServerOptions({
  agent: fileURLToPath(import.meta.url),
  agentName: process.env.AGENT_NAME || 'student-agent',
}));
