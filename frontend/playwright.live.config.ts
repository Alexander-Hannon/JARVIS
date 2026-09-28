import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// REAL-BACKEND browser test: no page.route mocks. Requires `jarvis serve` (serving frontend/dist) on 127.0.0.1:8765
// with a FRESH data dir, plus `jarvis worker`. Chromium's fake capture device plays a real speech WAV into getUserMedia,
// so the path browser mic → MediaRecorder → upload → local faster-whisper is exercised end to end.
// Providers are not connected; the test asserts truthful "not connected" blockers instead of fake success.
const wav = path.resolve(here, '../backend/tests/fixtures/audio/scenario1.wav');
export default defineConfig({
  testDir: './e2e-live',
  timeout: 120_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.JARVIS_URL ?? 'http://127.0.0.1:8765',
    permissions: ['microphone'],
    launchOptions: {
      args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', `--use-file-for-fake-audio-capture=${wav}`],
    },
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], permissions: ['microphone'] } },
  ],
});
