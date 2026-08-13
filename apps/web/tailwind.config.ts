import type { Config } from "tailwindcss";

export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#0b1220", soft: "#1a2233" },
        accent: { DEFAULT: "#2563eb", soft: "#dbeafe" },
        fresh: { DEFAULT: "#059669", soft: "#d1fae5" },
      },
    },
  },
  plugins: [],
} satisfies Config;
