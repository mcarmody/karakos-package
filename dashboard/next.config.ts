import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  env: {
    // Operator-set display name (lib/branding.ts).
    NEXT_PUBLIC_KARAKOS_NAME: process.env.KARAKOS_NAME || "",
  },
  distDir: process.env.NEXT_DIST_DIR_OVERRIDE || ".next",
  // /agents is the live all-agents page. /agents/live was its first address;
  // /hive and /conversations were retired into it. Temporary (307) so a later
  // reuse of a path is not stuck behind a cached permanent redirect.
  async redirects() {
    return [
      { source: "/agents/live", destination: "/agents", permanent: false },
      { source: "/hive", destination: "/agents", permanent: false },
      { source: "/conversations", destination: "/agents", permanent: false },
    ];
  },
};

export default nextConfig;
