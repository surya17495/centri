/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Single accent color for the minimal dark aesthetic.
        accent: {
          DEFAULT: "#6366f1",
          muted: "#4f46e5",
        },
        surface: {
          0: "#0a0a0b",
          1: "#121214",
          2: "#1a1a1d",
          3: "#242428",
        },
        ink: {
          DEFAULT: "#e7e7ea",
          muted: "#9a9aa3",
          faint: "#6b6b74",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
