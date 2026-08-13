/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // Self-contained server bundle, so the runtime image carries only what it needs
  // instead of the whole node_modules tree.
  output: "standalone",

  // API_BASE_URL is deliberately NOT declared here. Next's `env` key inlines values at
  // *build* time: with it, `http://127.0.0.1:8765/api/v1` was compiled into
  // .next/server/chunks, so one image could never be pointed at the `api` service in
  // another environment. Every call site is server-side (see src/lib/api.ts), so reading
  // process.env at runtime works and keeps one image deployable anywhere.
  //
  // The browser still never learns the API host: no client component reads it, and it is
  // not prefixed NEXT_PUBLIC_, so it cannot reach the client bundle.
};
export default nextConfig;
