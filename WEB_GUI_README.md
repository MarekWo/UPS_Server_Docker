# Web GUI dla UPS Power Management Server

## Przegląd

Web GUI dla UPS Server umożliwia łatwe zarządzanie konfiguracją systemu przez interfejs webowy dostępny z przeglądarki. Interfejs jest w pełni responsywny i zoptymalizowany dla urządzeń mobilnych.

## Funkcjonalności Web GUI

### 🏠 Dashboard
- **Przegląd systemu**: Wyświetla statystyki liczbowe (liczba hostów, sentinel, klientów)
- **Status hostów sentinel**: Monitorowanie na żywo hostów używanych do wykrywania awarii zasilania
- **Status zarządzanych hostów**: Wyświetla status online/offline wszystkich hostów z Wake-on-LAN
- **Lista klientów UPS Hub**: Przegląd wszystkich klientów z konfiguracją opóźnień wyłączenia
- **Automatyczne odświeżanie**: Status hostów jest automatycznie odświeżany co 30 sekund
- **Wysyłanie WoL**: Przyciski do natychmiastowego wysłania sygnału Wake-on-LAN do wybranych hostów

### ⚙️ Konfiguracja
- **Konfiguracja główna**: Edycja parametrów systemowych (hosty sentinel, opóźnienie WoL, adres broadcast)
- **Zarządzanie hostami Wake-on-LAN**: Dodawanie, edytowanie i usuwanie hostów z automatycznym wybudzaniem
- **Zarządzanie klientami UPS Hub**: Konfiguracja klientów API z indywidualnymi opóźnieniami wyłączenia
- **Walidacja danych**: Automatyczna walidacja adresów IP i MAC z informacjami o błędach
- **Formatowanie automatyczne**: Inteligentne formatowanie adresów MAC podczas wpisywania

## Instalacja i Konfiguracja

### 1. Aktualizacja plików aplikacji

Skopiuj nowe pliki do katalogu aplikacji:

```bash
# Przejdź do katalogu aplikacji
cd /opt/ups-server-docker/app

# Stwórz katalog templates
mkdir -p templates

# Skopiuj nowe pliki (web_gui.py, templates/base.html, templates/dashboard.html, templates/config.html)
```

### 2. Aktualizacja Docker

Zastąp istniejące pliki:
- `entrypoint.sh` - dodaje uruchomienie Web GUI na porcie 80
- `Dockerfile` - dodaje port 80 i katalog templates
- `docker-compose.yml.example` - dokumentuje port 80

### 3. Przebudowa kontenera

```bash
# Zatrzymaj istniejący kontener
docker compose down

# Przebuduj i uruchom z nowymi plikami
docker compose up --build -d
```

## Dostęp do Web GUI

Po uruchomieniu kontenera, Web GUI będzie dostępne pod adresem:

```
http://<IP_SERWERA_UPS>
```

Na przykład, jeśli Twój serwer UPS ma IP `192.168.1.10`, otwórz:

```
http://192.168.1.10
```

## Porty i Usługi

Po aktualizacji kontener będzie udostępniał następujące usługi:

- **Port 80**: Web GUI (nowy)
- **Port 5000**: REST API (istniejący)
- **Port 3493**: NUT Server (istniejący)

## Responsywność i Urządzenia Mobilne

Web GUI został zaprojektowany z myślą o dostępności na wszystkich urządzeniach:

### 📱 Funkcje mobilne:
- **Responsywny design**: Automatyczne dopasowanie do rozmiaru ekranu
- **Touch-friendly**: Duże przyciski i elementy łatwe do dotykania
- **Przesuwane tabele**: Tabele z dużą ilością danych są przewijalne poziomo
- **Zoptymalizowane formularze**: Modalowe okna dialogowe dopasowane do ekranów mobilnych
- **Czytelne ikony**: Font Awesome z wyraźnymi ikonami statusu

### 💻 Funkcje desktop:
- **Hover effects**: Animacje przy najechaniu myszką
- **Zaawansowane tabele**: Pełne tabele z większą ilością informacji
- **Szersze układy**: Wykorzystanie pełnej szerokości ekranu

## Bezpieczeństwo

### ⚠️ Ważne uwagi bezpieczeństwa:

1. **Brak uwierzytelniania**: Web GUI nie posiada systemu logowania. Dostęp jest otwarty dla każdego, kto zna adres IP serwera.

2. **Użycie wewnętrzne**: Interface jest przeznaczony do użytku w bezpiecznej sieci wewnętrznej.

3. **Brak HTTPS**: Komunikacja odbywa się przez niezaszyfrowane HTTP.

### 🔒 Rekomendacje bezpieczeństwa:

- Użyj Web GUI tylko w zaufanej sieci lokalnej
- Rozważ ograniczenie dostępu przez firewall
- Dla środowisk produkcyjnych rozważ dodanie uwierzytelniania

## Rozwiązywanie problemów

### Web GUI nie ładuje się
1. Sprawdź, czy kontener działa: `docker ps`
2. Sprawdź logi: `docker compose logs`
3. Sprawdź dostępność portu 80: `curl http://localhost`

### Nie można zapisać konfiguracji
1. Sprawdź uprawnienia do plików konfiguracyjnych
2. Sprawdź logi aplikacji: `docker compose logs ups-server`
3. Sprawdź, czy pliki konfiguracyjne istnieją w `./config/`

### Status hostów nie odświeża się
1. Sprawdź połączenie sieciowe z kontenerem
2. Sprawdź logi przeglądarki (F12 -> Console)
3. Sprawdź, czy endpoint `/status` odpowiada: `curl http://<SERVER_IP>/status`

### Problemy z Wake-on-LAN
1. Upewnij się, że używasz `network_mode: host` w docker-compose.yml
2. Sprawdź, czy `wakeonlan` jest zainstalowane w kontenerze
3. Sprawdź poprawność adresów MAC i broadcast IP

## Funkcje zaawansowane

### Automatyczne odświeżanie statusu
- Status wszystkich hostów jest odświeżany automatycznie co 30 sekund
- Możesz ręcznie odświeżyć status klikając przycisk "Odśwież"

### Walidacja formularzy
- Adresy IP są automatycznie walidowane
- Adresy MAC są formatowane podczas wpisywania (XX:XX:XX:XX:XX:XX)
- Błędne dane są podświetlane z komunikatami błędów

### Powiadomienia
- Wszystkie akcje (zapisywanie, dodawanie, usuwanie) pokazują powiadomienia
- Powiadomienia znikają automatycznie po 3-5 sekundach
- Błędy są wyświetlane w kolorze czerwonym, sukces w zielonym

## API Endpoints używane przez Web GUI

Web GUI korzysta z następujących endpointów:

- `GET /` - Dashboard główny
- `GET /config` - Strona konfiguracji
- `POST /save_main_config` - Zapisywanie konfiguracji głównej
- `POST /add_wake_host` - Dodawanie nowego hosta WoL
- `POST /edit_wake_host/<section>` - Edytowanie hosta WoL
- `POST /delete_wake_host/<section>` - Usuwanie hosta WoL
- `POST /add_upshub_client` - Dodawanie klienta UPS Hub
- `POST /edit_upshub_client/<ip>` - Edytowanie klienta UPS Hub
- `POST /delete_upshub_client/<ip>` - Usuwanie klienta UPS Hub
- `GET /wol/<section>` - Wysyłanie Wake-on-LAN
- `GET /status` - Pobieranie aktualnego statusu hostów (JSON)

Wszystkie endpointy Web GUI są niezależne od istniejącego REST API na porcie 5000, które nadal działa bez zmian.