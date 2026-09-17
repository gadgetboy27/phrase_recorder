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
