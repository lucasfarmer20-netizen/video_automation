import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// tsconfig `paths` are resolved natively (resolve.tsconfigPaths); the
// vite-tsconfig-paths plugin the Next.js guide still lists warns that it is
// superseded on this Vite, so it is not used.
export default defineConfig({
  plugins: [react()],
  resolve: { tsconfigPaths: true },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
