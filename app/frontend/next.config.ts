import type { NextConfig } from 'next';
const apiOrigin=process.env.SHOCKWAVE_API_URL||'http://127.0.0.1:8000';
const config: NextConfig = {turbopack: {root: process.cwd()}, outputFileTracingRoot: process.cwd(), async rewrites() { return [{ source: '/api/v1/:path*', destination: `${apiOrigin}/api/v1/:path*` }]; }};
export default config;
