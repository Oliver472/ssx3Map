# Zistenia z reálneho BAM.BIG (NTSC-U, `inspect` report 2026-10-09)

BAM.BIG sha1 `d28a53689d0d9ebab598da5265893f0bee368aa2`, na disku od LBA 865271, 113 078 400 bajtov.
Členy: `bam.sdb` (32 780), `bam.ssb` (109 051 904), `bam.phm`, `bam.psm`, `serial.txt`.

## bam.ssb
- 3 328 blokov, 159 chunkov. **Každý blok má rozsah presne 32 768 bajtov a začína na
  násobku 32 768.** RefPack stream je kratší a za ním sú nuly (výplň 3 až 31 959 B, medián 14).
  Hra teda výplň za stop príkazom toleruje. Upravený blok môže narásť až po 32 760 B.
- Dekódovaný blok má najviac 81 920 B (331 blokov je presne 81 920).
- Hlavička RefPack je vždy `0x10 FB` + 3 B. Každý stream končí stop príkazom.
- Náš enkodér je pri 24 vzorkách oproti originálu v mediáne o 313 B menší (rozsah −1285 až +34).
- Spolu 161 953 347 dekódovaných bajtov.

## bam.sdb
- 49 lokácií, 183 chunk infos (96 B), 159 sub-chunk infos (68 B); veľkosť súboru presne sedí.
- Tretí u32 záznamu lokácie je posledný chunk lokácie (vrátane). Chunky lokácie idú za sebou;
  každá lokácia má textúrové chunky (len druh 9/10) a posledný „hlavný“ chunk so všetkým ostatným.
- **Sub-chunk info: u32 na +4 = bajtový offset chunku v bam.ssb** (napr. chunk 1 → 131072,
  chunk 2 → 688128); u32 na +8 sa pri textúrových chunkoch rovná dekódovanej veľkosti chunku.
  Prvé u16 = počet záznamov, druhé = index chunku, za tým počty záznamov podľa druhu.
- Chunk info: bbox min/max (2× vec4), 48 B núl, potom 4 inty (−1, −1, index chunku, 0) – strom.

## Záznamy
- Terén (druh 1, 30 644 záznamov po 432 B):
  - `+0x40` 16× vec4 koeficienty; `coeff[15]` je bod P(0,0)
  - `+0x140` ohraničujúca guľa (stred xyz, polomer)
  - `+0x150` resource (rid<<8 | track)
  - `+0x156` s16 = index textúrového chunku (nie vlastný chunk)
  - `+0x158` bbox min, `+0x164` bbox max
  - `+0x170`, `+0x17C`, `+0x188`, `+0x194` štyri rohové body (SSX-Library má bbox a body v opačnom poradí)
  - `+0x1A0` textúra, `+0x1A2` light page
- Inštancie (druh 3): `+0x78` = resource, `+0x50` xyz = stred bboxu (`+0x60`/`+0x6C`), `+0x10` matica 4×4 (riadky).
- Textúry: 6 203 záznamov, 788 ID, všetky na tracku 255, kópie toho istého ID sú identické.
  Formáty: 4-bit 4 258×, 8-bit 1 771×, RGBA 174×.
- Painter (druh 15): jeden na lokáciu, skye a TRANSP majú prázdny (8 B).
  ARA1 má 9 fog payloadov, prvý: near 3000, far 10000, farba (0.70, 0.82, 1.00), density 2.
