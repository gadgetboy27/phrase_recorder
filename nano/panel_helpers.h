// Shared by the panel's lambdas (nano/panel.yaml). Kept next to panel.yaml; ESPHome includes it.
#pragma once
#include <string>
#include <cstdint>

// One colour per language, so a card/chip says which language it is in. Unknown codes → steel.
inline uint32_t lang_colour(const std::string &code) {
  if (code == "en")  return 0x4A7A68;   // sage (brief §10)
  if (code == "mi")  return 0x1B7F79;
  if (code == "ar")  return 0x8E5B2E;
  if (code == "es")  return 0xC2711D;
  if (code == "fa")  return 0x2F5DA8;
  if (code == "prs") return 0x4B6FB5;
  if (code == "hi")  return 0xB33A7A;
  if (code == "vi")  return 0x6A8F1F;
  if (code == "zh")  return 0xB8322E;
  if (code == "yue") return 0x9B2C5F;
  if (code == "ja")  return 0x5A4FA8;
  if (code == "ko")  return 0x1F6E9A;
  if (code == "lo")  return 0x7A5E1E;
  if (code == "pa")  return 0xA0522D;
  if (code == "sm")  return 0x2A7A9B;
  if (code == "tl")  return 0x8A6D0E;
  if (code == "to")  return 0x3E6B3E;
  return 0x6B7F9E;                      // steel
}

// Which of the panel's fonts can draw text in this language. 0 = Latin (Inter), 1 = Arabic script, -1 = none yet.
inline int lang_script(const std::string &code) {
  if (code == "ar" || code == "fa" || code == "prs") return 1;
  if (code == "hi" || code == "zh" || code == "yue" || code == "ja" || code == "ko" || code == "lo" || code == "pa") return -1;
  return 0;
}

// True if the text contains Arabic-script code points (U+0600–U+06FF, presentation forms).
inline bool has_arabic(const std::string &s) {
  for (size_t i = 0; i < s.size(); i++) {
    unsigned char c = s[i];
    if (c == 0xD8 || c == 0xD9 || c == 0xDA || c == 0xDB) return true;          // U+0600–U+06FF as UTF-8 lead bytes
    if (c == 0xEF && i + 1 < s.size() && (unsigned char) s[i + 1] >= 0xAD) return true;  // U+FB50–U+FEFF
  }
  return false;
}

// Show `text` on whichever twin can draw it (Latin font vs Arabic font); hide the other.
inline void set_text_any(lv_obj_t *latin, lv_obj_t *arabic, const std::string &text) {
  const bool ar = has_arabic(text);
  lv_obj_t *use = ar ? arabic : latin, *hide = ar ? latin : arabic;
  lv_label_set_text(use, text.c_str());
  lv_obj_clear_flag(use, LV_OBJ_FLAG_HIDDEN);
  lv_obj_add_flag(hide, LV_OBJ_FLAG_HIDDEN);
}

// Percent-encode for a query string.
inline std::string url_encode(const std::string &s) {
  static const char *hex = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : s) {
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += (char) c;
    else { out += '%'; out += hex[c >> 4]; out += hex[c & 15]; }
  }
  return out;
}

inline std::string hex6(uint32_t c) { char b[8]; snprintf(b, sizeof b, "%06X", (unsigned) c); return b; }
