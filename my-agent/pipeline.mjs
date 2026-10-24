// Voice pipeline without OpenAI: LiveKit Inference speech (billed to the LiveKit project in
// .env.local) + a Nebius Token Factory LLM through the OpenAI-compatible plugin.
import { voice } from '@livekit/agents';
import * as openai from '@livekit/agents-plugin-openai';
import * as silero from '@livekit/agents-plugin-silero';

// Qwen3-235B followed "don't invent what you can't see" and "two short sentences" best in our
// tests; Nemotron was as fast but fabricated a caller request.
export const NEBIUS_MODEL = process.env.NEBIUS_MODEL || 'Qwen/Qwen3-235B-A22B-Instruct-2507';

export async function prewarm(proc) {
  proc.userData.vad = await silero.VAD.load();
}

/** @param {import('@livekit/agents').JobContext} ctx */
export function createSession(ctx) {
  if (!process.env.NEBIUS_API_KEY) throw new Error('NEBIUS_API_KEY is not set');
  return new voice.AgentSession({
    vad: ctx.proc.userData.vad,
    stt: process.env.STT_MODEL || 'deepgram/nova-3',
    llm: new openai.LLM({
      model: NEBIUS_MODEL,
      baseURL: 'https://api.tokenfactory.nebius.com/v1/',
      apiKey: process.env.NEBIUS_API_KEY,
    }),
    tts: process.env.TTS_MODEL || 'cartesia/sonic-3',
  });
}
