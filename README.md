# ssx3map – úpravy sveta SSX 3 (PS2)

Nástroj na úpravy pôvodnej mapy SSX 3 (NTSC-U, `SLUS_207.72`). Upravuje svet hry
v súbore `DATA/WORLDS/BAM.BIG`. Pracuje priamo s ISO obrazom a vyrobí upravenú
kópiu, ktorá sa dá spustiť v PCSX2.

> **Stav: experimentálne.** Všetko je otestované iba na syntetických dátach.
> Herné súbory tu nie sú a do repozitára ani nepatria. Či hra upravený svet
> naozaj načíta, sa ešte musí overiť v PCSX2.

## Ako to funguje

Celá hora je jeden stream `bam.ssb`: bloky `CBXS`/`CEND` komprimované cez
RefPack. Hra si podľa `bam.sdb` a iných tabuliek nájde, kde ktorý kus leží.
Tieto tabuľky ešte nie sú úplne pochopené, preto ich nástroj vôbec nemení:

* Upravené bloky zakóduje **presne na pôvodný počet bajtov**. Žiaden blok sa
  neposunie a `BAM.BIG` aj ISO majú rovnakú veľkosť.
* Najprv skúsi **spájanie**: pôvodné RefPack príkazy pred zmeneným miestom aj
  za ním ostanú bajt po bajte rovnaké a prekóduje sa iba malý úsek okolo
  úpravy. Ak to nestačí, prekóduje celý blok. Ak blok mal za koncom streamu
  výplň, použije ju.
* Každý zapísaný blok spätne dekóduje a porovná s požadovanými dátami.

Preto sú zatiaľ podporené iba úpravy, ktoré **nemenia veľkosť záznamov**:
farby, hodnoty, palety.

## Požiadavky

Python 3.8 alebo novší, bez ďalších knižníc. Príkazy spúšťaj z priečinka
tohto repozitára.

- **macOS / Linux:** píš `python3` namiesto `python`. Ak chýba, na Macu ho
  nainštaluje `xcode-select --install` (alebo `brew install python`).
- **Windows:** nainštaluj Python z python.org a zaškrtni „Add python.exe to
  PATH“. Potom funguje `python` (alebo `py`).

## 1. Report

Rozbaľ `SSX 3 (USA).7z`, aby si mal `.iso`. Potom spusti:

```
python -m ssx3map inspect "SSX 3 (USA).iso" > report.txt
```

Trvá to pár minút, lebo dekóduje celý svet. Report ukáže:
- ako sú bloky uložené (výplň, zarovnanie),
- či náš enkodér dokáže pôvodné bloky zabaliť do rovnakého miesta,
- čo je v tabuľkách SDB,
- hmlu všetkých lokácií,
- výsledok **skúšobnej úpravy** hmly a textúr. Tá prebehne len v pamäti.

Pošli mi `report.txt`. Podľa neho doladím, čo sa bez herných dát odhadnúť
nedalo.

## 2. Prvá úprava: červená hmla na Snow Jam

```
python -m ssx3map fog "SSX 3 (USA).iso" --location ARA1 --location A_ARA1 --location ARA1_B ^
    --color 1 0.2 0.2 --near 1000 --far 8000 -o "SSX3_hmla.iso"
```

(Na macOS/Linuxe nahraď `^` znakom `\`.) Pôvodné ISO ostane nedotknuté.
Spusti `SSX3_hmla.iso` v PCSX2 a daj si Single Event → Snow Jam.

Bez `-o` príkaz iba vypíše aktuálne hodnoty:

```
python -m ssx3map fog "SSX 3 (USA).iso" --all
```

Parametre hmly:
- `--color R G B`: farba, 0 až 1,
- `--near`, `--far`: začiatok a koniec hmly v centimetroch,
- `--density`: hustota,
- `--scale-distance X`: vynásobí near aj far.

Každá lokácia má vlastnú hmlu a hra ju prepína podľa toho, na akom teréne
jazdec stojí. Pri pretekoch sú nahraté aj spojovacie úseky (pri Snow Jam
`A_ARA1` a `ARA1_B`), preto ich je dobré upraviť spolu.

## 3. Prefarbenie textúr

Najprv si textúry vyexportuj ako PNG a nájdi ID tej, ktorú chceš zmeniť
(súbory sa volajú `tex_<ID>_<šírka>x<výška>.png`):

```
python -m ssx3map textures "SSX 3 (USA).iso" --location ARA1 --export textury
```

Potom ju prefarbi. Napríklad ružový sneh, keď je jeho ID 17:

```
python -m ssx3map tint "SSX 3 (USA).iso" --texture 17 --rgb 1 0.6 0.8 -o "SSX3_ruzovy.iso"
```

Textúry v SSX 3 sú spoločné pre celú horu, takže zmena sa prejaví všade,
kde sa textúra používa. `--location ARA1` namiesto `--texture` prefarbí
všetky textúry, ktoré lokácia používa. Pri paletových textúrach sa mení
paleta, takže sa zmenia aj všetky mipmapy.

## Ďalšie príkazy

```
python -m ssx3map info  "SSX 3 (USA).iso"                 # lokácie a ich chunky
python -m ssx3map list  "SSX 3 (USA).iso" --location ARA1 # záznamy (druh, rid, veľkosť, meno)
python -m ssx3map list  "SSX 3 (USA).iso" --kind 15       # len painter záznamy (hmla, slnko...)
```

Namiesto ISO môže byť vstupom aj samotný `BAM.BIG`. Výstup je potom `.BIG`.

## Kódy lokácií

| Kód | Trať | Kód | Trať |
|---|---|---|---|
| ARA1 | Snow Jam | ABC1 | Happiness |
| BRA2 | Metro-City | DBC2 | Ruthless |
| CRA3 | Ruthless Ridge | EBC3 | The Throne |
| DRA4 | Intimidator | ASS1 | R&B |
| ERA5 | Gravitude | DSS2 | Style Mile |
| ABA1 | Crow's Nest | ESS3 | Kick Doubt |
| CBA2 | Launch Time | BHP1 | The Junction |
| EBA3 | Much-2-Much | CHP2 | Schizophrenia |
| A–E | stanice | EHP3 | Perpendiculous |

Spojky sa volajú podľa lokácií, ktoré spájajú (napr. `A_ARA1`). Oblohy sú
`ASKY` až `ESKY`.

## Čo zatiaľ nejde

- Zmena veľkosti záznamov: nové objekty, iný počet trojuholníkov a podobne.
  Na to treba pochopiť a prepisovať `bam.sdb`.
- Posúvanie objektov a terénu. Polohy sú známe, ale kolízie (záznamy druhu
  12) sú samostatné a pri posune by ostali na pôvodnom mieste. Na tom je
  ďalší krok.
- Import vlastného PNG do textúry. Hotový je export a prefarbenie.

## Testy

```
python -m unittest -v
```

Testy si vyrobia vlastný syntetický `BAM.BIG` a ISO. Nepoužívajú žiadne
herné dáta.

## Zdroje a licencia

Znalosť formátov pochádza z týchto projektov:
- [SSX-Library](https://github.com/GlitcherOG/SSX-Library) od GlitcherOG (GPL-3.0),
- poznámok v [ssxdecomp/ssx3](https://github.com/ssxdecomp/ssx3) (`docs/notes`),
- [ssx-web](https://github.com/owattenmaker/ssx-web) (GPL-3.0).

Swizzle textúr a parsovanie painter záznamov vychádzajú z ich popisov a kódu.
Preto je tento nástroj licencovaný ako **GPL-3.0** (súbor `LICENSE`).
