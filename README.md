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

## Editor v prehliadači (three.js)

Najpohodlnejšie sa mapa upravuje v 3D editore:

```
python3 -m ssx3map editor "SSX 3 (USA).iso"
```

Otvorí sa stránka `http://127.0.0.1:8765/` s vybranou traťou v 3D. Terminál
nechaj bežať a editor vypneš klávesmi Ctrl+C. Stránka potrebuje internet, lebo
three.js sa sťahuje z CDN. Herné dáta ostávajú u teba, editor beží iba lokálne.

- **Herný vzhľad** (zapnutý od začiatku) kreslí trať tak, ako ju skladá hra.
  Používa skutočné modely objektov (stromy, budovy, zábradlia…) s textúrami
  a so zapečenými farbami a terén s textúrou aj tieňovou mapou (light page).
  Pridáva aj oblohu oblasti a hmlu z dát trate. Textúry a modely sa dekódujú
  z tvojho ISO, takže prvé načítanie trate trvá pár sekúnd. Bez herného
  vzhľadu sa terén zafarbí podľa výšky a objekty sú krabice.
- **Pozerať:** ľavé tlačidlo otáča, pravé posúva, koliesko približuje.
  Posuvník „Kamera na trati“ ťa prenesie na zvolený meter trate. Myšou nad
  terénom vidíš súradnice a vzdialenosť od štartu.
- **Terén:** vyber tvar (skok, kopec/jama, plošina, zarovnanie) a výšku.
  Pod myšou sa ukáže obrys oblasti, ktorú úprava zasiahne, a klik ju
  vykoná. Skok sa natočí po smere trate, smer sa dá doladiť poľom
  „otočenie“. Rozmery sa nastavia podľa terénu, alebo ich zadáš ručne.
- **Objekty:** klikni na objekt (strom, budovu…) a ťahaj šípky,
  prípadne použi tlačidlá ↑/↓ alebo Odstrániť (aj kláves Delete). Herné
  pomocné objekty (oranžové) sú bez zaškrtnutia „pomocné objekty“ skryté.
- **Späť** (Ctrl+Z) vráti poslednú úpravu.
- **Uložiť upravenú hru** zapíše nové ISO. Pôvodné ostane nedotknuté.

Úpravy na viacerých tratiach sa uložia naraz. Pri každej úprave platia tie
isté kontroly ako v príkazovom riadku.

Čo sa zatiaľ líši od hry:
- Hmla je len približná. Hra ju skladá v samostatnom prechode
  a editor ju napodobňuje obyčajnou hmlou.
- Chýba ScreenTint (farebný nádych obrazu) a odlesky.
- Priesvitné modely (svetlá, efekty) sa kreslia so zjednodušeným alfa
  testom a bez sčítavacieho miešania.

Spôsob, akým hra skladá farby terénu a modelov, je prevzatý z poznámok
projektu [ssx-web](https://github.com/owattenmaker/ssx-web) (overené tam
v emulátore).

## 4. Úpravy trate z príkazového riadku: terén a objekty

Terén v SSX 3 je zároveň kolízia: čo zdvihneš alebo znížiš, po tom sa aj jazdí.

**Mapa trate.** Najprv si nakresli trať zhora a otvor výsledné SVG v prehliadači.
Červená čiara je trasa so značkami metrov od štartu, zelené body sú objekty.
Keď myšou prejdeš nad plochu, ukážu sa súradnice.

```
python3 -m ssx3map map "SSX 3 (USA).iso" --location ARA1 -o snowjam.svg
```

**Terén.** Miesto zadáš jedným z týchto spôsobov:
- `--along 250`: 250 m od štartu po trase,
- `--side 6`: posun 6 m doprava od tohto bodu (záporné číslo je doľava),
- `--at X Y`: súradnice v metroch z mapy,
- `--start`: pri štarte,
- `--session K`: pri reset bode číslo K (čísla sú na mape).

Rozmery tvarov netreba zadávať. Nástroj ich nastaví podľa toho, aké veľké sú
v danom mieste pláty terénu (na Snow Jame okolo 20 m). Potom porovná výsledný
povrch so zamýšľaným tvarom. Ak by sa líšil o viac ako 15 % výšky, nástroj
úpravu bez `--force` odmietne. Taký tvar je na danú veľkosť plátov príliš
malý.

Tvary:
- `kicker`: skok, ktorý sa otáča po smere trate. Parametre `--height`,
  `--length`, `--width`, `--drop`.
- `bump`: kopec, a pri zápornej výške jama. Parametre `--height`, `--radius`.
- `plateau`: zdvihnutá alebo znížená plocha s rovným vrchom.
- `flatten`: zarovná okolie na výšku daného bodu. `--height` k nej pripočíta
  posun.

```
# 3 m skok, 250 m od štartu Snow Jamu (rozmery podľa terénu)
python3 -m ssx3map terrain "SSX 3 (USA).iso" --location ARA1 --along 250 ^
    --shape kicker --height 3 -o SSX3_skok.iso
```

Stromy a ostatné objekty na upravenom teréne sa posunú spolu s ním. Posunú
sa aj body štartu a resetu. Zábradlia (rails) v oblasti nástroj
zdvihne alebo zníži spolu s ním. Iba zábradlie dlhšie ako celá úprava ostane
na mieste, a vtedy nástroj vypíše upozornenie.

**Objekty.** Môžeš ich vypísať, posunúť alebo odstrániť. Odstránenie ich
presunie 1 km pod horu a spolu s nimi aj ich kolíziu.

```
python3 -m ssx3map objects "SSX 3 (USA).iso" --location ARA1 --along 400 --radius 25
python3 -m ssx3map objects "SSX 3 (USA).iso" --location ARA1 --along 400 --radius 25 --name tree --remove -o SSX3_bez_stromov.iso
```

Herné pomocné objekty (štart, triggery, resety, ploty režimov) sa bez
`--force` nemenia.

Na jedno ISO sa dá spraviť viac úprav za sebou: výstup jedného príkazu
použi ako vstup ďalšieho.

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
- Posúvanie AI trás spolu s terénom. AI jazdci jazdia po teréne, ich trasa sa
  však nemení.
- Osvetlenie terénu je zapečené v textúrach, takže nový kopec nemá
  vlastné tiene.
- Import vlastného PNG do textúry. Hotový je export a prefarbenie.

## Testy

```
python -m unittest -v
```

Test editora v prehliadači (potrebuje Node a balík `playwright`) je v
`tests/browser/editor_smoke.mjs`.

Testy si vyrobia vlastný syntetický `BAM.BIG` a ISO. Nepoužívajú žiadne
herné dáta.

## Zdroje a licencia

Znalosť formátov pochádza z týchto projektov:
- [SSX-Library](https://github.com/GlitcherOG/SSX-Library) od GlitcherOG (GPL-3.0),
- poznámok v [ssxdecomp/ssx3](https://github.com/ssxdecomp/ssx3) (`docs/notes`),
- [ssx-web](https://github.com/owattenmaker/ssx-web) (GPL-3.0).

Swizzle textúr, parsovanie painter záznamov, dekodér modelov MDR
(`ssx3map/models.py`, upravený z `tools/world_models.py` v ssx-web) a vzorce
pre herný vzhľad editora vychádzajú z ich popisov a kódu.
Preto je tento nástroj licencovaný ako **GPL-3.0** (súbor `LICENSE`).
