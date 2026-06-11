/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // App Router is the default in 14; experimental flags kept minimal.
  experimental: {
    typedRoutes: true,
  },
  // Docker mode produces a small standalone server bundle for prod images.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
};

export default nextConfig;
