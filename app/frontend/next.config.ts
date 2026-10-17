import type { NextConfig } from 'next';
const config: NextConfig = {turbopack: {root: process.cwd()}, outputFileTracingRoot: process.cwd(), async rewrites() { return [{ source: '/api/v1/:path*', destination: 'http://127.0.0.1:8000/api/v1/:path*' }]; }};
export default config;
