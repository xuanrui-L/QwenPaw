import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./locales/zh.json";
import en from "./locales/en.json";

const resources = {
  zh: { translation: zh },
  en: { translation: en },
};

// Storage access is defensive: some jsdom/vitest combinations expose a
// localStorage global whose methods are not callable functions.
const storedLanguage = (() => {
  try {
    const storage = globalThis.localStorage as Storage | undefined;
    return typeof storage?.getItem === "function"
      ? storage.getItem("language")
      : null;
  } catch {
    return null;
  }
})();

i18n.use(initReactI18next).init({
  resources,
  lng:
    storedLanguage ||
    (typeof navigator !== "undefined" ? navigator.language : null) ||
    "en",
  load: "languageOnly",
  fallbackLng: "en",
  supportedLngs: Object.keys(resources),
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
