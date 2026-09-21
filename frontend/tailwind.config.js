import colors from 'tailwindcss/colors';
import typography from '@tailwindcss/typography';

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // 品牌主色与语义色 tokens（消费方一律用 brand/success/warning/danger，
        // 不再直接写 indigo-600 / emerald-500 等裸色名）
        brand: { ...colors.indigo, DEFAULT: colors.indigo[600] },
        success: { ...colors.emerald, DEFAULT: colors.emerald[600] },
        warning: { ...colors.amber, DEFAULT: colors.amber[500] },
        danger: { ...colors.red, DEFAULT: colors.red[600] },
      },
      borderRadius: {
        DEFAULT: '0.5rem',
      },
    },
  },
  plugins: [typography],
}
