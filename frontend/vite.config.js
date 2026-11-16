import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` -> proxy al gateway de docker-compose (localhost:8080) o de un ambiente (localhost:30080)
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/graphql": process.env.API || "http://localhost:8080" } },
});
