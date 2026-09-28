import { defineConfig } from '@playwright/test'

// Acceptance tests against the deployed intranet stack; see e2e/platform.spec.ts
// for the required environment and docs/operations.md for the runbook.
export default defineConfig({
  testDir: './e2e',
  timeout: 300_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8080',
    trace: 'retain-on-failure',
  },
})
