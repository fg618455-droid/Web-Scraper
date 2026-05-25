# Deal Scraper 🛒🔍

Ein automatisiertes Python-Tool zum Scrapen, Überwachen und Auswerten von Angeboten auf verschiedenen E-Commerce- und Kleinanzeigen-Plattformen. 

> **Hinweis:** Dieses Projekt befindet sich im aktiven Aufbau. Es werden in Kürze noch weitere Dateien (z. B. das Hauptskript `main.py` und Web-UI-Komponenten) hinzugefügt.

## ✨ Features

* **Multi-Plattform Unterstützung:** Scrapt Daten von Seiten wie *Kleinanzeigen, eBay, Apple, Amazon, mac-store24* und vielen mehr.
* **Smart Scraping:** Nutzt sowohl simple HTTP-Requests (`BeautifulSoup`) als auch dynamisches JavaScript-Rendering (`Playwright`), um Anti-Bot-Maßnahmen zu umgehen.
* **Lokale Datenbank:** Speichert Deals, Suchaufträge und Preisverläufe effizient in einer lokalen SQLite-Datenbank (`deals.db`).
* **Desktop-Benachrichtigungen:** Benachrichtigt den Nutzer sofort über neue Treffer direkt auf dem Desktop (`notifier.py`).
* **eBay Session-Management:** Intelligentes Cookie- und Session-Handling (`ebay_session.py`), um Gebotshistorien auch hinter Login-Schranken auszulesen.
* **Automatisches Setup (Windows):** Beinhaltet ein Batch-Skript für die schnelle Installation aller Abhängigkeiten inklusive Desktop-Verknüpfung.

## 📂 Projektstruktur (Bisherige Dateien)

* `scraper.py` - Enthält die Hauptlogik für das Scraping der verschiedenen Plattformen.
* `database.py` - Verwaltet die SQLite-Datenbank (`deals.db`), inkl. Tabellen für Deals, Alarme und Preisverläufe.
* `notifier.py` - Modul zum Senden von System-Benachrichtigungen (via `plyer`).
* `ebay_session.py` & `ebay_session.json` - Logik zur Authentifizierung und Erhaltung der Sitzung für eBay.
* `setup.bat` - Windows-Batch-Skript zur automatischen Installation von Python-Paketen und Playwright.
* `create_shortcut.py` - Erstellt automatisch eine Windows-Desktop-Verknüpfung für das Programm.
* `requirements.txt` - Liste aller benötigten Python-Bibliotheken (Flask, Playwright, BeautifulSoup4 etc.).
* `DealScraper.spec` - Konfigurationsdatei für PyInstaller, um das Skript später in eine ausführbare `.exe`-Datei zu kompilieren.

## 🚀 Installation & Start (Windows)

1. **Repository klonen:**
   ```bash
   git clone [https://github.com/fg618455-droid/Web-Scraper.git](https://fg618455-droid/Web-Scraper.git)
   cd deal-scraper
Setup ausführen:
Führe einfach die setup.bat per Doppelklick aus. Das Skript erledigt Folgendes automatisch:

Installiert alle Pakete aus der requirements.txt.

Installiert die benötigten Chromium-Browser für Playwright.

Erstellt eine Verknüpfung auf deinem Desktop.

Programm starten:
Sobald die fehlende main.py hinzugefügt wurde, kannst du das Programm entweder über die erstelle Desktop-Verknüpfung oder über das Terminal starten:

Bash
python main.py
🛠️ Technologien
Python 3.x

Scraping: Playwright, BeautifulSoup4, Requests

Web/UI: Flask (teilweise ausstehend)

Datenbank: SQLite3

Benachrichtigungen: Plyer

🤝 Mitwirken
Pull Requests sind willkommen! Bei größeren Änderungen öffne bitte zuerst ein Issue, um zu besprechen, was du ändern möchtest.

📝 Lizenz
MIT (Oder eine andere Lizenz deiner Wahl eintragen)


***
