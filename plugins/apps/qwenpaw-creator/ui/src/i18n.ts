import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./locales/zh.json";
import en from "./locales/en.json";

const resources = {
  zh: { translation: zh },
  en: { translation: en },
};

i18n.use(initReactI18next).init({
  resources,
  lng: (typeof localStorage !== "undefined" ? localStorage.getItem("language") : null) || "en",
  fallbackLng: "en",
  supportedLngs: Object.keys(resources),
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
