/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Single accent, used sparingly: primary actions + live state only.
        accent: {
          DEFAULT: "#7c7cf4",
          hover: "#8f8ff7",
          muted: "#5b5be0",
        },
        surface: {
          0: "#0b0b0e",
          1: "#111114",
          2: "#19191e",
          3: "#232329",
        },
        ink: {
          DEFAULT: "#ececf1",
          dim: "#a0a0ab",
          muted: "#8b8b96",
          faint: "#5d5d68",
        },
        line: {
          DEFAULT: "rgba(255,255,255,0.07)",
          strong: "rgba(255,255,255,0.13)",
        },
      },
      fontFamily: {
        sans: [
          "Inter Variable",
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      boxShadow: {
        composer:
          "0 0 0 1px rgba(255,255,255,0.06), 0 8px 24px rgba(0,0,0,0.45)",
        card: "0 1px 0 rgba(255,255,255,0.04) inset, 0 1px 2px rgba(0,0,0,0.3)",
        drawer: "-16px 0 48px rgba(0,0,0,0.5)",
      },
      animation: {
        "rise-in": "rise-in 0.25s cubic-bezier(0.21, 1.02, 0.73, 1) both",
        "fade-in": "fade-in 0.2s ease-out both",
        "slide-in-right": "slide-in-right 0.28s cubic-bezier(0.21, 1.02, 0.73, 1) both",
      },
      keyframes: {
        "rise-in": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "slide-in-right": {
          from: { opacity: "0", transform: "translateX(24px)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
      },
    },
  },
  plugins: [],
};
