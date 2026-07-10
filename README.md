# PODA data – Home Assistant integrace

Vlastní integrace pro Home Assistant, která stahuje měsíční vyúčtování mobilních
čísel z klientské zóny **[klient.poda.cz](https://klient.poda.cz/mobily/vyuctovani)**
a vytvoří pro každé číslo 6 senzorů:

| Senzor | Popis | Jednotka |
|---|---|---|
| Volání – doba | Součet délky všech hovorů za aktuální měsíc | min |
| Volání – cena | Součet ceny všech hovorů | Kč |
| SMS – počet | Počet odeslaných SMS | SMS |
| SMS – cena | Součet ceny SMS | Kč |
| Data – vyčerpáno | Vyčerpaná data z FUP balíčku | MB |
| Data – limit | Velikost datového balíčku (FUP) | MB |

Data o voláních, SMS i datových přenosech se stahují přes odkazy
**„Stáhnout jako CSV"** na stránce vyúčtování a integrace si je sama sečte po
číslech. Z HTML tabulky se navíc bere jméno tarifu a FUP limit (ten v CSV není).

Formáty všech tří CSV exportů jsou **ověřené na reálných datech** a parsování
je otestované proti skutečnému vyúčtování (výsledky se shodují do desetin):

| Export | Sloupce |
|---|---|
| Volání | `src, dst, start, billsec, price, free_units` |
| SMS a MMS | `src, dst, type, start, price, free_units` |
| Datové přenosy | `src, start, kb, price, zone` (jeden řádek = jedna datová session, `kb` se sčítá a převádí na MB) |

Pokud PODA formát CSV exportů v budoucnu změní, integrace to pozná (zaloguje
warning "could not identify... columns") – parsery jsou napsané obecně
(hledají sloupec podle klíčových slov v názvu), takže menší změny by měly
přežít bez zásahu.

## Instalace přes HACS

1. V Home Assistant otevři **HACS → Integrace → tři tečky vpravo nahoře →
   Vlastní repozitáře (Custom repositories)**.
2. Vlož URL tvého GitHub repozitáře (např. `https://github.com/jerzik/PODA-data`),
   kategorie **Integration**, klikni **Přidat**.
3. Najdi „PODA data" v seznamu HACS integrací a klikni **Stáhnout (Download)**.
4. Restartuj Home Assistant.
5. Přejdi na **Nastavení → Zařízení a služby → Přidat integraci** a vyhledej
   „PODA data". Zadej přihlašovací jméno a heslo do klient.poda.cz.

Po dokončení průvodce se automaticky vytvoří zařízení pro každé nalezené
telefonní číslo se 6 senzory.

### Interval aktualizace

Výchozí interval je 6 hodin (vyúčtování se stejně mění jen postupně během
měsíce). Dá se změnit v **Nastavení → Zařízení a služby → PODA data →
Konfigurovat**.

## Jak nahrát na GitHub a zprovoznit aktualizace přes HACS

```bash
cd poda-data
git init
git add .
git commit -m "Initial release of PODA data integration"
git branch -M main
git remote add origin https://github.com/<tvuj-ucet>/PODA-data.git
git push -u origin main
```

Poté na GitHubu:

1. Jdi na **Releases → Draft a new release**.
2. Tag verze musí odpovídat `"version"` v `custom_components/poda_data/manifest.json`
   (např. `v0.1.0`). **HACS detekuje aktualizace podle tagovaných releasů**,
   ne podle commitů do `main` – takže při každé nové verzi:
   - zvyš `version` v `manifest.json`,
   - commitni a pushni,
   - vytvoř nový GitHub Release se stejným tagem.
3. Repozitář musí mít v **Settings → General** povolené **Issues** (HACS to
   vyžaduje u community repozitářů) a musí projít workflow validací
   (`.github/workflows/validate.yml` – HACS action + hassfest), jinak ho HACS
   ve výchozím vyhledávání nenabídne (funguje ale i tak jako custom repository).

## Návrh grafů / dashboardu

### Jednoduchý přehled (vestavěné karty, bez doplňků)

```yaml
type: entities
title: PODA – 734714008
entities:
  - entity: sensor.poda_734714008_volani_doba
  - entity: sensor.poda_734714008_volani_cena
  - entity: sensor.poda_734714008_sms_pocet
  - entity: sensor.poda_734714008_sms_cena
  - entity: sensor.poda_734714008_data_vycerpano
  - entity: sensor.poda_734714008_data_limit
```

### Graf čerpání dat (vestavěná karta `statistics-graph`)

```yaml
type: statistics-graph
title: Čerpání dat – 734714008
entities:
  - sensor.poda_734714008_data_vycerpano
  - sensor.poda_734714008_data_limit
stat_types:
  - mean
period:
  calendar:
    period: month
```

### Doporučený graf s procentem využití FUP (`apexcharts-card` z HACS)

Pokud si přes HACS (Frontend) doinstaluješ oblíbenou kartu
[`apexcharts-card`](https://github.com/RomRider/apexcharts-card), dá se
udělat pěkný "gauge" na využití dat:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Využití dat – 734714008
chart_type: radialBar
series:
  - entity: sensor.poda_734714008_data_vycerpano
    name: Vyčerpáno
    show:
      in_header: raw
    float_precision: 0
    transform: |
      return (x / states['sensor.poda_734714008_data_limit'].state) * 100;
apex_config:
  plotOptions:
    radialBar:
      dataLabels:
        value:
          formatter: |
            EVAL:function(val) { return val.toFixed(0) + " %" }
```

A klasický sloupcový graf porovnání obou čísel:

```yaml
type: custom:apexcharts-card
header:
  title: Volání – minuty za měsíc
  show: true
chart_type: bar
series:
  - entity: sensor.poda_734714008_volani_doba
    name: 734714008
  - entity: sensor.poda_702007088_volani_doba
    name: 702007088
```

*(Uprav `entity_id` podle skutečných čísel po instalaci – integrace je pojmenuje
podle nalezeného čísla, viz Nastavení → Zařízení.)*

## Struktura repozitáře

```
poda-data/
├── custom_components/
│   └── poda_data/
│       ├── __init__.py
│       ├── api.py            # přihlášení + stažení a parsování CSV/HTML
│       ├── config_flow.py    # UI průvodce přidáním integrace
│       ├── const.py
│       ├── coordinator.py    # pravidelná aktualizace dat
│       ├── manifest.json
│       ├── sensor.py         # 6 senzorů na číslo
│       ├── strings.json
│       └── translations/
│           ├── cs.json
│           └── en.json
├── .github/workflows/
│   ├── validate.yml          # HACS + hassfest kontrola při každém push
│   └── release.yml           # zabalí komponentu při vydání release
├── hacs.json
├── LICENSE
└── README.md
```

## Řešení problémů

- **„invalid_auth" při přidávání integrace** – ověř přihlašovací údaje ručně
  na https://klient.poda.cz. Pokud fungují, ale integrace je odmítá, zapni si
  debug log (viz níže) a zkontroluj, jak vypadá odpověď login formuláře.
- **Chybí senzory dat/SMS/volání** – zkontroluj log, integrace loguje warning,
  pokud se jí nepodaří najít odkaz „Stáhnout jako CSV" nebo tabulku dat.
  Struktura portálu se může změnit – v tom případě uprav `api.py` (funkce
  `_find_csv_link` / `_parse_data_usage`) podle aktuální HTML struktury.

Debug log si zapneš v `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.poda_data: debug
```

## Licence

MIT – viz [LICENSE](LICENSE).
