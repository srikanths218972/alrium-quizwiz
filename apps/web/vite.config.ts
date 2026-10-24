/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

const target = process.env.API_PROXY_TARGET ?? `http://127.0.0.1:${process.env.API_PORT ?? '8000'}`;

function runtimeConfig(): Plugin {
  return {
    name: 'daily-qa-runtime-config',
    configureServer(server) {
      server.middlewares.use('/config.json', (_req, res) => {
        res.setHeader('content-type', 'application/json');
        res.end(JSON.stringify({
          appName: process.env.APP_NAME ?? 'ålrium - QuizWiz',
          tenantId: process.env.ENTRA_TENANT_ID ?? '',
          clientId: process.env.ENTRA_SPA_CLIENT_ID ?? '',
          apiScope: process.env.ENTRA_API_SCOPE ?? '',
          redirectUri: process.env.WEB_REDIRECT_URI ?? 'http://localhost:5173/',
        }));
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), runtimeConfig()],
  server: {
    host: '127.0.0.1',
    port: Number(process.env.WEB_PORT ?? 5173),
    strictPort: true,
    proxy: {
      '/api': target,
      '/health': target,
    },
  },
  build: { sourcemap: false },
  test: {
    environment: 'jsdom',
    setupFiles: ['./test/setup.ts'],
  },
});
